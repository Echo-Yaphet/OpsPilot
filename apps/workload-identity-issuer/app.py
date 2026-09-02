import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from workload_identity import IdentityError, mint_external_identity, verify_issuer_request


ISSUER = os.getenv("WORKLOAD_IDENTITY_ISSUER", "opspilot-workload-identity-issuer")
KEY_ID = os.getenv("WORKLOAD_IDENTITY_KEY_ID", "opspilot-issuer-v1")
PRIVATE_KEY_FILE = os.getenv("WORKLOAD_IDENTITY_SIGNING_KEY_FILE", "/identity/issuer-private/private.pem")
DATABASE_PATH = os.getenv("WORKLOAD_IDENTITY_DATABASE_PATH", "/data/issuer.db")
CLIENT_KEYS = json.loads(os.getenv("WORKLOAD_IDENTITY_CLIENT_KEYS", "{}"))
ALLOWED_AUDIENCES = {
    "control-api": {"opspilot-executor-gateway"},
    "executor-gateway": {"opspilot-docker-proxy"},
    "container-metrics-exporter": {"opspilot-docker-proxy"},
}
ALLOWED_OPERATIONS = {
    "control-api": {"container_status", "restart_container", "stop_container"},
    "executor-gateway": {"container_status", "restart_container", "stop_container"},
    "container-metrics-exporter": {"container_stats"},
}


class IdentityRequest(BaseModel):
    audience: str
    ttl_seconds: int = Field(ge=1, le=15)
    method: str
    path: str
    operation: str
    target: str


def initialize_database() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS consumed_issuer_nonces (
            nonce TEXT PRIMARY KEY, subject TEXT NOT NULL, consumed_at INTEGER NOT NULL
        )""")


def consume_nonce(nonce: str, subject: str) -> None:
    now = int(time.time())
    try:
        with sqlite3.connect(DATABASE_PATH) as db:
            db.execute("DELETE FROM consumed_issuer_nonces WHERE consumed_at < ?", (now - 60,))
            db.execute(
                "INSERT INTO consumed_issuer_nonces(nonce,subject,consumed_at) VALUES(?,?,?)",
                (nonce, subject, now),
            )
    except sqlite3.IntegrityError as exc:
        raise IdentityError("issuer request has already been used") from exc


app = FastAPI(title="OpsPilot Workload Identity Issuer", version="0.1.0")
initialize_database()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-workload-identity-issuer", "key_id": KEY_ID}


@app.post("/v1/identity")
async def issue_identity(
    request: IdentityRequest,
    x_workload_subject: str | None = Header(None),
    x_workload_timestamp: str | None = Header(None),
    x_workload_nonce: str | None = Header(None),
    x_workload_signature: str | None = Header(None),
):
    subject = x_workload_subject or ""
    key_file = CLIENT_KEYS.get(subject)
    if not key_file or request.audience not in ALLOWED_AUDIENCES.get(subject, set()):
        raise HTTPException(status_code=403, detail="workload or audience is not allowlisted")
    if request.operation not in ALLOWED_OPERATIONS[subject]:
        raise HTTPException(status_code=403, detail="workload operation is not allowlisted")
    if not all((x_workload_timestamp, x_workload_nonce, x_workload_signature)):
        raise HTTPException(status_code=401, detail="workload proof is required")
    payload = request.model_dump()
    try:
        verify_issuer_request(
            Path(key_file).read_bytes(), subject, x_workload_timestamp,
            x_workload_nonce, x_workload_signature, payload,
        )
        consume_nonce(x_workload_nonce, subject)
    except (OSError, IdentityError) as exc:
        raise HTTPException(status_code=401, detail=f"invalid workload proof: {exc}") from exc
    token = mint_external_identity(
        Path(PRIVATE_KEY_FILE).read_bytes(), key_id=KEY_ID, issuer=ISSUER,
        audience=request.audience, subject=subject, ttl_seconds=request.ttl_seconds,
        method=request.method, path=request.path, operation=request.operation, target=request.target,
    )
    return {"token": token, "expires_in": request.ttl_seconds, "key_id": KEY_ID}
