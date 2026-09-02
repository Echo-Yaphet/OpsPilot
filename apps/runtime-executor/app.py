import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from workload_identity import IdentityError, verify_external_identity


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
ACTUATOR_ROOT = Path(os.getenv("RUNTIME_ACTUATOR_ROOT", "/run/actuators"))


def initialize_database() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS consumed_credentials (
            credential_id TEXT PRIMARY KEY, identity_subject TEXT NOT NULL,
            expires_at INTEGER NOT NULL, consumed_at INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS runtime_audit (
            id INTEGER PRIMARY KEY, operation TEXT NOT NULL, target TEXT NOT NULL,
            outcome TEXT NOT NULL, detail TEXT NOT NULL, identity_subject TEXT,
            credential_id TEXT, identity_key_id TEXT, created_at TEXT NOT NULL
        )""")


def consume_identity(identity: dict) -> None:
    now = int(time.time())
    try:
        with sqlite3.connect(DATABASE_PATH) as db:
            db.execute("DELETE FROM consumed_credentials WHERE expires_at < ?", (now - 60,))
            db.execute(
                "INSERT INTO consumed_credentials VALUES(?,?,?,?)",
                (identity["jti"], identity["sub"], identity["exp"], now),
            )
    except sqlite3.IntegrityError as exc:
        raise IdentityError("credential has already been used") from exc


def audit(operation: str, target: str, outcome: str, detail: str, identity: dict) -> None:
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute(
            """INSERT INTO runtime_audit(
                operation,target,outcome,detail,identity_subject,credential_id,identity_key_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                operation, target, outcome, detail, identity.get("sub"), identity.get("jti"),
                identity.get("key_id"), datetime.now(timezone.utc).isoformat(),
            ),
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
        expected_subject = "container-metrics-exporter" if operation == "container_stats" else "executor-gateway"
        if identity["sub"] != expected_subject:
            raise IdentityError("credential workload subject is not authorized")
        consume_identity(identity)
        return identity
    except (OSError, IdentityError) as exc:
        raise HTTPException(status_code=401, detail=f"invalid runtime executor identity: {exc}") from exc


def require_target(target: str, allowed: frozenset[str], operation: str) -> None:
    if target not in allowed:
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
initialize_database()


@app.get("/health")
async def health():
    available = sum((ACTUATOR_ROOT / target / "actuator.sock").exists() for target in ACTUATOR_TARGETS)
    return {"status": "ok" if available == len(ACTUATOR_TARGETS) else "degraded", "service": "opspilot-runtime-executor", "actuators": available}


@app.get("/metrics")
async def metrics():
    available = sum((ACTUATOR_ROOT / target / "actuator.sock").exists() for target in ACTUATOR_TARGETS)
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
