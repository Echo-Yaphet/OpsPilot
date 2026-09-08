import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict


WORKSPACE = Path(os.getenv("REPAIR_WORKSPACE", "/lab"))
DATABASE = os.getenv("REPAIR_DATABASE", "/data/repair.db")
TOKEN = os.getenv("REPAIR_SANDBOX_TOKEN", "")
APPROVAL_KEY = os.getenv("REPAIR_APPROVAL_KEY", "")
VALIDATOR_URL = os.getenv("REPAIR_VALIDATOR_URL", "http://repair-validator:8096")
VALIDATOR_TOKEN = os.getenv("REPAIR_VALIDATOR_TOKEN", "")
REPLICA_URL = os.getenv("REPAIR_REPLICA_URL", "http://repair-lab-payment:8097")
TARGET = "repair-lab-payment"
BROKEN_CONFIG = {"service_version": "payment-lab-v1", "redis_url": "redis://redis.invalid:6379/0"}
app = FastAPI(title="OpsPilot isolated repair sandbox")


class CandidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_version: str
    redis_url: str


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_digest: str
    config: CandidateConfig


class ShellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str


class DiagnosticScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["redis-connectivity-check"]
    steps: list[Literal["show-config-digest", "resolve-configured-redis"]]


def run_command(command: str, config: dict) -> dict:
    if command == "show-config-digest":
        argv = ["sha256sum", str(active_path())]
    elif command == "resolve-configured-redis":
        host = config["redis_url"].split("//", 1)[1].split(":", 1)[0]
        argv = ["getent", "hosts", host]
    else:
        raise HTTPException(status_code=403, detail="shell command is not allowlisted")
    try:
        result = subprocess.run(
            argv, cwd=WORKSPACE, capture_output=True, text=True, timeout=2,
            env={"PATH": "/usr/bin:/bin"}, check=False,
        )
        return {"command": command, "exit_code": result.returncode,
                "stdout": result.stdout[:2000], "stderr": result.stderr[:2000]}
    except subprocess.TimeoutExpired as exc:
        return {"command": command, "exit_code": 124,
                "stdout": (exc.stdout or "")[:2000], "stderr": "command timed out"}


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_id: str
    package_digest: str
    base_digest: str
    target: str
    expires_at: int
    jti: str
    signature: str


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def active_path() -> Path:
    return WORKSPACE / "active" / "config.json"


def load_active() -> dict:
    return json.loads(active_path().read_text())


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical(value))
    os.replace(temporary, path)


def connect():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


