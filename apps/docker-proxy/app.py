import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import docker
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from workload_identity import IdentityError, verify_external_identity


STATUS_TARGETS = frozenset({
    "redis", "mysql", "user-service", "order-service", "payment-service",
    "prometheus", "alertmanager", "loki",
})
RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
STATS_TARGETS = frozenset({"user-service", "order-service", "payment-service"})
SUPPORTED_LOG_TARGETS = frozenset({"user-service", "order-service", "payment-service"})
IDENTITY_ISSUER = os.getenv("WORKLOAD_IDENTITY_ISSUER", "opspilot-workload-identity-issuer")
IDENTITY_KEY_ID = os.getenv("WORKLOAD_IDENTITY_KEY_ID", "opspilot-issuer-v1")
IDENTITY_PUBLIC_KEY_FILE = os.getenv("WORKLOAD_IDENTITY_PUBLIC_KEY_FILE", "/identity/issuer-public/public.pem")
IDENTITY_AUDIENCE = os.getenv("DOCKER_PROXY_IDENTITY_AUDIENCE", "opspilot-docker-proxy")
IDENTITY_MAX_TTL_SECONDS = int(os.getenv("DOCKER_PROXY_IDENTITY_MAX_TTL_SECONDS", "15"))
IDENTITY_DATABASE_PATH = os.getenv("DOCKER_PROXY_IDENTITY_DATABASE_PATH", "/data/proxy-identity.db")
DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")
DOCKER_PROJECT = os.getenv("DOCKER_PROXY_PROJECT", "opspilot")
LOG_DISCOVERY_FILE = os.getenv("DOCKER_PROXY_LOG_DISCOVERY_FILE", "")
LOG_DISCOVERY_INTERVAL_SECONDS = float(os.getenv("DOCKER_PROXY_LOG_DISCOVERY_INTERVAL_SECONDS", "5"))


def configured_log_targets(raw: str) -> frozenset[str]:
    targets = frozenset(target.strip() for target in raw.split(",") if target.strip())
    if not targets:
        raise RuntimeError("DOCKER_PROXY_LOG_TARGETS must configure at least one service")
    unsupported = targets - SUPPORTED_LOG_TARGETS
    if unsupported:
        raise RuntimeError(
            "DOCKER_PROXY_LOG_TARGETS contains unsupported services: "
            + ", ".join(sorted(unsupported))
        )
    return targets


LOG_TARGETS = configured_log_targets(os.getenv(
    "DOCKER_PROXY_LOG_TARGETS",
    ",".join(sorted(SUPPORTED_LOG_TARGETS)),
))
LOG_TARGET_PUBLICATION_UP = 0
LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP = (
    Path(LOG_DISCOVERY_FILE).stat().st_mtime
    if LOG_DISCOVERY_FILE and Path(LOG_DISCOVERY_FILE).is_file()
    else 0.0
)
LOG_TARGET_PUBLICATION_FAILURES = 0
LOG_TARGET_COUNT = 0
log = logging.getLogger(__name__)


def log_target_info(targets: list[dict]) -> list[tuple[str, str]]:
    info = []
    for target in targets:
        labels = target.get("labels", {})
        service = labels.get("compose_service")
        path = labels.get("__path__")
        if service in LOG_TARGETS and isinstance(path, str) and path:
            info.append((service, path))
    return sorted(set(info))


def load_log_target_info(path: str) -> list[tuple[str, str]]:
    if not path:
        return []
    try:
        targets = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return log_target_info(targets) if isinstance(targets, list) else []


def prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


LOG_TARGET_INFO = load_log_target_info(LOG_DISCOVERY_FILE)


