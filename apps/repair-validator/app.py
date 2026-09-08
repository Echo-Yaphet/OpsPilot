import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import redis.asyncio as redis
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict


TOKEN = os.getenv("REPAIR_VALIDATOR_TOKEN", "")
ALLOWED_REDIS_HOST = os.getenv("REPAIR_ALLOWED_REDIS_HOST", "repair-lab-redis")
ALLOWED_SERVICE_VERSION = os.getenv("REPAIR_ALLOWED_SERVICE_VERSION", "payment-lab-v1")
app = FastAPI(title="OpsPilot repair validator")


class CandidateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_version: str
    redis_url: str


def canonical(config: CandidateConfig) -> bytes:
    return json.dumps(config.model_dump(), sort_keys=True, separators=(",", ":")).encode()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/validate")
async def validate(config: CandidateConfig, authorization: str | None = Header(default=None)):
    if not TOKEN or authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="validator identity required")
    parsed = urlparse(config.redis_url)
    endpoint_allowed = (
        parsed.scheme == "redis" and parsed.hostname == ALLOWED_REDIS_HOST
        and parsed.port == 6379 and parsed.path in ("", "/0")
        and not parsed.username and not parsed.password and not parsed.query
    )
    version_allowed = config.service_version == ALLOWED_SERVICE_VERSION
    allowed = endpoint_allowed and version_allowed
    observations = [
        {"probe": "config_scope", "passed": endpoint_allowed},
        {"probe": "service_version", "passed": version_allowed},
    ]
    ping_ok = False
    error_type = None
    if allowed:
        try:
            client = redis.from_url(config.redis_url, socket_connect_timeout=1, socket_timeout=1)
            ping_ok = bool(await client.ping())
            await client.aclose()
        except Exception as exc:
            error_type = type(exc).__name__
    observations.append({"probe": "redis_ping", "passed": ping_ok, "error_type": error_type})
    return {
        "passed": allowed and ping_ok,
        "config_digest": "sha256:" + hashlib.sha256(canonical(config)).hexdigest(),
        "probe_version": "redis-config-probe-v1",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "observations": observations,
    }
