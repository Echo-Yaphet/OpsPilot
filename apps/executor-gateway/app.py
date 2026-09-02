import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from issuer_client import request_identity
from workload_identity import IdentityError, verify_external_identity


RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
IDENTITY_ISSUER = os.getenv("WORKLOAD_IDENTITY_ISSUER", "opspilot-workload-identity-issuer")
IDENTITY_KEY_ID = os.getenv("WORKLOAD_IDENTITY_KEY_ID", "opspilot-issuer-v1")
IDENTITY_PUBLIC_KEY_FILE = os.getenv("WORKLOAD_IDENTITY_PUBLIC_KEY_FILE", "/identity/issuer-public/public.pem")
IDENTITY_AUDIENCE = os.getenv("EXECUTOR_IDENTITY_AUDIENCE", "opspilot-executor-gateway")
IDENTITY_MAX_TTL_SECONDS = int(os.getenv("EXECUTOR_IDENTITY_MAX_TTL_SECONDS", "15"))
DATABASE_PATH = os.getenv("EXECUTOR_DATABASE_PATH", "/data/executor.db")
DOCKER_PROXY_URL = os.getenv("DOCKER_PROXY_URL", "http://docker-proxy:2375")
DOCKER_PROXY_TIMEOUT = float(os.getenv("DOCKER_PROXY_TIMEOUT", "15"))
ISSUER_URL = os.getenv("WORKLOAD_IDENTITY_ISSUER_URL", "http://workload-identity-issuer:8085")
WORKLOAD_PRIVATE_KEY_FILE = os.getenv("WORKLOAD_IDENTITY_PRIVATE_KEY_FILE", "/identity/gateway-private/private.pem")
PROXY_AUDIENCE = os.getenv("DOCKER_PROXY_IDENTITY_AUDIENCE", "opspilot-docker-proxy")


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
        if "identity_key_id" not in columns:
            db.execute("ALTER TABLE execution_audit ADD COLUMN identity_key_id TEXT")
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
                operation,target,outcome,detail,created_at,identity_subject,credential_id,identity_key_id
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                operation, target, outcome, detail, datetime.now(timezone.utc).isoformat(),
                identity.get("sub") if identity else None,
                identity.get("jti") if identity else None,
                identity.get("key_id") if identity else None,
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
        identity = verify_external_identity(
            authorization.removeprefix("Bearer "),
            Path(IDENTITY_PUBLIC_KEY_FILE).read_bytes(), key_id=IDENTITY_KEY_ID,
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


async def runtime_request(method: str, path: str, operation: str, target: str) -> dict:
    credential = await request_identity(
        ISSUER_URL, WORKLOAD_PRIVATE_KEY_FILE, "executor-gateway",
        audience=PROXY_AUDIENCE, ttl_seconds=10, method=method, path=path,
        operation=operation, target=target,
    )
    headers = {"Authorization": f"Bearer {credential}"}
    async with httpx.AsyncClient(timeout=DOCKER_PROXY_TIMEOUT) as client:
        response = await client.request(
            method, f"{DOCKER_PROXY_URL.rstrip('/')}{path}", headers=headers,
        )
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"restricted Docker proxy rejected request: {detail}")
    return response.json()


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
        payload = await runtime_request("GET", f"/v1/containers/{target}/status", "container_status", target)
        return {"target": target, "status": payload["status"]}
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
        if action.operation == "restart_container":
            await runtime_request("POST", f"/v1/containers/{action.target}/restart", action.operation, action.target)
            result = f"restarted {action.target}"
        else:
            await runtime_request("POST", f"/v1/containers/{action.target}/stop", action.operation, action.target)
            result = f"stopped {action.target}"
        audit(action.operation, action.target, "allowed", result, identity)
        return {"status": "completed", "result": result}
    except Exception as exc:
        audit(action.operation, action.target, "failed", str(exc), identity)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