def initialize_identity_database() -> None:
    Path(IDENTITY_DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(IDENTITY_DATABASE_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS consumed_credentials (
            credential_id TEXT PRIMARY KEY, identity_subject TEXT NOT NULL,
            expires_at INTEGER NOT NULL, consumed_at INTEGER NOT NULL
        )""")


def consume_identity(identity: dict) -> None:
    now = int(time.time())
    try:
        with sqlite3.connect(IDENTITY_DATABASE_PATH) as db:
            db.execute("DELETE FROM consumed_credentials WHERE expires_at < ?", (now - 60,))
            db.execute(
                "INSERT INTO consumed_credentials VALUES(?,?,?,?)",
                (identity["jti"], identity["sub"], identity["exp"], now),
            )
    except sqlite3.IntegrityError as exc:
        raise IdentityError("credential has already been used") from exc


def authorize(request: Request, authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid restricted Docker proxy identity")
    target = request.path_params.get("target", "")
    suffix = request.url.path.rsplit("/", 1)[-1]
    operation = {"status": "container_status", "stats": "container_stats", "restart": "restart_container", "stop": "stop_container"}.get(suffix)
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
        raise HTTPException(status_code=401, detail=f"invalid restricted Docker proxy identity: {exc}") from exc


def container_for(target: str):
    client = docker.DockerClient(base_url=DOCKER_HOST)
    matches = client.containers.list(all=True, filters={"label": [
        f"com.docker.compose.project={DOCKER_PROJECT}",
        f"com.docker.compose.service={target}",
    ]})
    if not matches:
        raise RuntimeError(f"container not found: {target}")
    return matches[0]


def require_target(target: str, allowed: frozenset[str], operation: str) -> None:
    if target not in allowed:
        raise HTTPException(status_code=403, detail=f"{operation} target is not allowlisted: {target}")


def discover_log_targets() -> list[dict]:
    client = docker.DockerClient(base_url=DOCKER_HOST)
    discovered = []
    for service in sorted(LOG_TARGETS):
        containers = client.containers.list(all=True, filters={"label": [
            f"com.docker.compose.project={DOCKER_PROJECT}",
            f"com.docker.compose.service={service}",
        ]})
        for container in containers:
            container_id = container.id
            discovered.append({
                "targets": ["localhost"],
                "labels": {
                    "__path__": (
                        f"/var/lib/docker/containers/{container_id}/"
                        f"{container_id}-json.log"
                    ),
                    "compose_service": service,
                    "container": f"/{container.name}",
                },
            })
    return sorted(
        discovered,
        key=lambda target: (
            target["labels"]["compose_service"],
            target["labels"]["container"],
        ),
    )


def publish_log_targets(path: str, targets: list[dict]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(json.dumps(targets, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, destination)


async def refresh_log_targets_once() -> None:
    global LOG_TARGET_PUBLICATION_UP
    global LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP
    global LOG_TARGET_PUBLICATION_FAILURES
    global LOG_TARGET_COUNT
    global LOG_TARGET_INFO
    try:
        targets = await asyncio.to_thread(discover_log_targets)
        LOG_TARGET_COUNT = len(targets)
        if not targets:
            LOG_TARGET_PUBLICATION_UP = 0
            LOG_TARGET_PUBLICATION_FAILURES += 1
            log.warning("no allowlisted log targets discovered; retaining last-known-good file")
            return
        await asyncio.to_thread(publish_log_targets, LOG_DISCOVERY_FILE, targets)
        LOG_TARGET_PUBLICATION_UP = 1
        LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP = time.time()
        LOG_TARGET_INFO = log_target_info(targets)
    except Exception:
        LOG_TARGET_PUBLICATION_UP = 0
        LOG_TARGET_COUNT = 0
        LOG_TARGET_PUBLICATION_FAILURES += 1
        log.exception("failed to refresh allowlisted Promtail log targets")


async def refresh_log_targets() -> None:
    while True:
        await refresh_log_targets_once()
        await asyncio.sleep(LOG_DISCOVERY_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(refresh_log_targets()) if LOG_DISCOVERY_FILE else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="OpsPilot Restricted Docker Proxy", version="0.1.0", lifespan=lifespan)
initialize_identity_database()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-docker-proxy"}


@app.get("/metrics")
async def metrics():
    lines = [
        "# HELP docker_proxy_log_target_publication_up Whether the latest allowlisted log target publication succeeded.",
        "# TYPE docker_proxy_log_target_publication_up gauge",
        f"docker_proxy_log_target_publication_up {LOG_TARGET_PUBLICATION_UP}",
        "# HELP docker_proxy_log_target_publication_last_success_timestamp_seconds Unix time of the last successful log target publication.",
        "# TYPE docker_proxy_log_target_publication_last_success_timestamp_seconds gauge",
        (
            "docker_proxy_log_target_publication_last_success_timestamp_seconds "
            f"{LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP:.3f}"
        ),
        "# HELP docker_proxy_log_targets Number of allowlisted log targets discovered in the latest attempt.",
        "# TYPE docker_proxy_log_targets gauge",
        f"docker_proxy_log_targets {LOG_TARGET_COUNT}",
        "# HELP docker_proxy_log_target_publication_failures_total Total failed or empty log target publication attempts.",
        "# TYPE docker_proxy_log_target_publication_failures_total counter",
        f"docker_proxy_log_target_publication_failures_total {LOG_TARGET_PUBLICATION_FAILURES}",
        "# HELP docker_proxy_log_target_info Allowlisted Promtail target path mapped to its Compose service.",
        "# TYPE docker_proxy_log_target_info gauge",
    ]
    lines.extend(
        'docker_proxy_log_target_info{service="'
        f'{prometheus_label(service)}",path="{prometheus_label(path)}"}} 1'
        for service, path in LOG_TARGET_INFO
    )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/v1/containers/{target}/status", dependencies=[Depends(authorize)])
async def container_status(target: str):
    require_target(target, STATUS_TARGETS, "status")
    try:
        return {"target": target, "status": container_for(target).status}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/containers/{target}/stats", dependencies=[Depends(authorize)])
def container_stats(target: str):
    """Return only the CPU counters required by the metrics exporter."""
    require_target(target, STATS_TARGETS, "stats")
    try:
        stats = container_for(target).stats(stream=False)
        current = stats.get("cpu_stats", {})
        previous = stats.get("precpu_stats", {})
        return {
            "target": target,
            "cpu_total_usage": current.get("cpu_usage", {}).get("total_usage", 0),
            "previous_cpu_total_usage": previous.get("cpu_usage", {}).get("total_usage", 0),
            "system_cpu_usage": current.get("system_cpu_usage", 0),
            "previous_system_cpu_usage": previous.get("system_cpu_usage", 0),
            "online_cpus": current.get("online_cpus", 1),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/containers/{target}/restart", dependencies=[Depends(authorize)])
async def restart_container(target: str):
    require_target(target, RESTART_TARGETS, "restart")
    try:
        container_for(target).restart(timeout=10)
        return {"status": "completed", "result": f"restarted {target}"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/containers/{target}/stop", dependencies=[Depends(authorize)])
async def stop_container(target: str):
    require_target(target, STOP_TARGETS, "stop")
    try:
        container_for(target).stop(timeout=10)
        return {"status": "completed", "result": f"stopped {target}"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
