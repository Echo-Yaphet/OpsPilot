import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import docker
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from workload_identity import IdentityError, verify_identity


RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
IDENTITY_KEY = os.getenv("EXECUTOR_IDENTITY_KEY", "")
IDENTITY_ISSUER = os.getenv("EXECUTOR_IDENTITY_ISSUER", "opspilot-control-api")
IDENTITY_AUDIENCE = os.getenv("EXECUTOR_IDENTITY_AUDIENCE", "opspilot-executor-gateway")
IDENTITY_MAX_TTL_SECONDS = int(os.getenv("EXECUTOR_IDENTITY_MAX_TTL_SECONDS", "15"))
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
        columns = {row[1] for row in db.execute("PRAGMA table_info(execution_audit)")}
        if "identity_subject" not in columns:
            db.execute("ALTER TABLE execution_audit ADD COLUMN identity_subject TEXT")
        if "credential_id" not in columns:
            db.execute("ALTER TABLE execution_audit ADD COLUMN credential_id TEXT")
        db.execute("""
            CREATE TABLE IF NOT EXISTS consumed_credentials (
                credential_id TEXT PRIMARY KEY, identity_subject TEXT NOT NULL,
                expires_at INTEGER NOT NULL, consumed_at TEXT NOT NULL
            )
        """)


def audit(operation: str, target: str, outcome: str, detail: str, identity: dict | None = None) -> None:
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute(
            """INSERT INTO execution_audit(
                operation,target,outcome,detail,created_at,identity_subject,credential_id
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                operation, target, outcome, detail, datetime.now(timezone.utc).isoformat(),
                identity.get("sub") if identity else None,
                identity.get("jti") if identity else None,
            ),
        )


def consume_identity(identity: dict) -> None:
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        with sqlite3.connect(DATABASE_PATH) as db:
            # Keep recently expired IDs beyond the verifier's clock-skew window.
            db.execute("DELETE FROM consumed_credentials WHERE expires_at < ?", (now - 60,))
            db.execute(
                """INSERT INTO consumed_credentials(
                    credential_id,identity_subject,expires_at,consumed_at
                ) VALUES(?,?,?,?)""",
                (identity["jti"], identity["sub"], identity["exp"], datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.IntegrityError as exc:
        raise IdentityError("credential has already been used") from exc


def authorize(request: Request, authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid executor gateway identity")
    try:
        identity = verify_identity(
            authorization.removeprefix("Bearer "),
            IDENTITY_KEY,
            issuer=IDENTITY_ISSUER,
            audience=IDENTITY_AUDIENCE,
            method=request.method,
            path=request.url.path,
            maximum_ttl_seconds=IDENTITY_MAX_TTL_SECONDS,
        )
        consume_identity(identity)
        return identity
    except IdentityError as exc:
        raise HTTPException(status_code=401, detail=f"invalid executor gateway identity: {exc}") from exc


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


@app.get("/v1/containers/{target}/status")
async def container_status(target: str, identity: dict = Depends(authorize)):
    if identity["operation"] != "container_status" or identity["target"] != target:
        raise HTTPException(status_code=401, detail="credential action claims do not match request")
    if target not in RESTART_TARGETS | {"prometheus", "alertmanager", "loki"}:
        raise HTTPException(status_code=403, detail=f"status target is not allowlisted: {target}")
    try:
        return {"target": target, "status": container_for(target).status}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/actions")
async def execute(action: ActionRequest, identity: dict = Depends(authorize)):
    if identity["operation"] != action.operation or identity["target"] != action.target:
        detail = "credential action claims do not match request"
        audit(action.operation, action.target, "denied", detail, identity)
        raise HTTPException(status_code=401, detail=detail)
    allowed = RESTART_TARGETS if action.operation == "restart_container" else STOP_TARGETS
    if action.target not in allowed:
        detail = f"{action.operation} target is not allowlisted: {action.target}"
        audit(action.operation, action.target, "denied", detail, identity)
        raise HTTPException(status_code=403, detail=detail)
    try:
        container = container_for(action.target)
        if action.operation == "restart_container":
            container.restart(timeout=10)
            result = f"restarted {action.target}"
        else:
            container.stop(timeout=10)
            result = f"stopped {action.target}"
        audit(action.operation, action.target, "allowed", result, identity)
        return {"status": "completed", "result": result}
    except Exception as exc:
        audit(action.operation, action.target, "failed", str(exc), identity)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
