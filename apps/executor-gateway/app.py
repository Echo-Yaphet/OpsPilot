import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from workload_identity import IdentityError, verify_identity


RESTART_TARGETS = frozenset({"redis", "mysql", "user-service", "order-service", "payment-service"})
STOP_TARGETS = frozenset({"redis", "mysql"})
IDENTITY_KEY_ID = os.getenv("EXECUTOR_IDENTITY_KEY_ID", "control-api-v1")
IDENTITY_KEY = os.getenv("EXECUTOR_IDENTITY_KEY", "")
IDENTITY_PREVIOUS_KEY_ID = os.getenv("EXECUTOR_IDENTITY_PREVIOUS_KEY_ID", "")
IDENTITY_PREVIOUS_KEY = os.getenv("EXECUTOR_IDENTITY_PREVIOUS_KEY", "")
IDENTITY_PREVIOUS_KEY_VALID_UNTIL = os.getenv("EXECUTOR_IDENTITY_PREVIOUS_KEY_VALID_UNTIL", "")
IDENTITY_MAX_ROTATION_OVERLAP_SECONDS = int(
    os.getenv("EXECUTOR_IDENTITY_MAX_ROTATION_OVERLAP_SECONDS", "3600")
)
IDENTITY_ISSUER = os.getenv("EXECUTOR_IDENTITY_ISSUER", "opspilot-control-api")
IDENTITY_AUDIENCE = os.getenv("EXECUTOR_IDENTITY_AUDIENCE", "opspilot-executor-gateway")
IDENTITY_MAX_TTL_SECONDS = int(os.getenv("EXECUTOR_IDENTITY_MAX_TTL_SECONDS", "15"))
DATABASE_PATH = os.getenv("EXECUTOR_DATABASE_PATH", "/data/executor.db")
DOCKER_PROXY_URL = os.getenv("DOCKER_PROXY_URL", "http://docker-proxy:2375")
DOCKER_PROXY_TOKEN = os.getenv("DOCKER_PROXY_TOKEN", "")
DOCKER_PROXY_TIMEOUT = float(os.getenv("DOCKER_PROXY_TIMEOUT", "15"))


def load_identity_keyring(now: int | None = None) -> tuple[dict[str, str], str | None, int | None]:
    current = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", IDENTITY_KEY_ID):
        raise RuntimeError("EXECUTOR_IDENTITY_KEY_ID must be 1-64 safe characters")
    if not IDENTITY_KEY:
        raise RuntimeError("EXECUTOR_IDENTITY_KEY must not be empty")
    if not 1 <= IDENTITY_MAX_ROTATION_OVERLAP_SECONDS <= 86400:
        raise RuntimeError("identity rotation overlap limit must be between 1 and 86400 seconds")
    previous_values = (
        IDENTITY_PREVIOUS_KEY_ID,
        IDENTITY_PREVIOUS_KEY,
        IDENTITY_PREVIOUS_KEY_VALID_UNTIL,
    )
    if not any(previous_values):
        return {IDENTITY_KEY_ID: IDENTITY_KEY}, None, None
    if not all(previous_values):
        raise RuntimeError("previous identity key ID, key, and valid-until must be configured together")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", IDENTITY_PREVIOUS_KEY_ID):
        raise RuntimeError("EXECUTOR_IDENTITY_PREVIOUS_KEY_ID must be 1-64 safe characters")
    if IDENTITY_PREVIOUS_KEY_ID == IDENTITY_KEY_ID:
        raise RuntimeError("current and previous identity key IDs must differ")
    if IDENTITY_PREVIOUS_KEY == IDENTITY_KEY:
        raise RuntimeError("current and previous identity keys must differ")
    try:
        valid_until = int(IDENTITY_PREVIOUS_KEY_VALID_UNTIL)
    except ValueError as exc:
        raise RuntimeError("EXECUTOR_IDENTITY_PREVIOUS_KEY_VALID_UNTIL must be a Unix timestamp") from exc
    if valid_until <= current:
        raise RuntimeError("previous identity key overlap must end in the future")
    if valid_until > current + IDENTITY_MAX_ROTATION_OVERLAP_SECONDS:
        raise RuntimeError("previous identity key overlap exceeds the configured limit")
    return {
        IDENTITY_KEY_ID: IDENTITY_KEY,
        IDENTITY_PREVIOUS_KEY_ID: IDENTITY_PREVIOUS_KEY,
    }, IDENTITY_PREVIOUS_KEY_ID, valid_until


IDENTITY_KEYS, IDENTITY_PREVIOUS_ID, IDENTITY_PREVIOUS_VALID_UNTIL = load_identity_keyring()


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
        identity = verify_identity(
            authorization.removeprefix("Bearer "),
            verification_keys=IDENTITY_KEYS,
            issuer=IDENTITY_ISSUER,
            audience=IDENTITY_AUDIENCE,
            method=request.method,
            path=request.url.path,
            maximum_ttl_seconds=IDENTITY_MAX_TTL_SECONDS,
            previous_key_id=IDENTITY_PREVIOUS_ID,
            previous_key_valid_until=IDENTITY_PREVIOUS_VALID_UNTIL,
        )
        consume_identity(identity)
        return identity
    except IdentityError as exc:
        raise HTTPException(status_code=401, detail=f"invalid executor gateway identity: {exc}") from exc


async def runtime_request(method: str, path: str) -> dict:
    headers = {"Authorization": f"Bearer {DOCKER_PROXY_TOKEN}"}
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
        payload = await runtime_request("GET", f"/v1/containers/{target}/status")
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
            await runtime_request("POST", f"/v1/containers/{action.target}/restart")
            result = f"restarted {action.target}"
        else:
            await runtime_request("POST", f"/v1/containers/{action.target}/stop")
            result = f"stopped {action.target}"
        audit(action.operation, action.target, "allowed", result, identity)
        return {"status": "completed", "result": result}
    except Exception as exc:
        audit(action.operation, action.target, "failed", str(exc), identity)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