@app.on_event("startup")
def startup():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    Path(DATABASE).parent.mkdir(parents=True, exist_ok=True)
    if not active_path().exists():
        atomic_write(active_path(), BROKEN_CONFIG)
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS packages(
              package_id TEXT PRIMARY KEY, package_digest TEXT NOT NULL, base_digest TEXT NOT NULL,
              payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consumed_approvals(jti TEXT PRIMARY KEY, consumed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
              package_id TEXT, detail TEXT NOT NULL, created_at TEXT NOT NULL);
        """)


def authorize(value: str | None):
    if not TOKEN or value != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="sandbox identity required")


async def validate_candidate(config: CandidateConfig) -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(
            f"{VALIDATOR_URL}/v1/validate", json=config.model_dump(),
            headers={"Authorization": f"Bearer {VALIDATOR_TOKEN}"},
        )
    response.raise_for_status()
    result = response.json()
    if (
        not isinstance(result, dict)
        or result.get("probe_version") != "redis-config-probe-v1"
        or result.get("config_digest") != digest(config.model_dump())
        or not isinstance(result.get("observations"), list)
    ):
        raise HTTPException(status_code=502, detail="validator returned an invalid attestation")
    return result


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/workspace")
async def workspace(authorization: str | None = Header(default=None)):
    authorize(authorization)
    config = load_active()
    return {"target": TARGET, "config": config, "base_digest": digest(config)}


@app.get("/v1/replica-health")
async def replica_health(authorization: str | None = Header(default=None)):
    """Expose only the fixed fault-replica probe, never an arbitrary URL."""
    authorize(authorization)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{REPLICA_URL}/health")
        return {"status_code": response.status_code, "result": response.json()}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status_code": 503, "result": {"error_type": type(exc).__name__}}


@app.post("/v1/shell")
async def shell(request: ShellRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    return run_command(request.command, load_active())


@app.post("/v1/scripts")
async def create_script(request: DiagnosticScriptRequest,
                        authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not 1 <= len(request.steps) <= 4:
        raise HTTPException(status_code=422, detail="diagnostic script needs 1-4 steps")
    script = {
        "script_id": str(uuid4()), "target": TARGET, "name": request.name,
        "base_digest": digest(load_active()), "steps": request.steps,
    }
    script["script_digest"] = digest(script)
    atomic_write(WORKSPACE / "scripts" / script["script_id"] / "script.json", script)
    return script


@app.post("/v1/scripts/{script_id}/run")
async def run_script(script_id: UUID, authorization: str | None = Header(default=None)):
    authorize(authorization)
    path = WORKSPACE / "scripts" / str(script_id) / "script.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="diagnostic script not found")
    script = json.loads(path.read_text())
    unsigned = {key: value for key, value in script.items() if key != "script_digest"}
    if digest(unsigned) != script.get("script_digest"):
        raise HTTPException(status_code=409, detail="diagnostic script integrity check failed")
    if script["base_digest"] != digest(load_active()):
        raise HTTPException(status_code=409, detail="diagnostic script base changed")
    results = [run_command(step, load_active()) for step in script["steps"]]
    return {"script_id": str(script_id), "script_digest": script["script_digest"],
            "results": results}


@app.post("/v1/candidates")
async def create_candidate(request: CandidateRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    current = load_active()
    if request.base_digest != digest(current):
        raise HTTPException(status_code=409, detail="base configuration changed")
    validation = await validate_candidate(request.config)
    if not validation["passed"]:
        raise HTTPException(status_code=422, detail={"validation": validation})
    package_id = str(uuid4())
    candidate = request.config.model_dump()
    package = {
        "package_id": package_id, "target": TARGET, "base_digest": request.base_digest,
        "candidate_digest": digest(candidate), "candidate": candidate, "validation": validation,
    }
    package["package_digest"] = digest(package)
    atomic_write(WORKSPACE / "packages" / package_id / "package.json", package)
    with connect() as db:
        db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)", (
            package_id, package["package_digest"], request.base_digest,
            json.dumps(package), "validated", datetime.now(timezone.utc).isoformat(),
        ))
    return package


def approval_message(approval: Approval) -> bytes:
    return canonical({k: v for k, v in approval.model_dump().items() if k != "signature"})


@app.post("/v1/packages/{package_id}/apply")
async def apply_package(package_id: str, approval: Approval,
                        authorization: str | None = Header(default=None)):
    authorize(authorization)
    if not APPROVAL_KEY or not hmac.compare_digest(
        approval.signature, hmac.new(APPROVAL_KEY.encode(), approval_message(approval), hashlib.sha256).hexdigest()
    ):
        raise HTTPException(status_code=401, detail="invalid approval signature")
    if approval.expires_at < int(time.time()):
        raise HTTPException(status_code=401, detail="approval expired")
    if approval.package_id != package_id or approval.target != TARGET:
        raise HTTPException(status_code=403, detail="approval binding mismatch")
    with connect() as db:
        row = db.execute("SELECT * FROM packages WHERE package_id = ?", (package_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="package not found")
        if db.execute("SELECT 1 FROM consumed_approvals WHERE jti = ?", (approval.jti,)).fetchone():
            raise HTTPException(status_code=401, detail="approval replayed")
        package = json.loads(row["payload"])
        unsigned_package = {k: v for k, v in package.items() if k != "package_digest"}
        if (
            package.get("package_digest") != row["package_digest"]
            or digest(unsigned_package) != row["package_digest"]
        ):
            raise HTTPException(status_code=409, detail="stored package integrity check failed")
        current = load_active()
        if (approval.package_digest != row["package_digest"] or
                approval.base_digest != row["base_digest"] or digest(current) != row["base_digest"]):
            raise HTTPException(status_code=409, detail="package or base binding changed")
        db.execute("INSERT INTO consumed_approvals VALUES(?,?)", (
            approval.jti, datetime.now(timezone.utc).isoformat()))
    previous = current
    atomic_write(active_path(), package["candidate"])
    verified = False
    detail = {}
    try:
        validation = await validate_candidate(CandidateConfig.model_validate(package["candidate"]))
        async with httpx.AsyncClient(timeout=5) as client:
            replica = await client.get(f"{REPLICA_URL}/health")
        detail = {"validation": validation, "replica_status": replica.status_code,
                  "replica": replica.json()}
        verified = validation["passed"] and replica.status_code == 200
    finally:
        if not verified:
            atomic_write(active_path(), previous)
    with connect() as db:
        db.execute("UPDATE packages SET status = ? WHERE package_id = ?",
                   ("applied" if verified else "rolled_back", package_id))
        db.execute("INSERT INTO audit(event,package_id,detail,created_at) VALUES(?,?,?,?)", (
            "apply_verified" if verified else "apply_rolled_back", package_id,
            json.dumps(detail), datetime.now(timezone.utc).isoformat(),
        ))
    if not verified:
        raise HTTPException(status_code=409, detail={"message": "independent verification failed", **detail})
    return {"package_id": package_id, "status": "applied", "verified": True, **detail}


@app.post("/v1/reset")
async def reset(authorization: str | None = Header(default=None)):
    authorize(authorization)
    atomic_write(active_path(), BROKEN_CONFIG)
    return {"status": "reset", "base_digest": digest(BROKEN_CONFIG)}
