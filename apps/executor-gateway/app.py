import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import docker
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel


RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
TOKEN = os.getenv("EXECUTOR_GATEWAY_TOKEN", "")
DATABASE_PATH = os.getenv("EXECUTOR_DATABASE_PATH", "/data/executor.db")


class ActionRequest(BaseModel):
    operation: Literal["restart_container", "stop_container"]
    target: str


def initialize_database() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY, operation TEXT NOT NULL, target TEXT NOT NULL,
                outcome TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            )
        """)


def audit(operation: str, target: str, outcome: str, detail: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute(
            "INSERT INTO execution_audit(operation,target,outcome,detail,created_at) VALUES(?,?,?,?,?)",
            (operation, target, outcome, detail, datetime.now(timezone.utc).isoformat()),
        )


def authorize(authorization: str | None = Header(None)) -> None:
    expected = f"Bearer {TOKEN}"
    if not TOKEN or not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid executor gateway identity")


def container_for(target: str):
    client = docker.DockerClient(base_url=os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock"))
    matches = client.containers.list(all=True, filters={"label": f"com.docker.compose.service={target}"})
    if not matches:
        raise RuntimeError(f"container not found: {target}")
    return matches[0]


app = FastAPI(title="OpsPilot Executor Gateway", version="0.1.0")
initialize_database()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-executor-gateway"}


@app.get("/v1/containers/{target}/status", dependencies=[Depends(authorize)])
async def container_status(target: str):
    if target not in RESTART_TARGETS | {"prometheus", "alertmanager", "loki"}:
        raise HTTPException(status_code=403, detail=f"status target is not allowlisted: {target}")
    try:
        return {"target": target, "status": container_for(target).status}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/actions", dependencies=[Depends(authorize)])
async def execute(action: ActionRequest):
    allowed = RESTART_TARGETS if action.operation == "restart_container" else STOP_TARGETS
    if action.target not in allowed:
        detail = f"{action.operation} target is not allowlisted: {action.target}"
        audit(action.operation, action.target, "denied", detail)
        raise HTTPException(status_code=403, detail=detail)
    try:
        container = container_for(action.target)
        if action.operation == "restart_container":
            container.restart(timeout=10)
            result = f"restarted {action.target}"
        else:
            container.stop(timeout=10)
            result = f"stopped {action.target}"
        audit(action.operation, action.target, "allowed", result)
        return {"status": "completed", "result": result}
    except Exception as exc:
        audit(action.operation, action.target, "failed", str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
