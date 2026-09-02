import os
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from workload_identity import IdentityError, verify_external_identity
from runtime_store import ReplayError, RuntimeStore


STATUS_TARGETS = frozenset({
    "redis", "mysql", "user-service", "order-service", "payment-service",
    "prometheus", "alertmanager", "loki",
})
RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
STATS_TARGETS = frozenset({"user-service", "order-service", "payment-service"})
ACTUATOR_TARGETS = RESTART_TARGETS
IDENTITY_ISSUER = os.getenv("WORKLOAD_IDENTITY_ISSUER", "opspilot-workload-identity-issuer")
IDENTITY_KEY_ID = os.getenv("WORKLOAD_IDENTITY_KEY_ID", "opspilot-issuer-v1")
IDENTITY_PUBLIC_KEY_FILE = os.getenv("WORKLOAD_IDENTITY_PUBLIC_KEY_FILE", "/identity/issuer-public/public.pem")
IDENTITY_AUDIENCE = os.getenv("RUNTIME_EXECUTOR_IDENTITY_AUDIENCE", "opspilot-runtime-executor")
IDENTITY_MAX_TTL_SECONDS = int(os.getenv("RUNTIME_EXECUTOR_IDENTITY_MAX_TTL_SECONDS", "15"))
DATABASE_PATH = os.getenv("RUNTIME_EXECUTOR_DATABASE_PATH", "/data/runtime-executor.db")
DATABASE_URL = os.getenv("RUNTIME_EXECUTOR_DATABASE_URL", "")
ACTUATOR_ROOT = Path(os.getenv("RUNTIME_ACTUATOR_ROOT", "/run/actuators"))
PLACEMENT = os.getenv("RUNTIME_EXECUTOR_PLACEMENT", "local-compose")
EXECUTOR_ID = os.getenv("RUNTIME_EXECUTOR_ID", "runtime-executor-local")


def load_targets() -> frozenset[str]:
    raw = os.getenv("RUNTIME_EXECUTOR_TARGETS", "")
    targets = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if targets and not targets.issubset(ACTUATOR_TARGETS):
        raise ValueError("RUNTIME_EXECUTOR_TARGETS contains a non-actuator target")
    return targets or ACTUATOR_TARGETS


LOCAL_TARGETS = load_targets()
STORE = RuntimeStore(database_path=DATABASE_PATH, database_url=DATABASE_URL)


def consume_identity(identity: dict) -> None:
    try:
        STORE.consume(identity, placement=PLACEMENT, executor_id=EXECUTOR_ID)
    except ReplayError as exc:
        raise IdentityError("credential has already been used") from exc


def audit(operation: str, target: str, outcome: str, detail: str, identity: dict) -> None:
    STORE.audit(
        operation, target, outcome, detail, identity,
        placement=PLACEMENT, executor_id=EXECUTOR_ID,
    )


def request_operation(request: Request) -> str | None:
    suffix = request.url.path.rsplit("/", 1)[-1]
    return {
        "status": "container_status", "stats": "container_stats",
        "restart": "restart_container", "stop": "stop_container",
    }.get(suffix)


def authorize(request: Request, authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid runtime executor identity")
    target = request.path_params.get("target", "")
    operation = request_operation(request)
    try:
        identity = verify_external_identity(
            authorization.removeprefix("Bearer "), Path(IDENTITY_PUBLIC_KEY_FILE).read_bytes(),
            key_id=IDENTITY_KEY_ID, issuer=IDENTITY_ISSUER, audience=IDENTITY_AUDIENCE,
            method=request.method, path=request.url.path, maximum_ttl_seconds=IDENTITY_MAX_TTL_SECONDS,
        )
        if identity["operation"] != operation or identity["target"] != target:
            raise IdentityError("credential action claims do not match request")
        if identity.get("placement") != PLACEMENT:
            raise IdentityError("credential placement does not match this executor")
        expected_subject = "container-metrics-exporter" if operation == "container_stats" else "executor-gateway"
        if identity["sub"] != expected_subject:
            raise IdentityError("credential workload subject is not authorized")
        consume_identity(identity)
        return identity
    except (OSError, IdentityError) as exc:
        raise HTTPException(status_code=401, detail=f"invalid runtime executor identity: {exc}") from exc


def require_target(target: str, allowed: frozenset[str], operation: str) -> None:
    if target not in allowed or (target in ACTUATOR_TARGETS and target not in LOCAL_TARGETS):
        raise HTTPException(status_code=403, detail=f"{operation} target is not allowlisted: {target}")


async def actuator_request(target: str, method: str, path: str) -> dict:
    if target not in ACTUATOR_TARGETS:
        raise RuntimeError(f"no OS-isolated actuator for target: {target}")
    socket_path = ACTUATOR_ROOT / target / "actuator.sock"
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://actuator", timeout=12) as client:
        response = await client.request(method, path)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"target actuator rejected request: {detail}")
    return response.json()


app = FastAPI(title="OpsPilot OS-Isolated Runtime Executor", version="0.1.0")


@app.get("/health")
async def health():
    available = sum((ACTUATOR_ROOT / target / "actuator.sock").exists() for target in LOCAL_TARGETS)
    return {
        "status": "ok" if available == len(LOCAL_TARGETS) else "degraded",
        "service": "opspilot-runtime-executor", "actuators": available,
        "placement": PLACEMENT, "executor_id": EXECUTOR_ID, "shared_store": STORE.shared,
    }


@app.get("/metrics")
async def metrics():
    available = sum((ACTUATOR_ROOT / target / "actuator.sock").exists() for target in LOCAL_TARGETS)
    body = (
        "# HELP runtime_executor_actuators_available OS-isolated target actuators with a ready Unix socket.\n"
        "# TYPE runtime_executor_actuators_available gauge\n"
        f"runtime_executor_actuators_available {available}\n"
    )
    return Response(body, media_type="text/plain; version=0.0.4")


@app.get("/v1/containers/{target}/status", dependencies=[Depends(authorize)])
async def container_status(target: str):
    require_target(target, STATUS_TARGETS, "status")
    if target not in ACTUATOR_TARGETS:
        return {"target": target, "status": "running"}
    try:
        return await actuator_request(target, "GET", "/v1/status")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/containers/{target}/stats", dependencies=[Depends(authorize)])
async def container_stats(target: str):
    require_target(target, STATS_TARGETS, "stats")
    try:
        return await actuator_request(target, "GET", "/v1/stats")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def execute_action(target: str, operation: str, identity: dict) -> dict:
    path = "/v1/restart" if operation == "restart_container" else "/v1/stop"
    try:
        result = await actuator_request(target, "POST", path)
        audit(operation, target, "allowed", result["result"], identity)
        return result
    except Exception as exc:
        audit(operation, target, "failed", str(exc), identity)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/containers/{target}/restart")
async def restart_container(target: str, identity: dict = Depends(authorize)):
    require_target(target, RESTART_TARGETS, "restart")
    return await execute_action(target, "restart_container", identity)


@app.post("/v1/containers/{target}/stop")
async def stop_container(target: str, identity: dict = Depends(authorize)):
    require_target(target, STOP_TARGETS, "stop")
    return await execute_action(target, "stop_container", identity)
