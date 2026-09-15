import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response


app = FastAPI(title="OpsPilot Verification Policy Distributor", version="0.1.0")
policy_path = Path(os.getenv("VERIFICATION_POLICY_BUNDLE_FILE", "/policy/bundle.json"))
try:
    configured_channels = json.loads(os.getenv("VERIFICATION_POLICY_BUNDLE_FILES", "{}"))
except json.JSONDecodeError as exc:
    raise RuntimeError("VERIFICATION_POLICY_BUNDLE_FILES must be valid JSON") from exc
if not isinstance(configured_channels, dict) or any(
    not isinstance(channel, str)
    or not channel
    or not isinstance(path, str)
    or not path
    for channel, path in configured_channels.items()
):
    raise RuntimeError("VERIFICATION_POLICY_BUNDLE_FILES must map channel names to paths")
policy_channels = {channel: Path(path) for channel, path in configured_channels.items()}
distribution_token = os.getenv("VERIFICATION_POLICY_DISTRIBUTION_TOKEN", "")
if not distribution_token:
    raise RuntimeError("VERIFICATION_POLICY_DISTRIBUTION_TOKEN must not be empty")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-policy-distributor"}


def read_bundle(path: Path, authorization: str | None) -> Response:
    expected = f"Bearer {distribution_token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Valid distribution identity is required")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Policy bundle unavailable: {exc}") from exc
    return Response(content=content, media_type="application/json")


@app.get("/bundle")
async def bundle(authorization: str | None = Header(default=None)):
    return read_bundle(policy_path, authorization)


@app.get("/bundle/{channel}")
async def channel_bundle(channel: str, authorization: str | None = Header(default=None)):
    path = policy_channels.get(channel)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown policy bundle channel")
    return read_bundle(path, authorization)
