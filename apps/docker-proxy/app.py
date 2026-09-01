import hmac
import os

import docker
from fastapi import Depends, FastAPI, Header, HTTPException


STATUS_TARGETS = frozenset({
    "redis", "mysql", "user-service", "order-service", "payment-service",
    "prometheus", "alertmanager", "loki",
})
RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
STATS_TARGETS = frozenset({"user-service", "order-service", "payment-service"})
PROXY_TOKEN = os.getenv("DOCKER_PROXY_TOKEN", "")
DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")


def authorize(authorization: str | None = Header(None)) -> None:
    expected = f"Bearer {PROXY_TOKEN}"
    if not PROXY_TOKEN or not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid restricted Docker proxy identity")


def container_for(target: str):
    client = docker.DockerClient(base_url=DOCKER_HOST)
    matches = client.containers.list(all=True, filters={"label": f"com.docker.compose.service={target}"})
    if not matches:
        raise RuntimeError(f"container not found: {target}")
    return matches[0]


def require_target(target: str, allowed: frozenset[str], operation: str) -> None:
    if target not in allowed:
        raise HTTPException(status_code=403, detail=f"{operation} target is not allowlisted: {target}")


app = FastAPI(title="OpsPilot Restricted Docker Proxy", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-docker-proxy"}


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
