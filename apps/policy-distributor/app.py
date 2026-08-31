import hmac
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response


app = FastAPI(title="OpsPilot Verification Policy Distributor", version="0.1.0")
policy_path = Path(os.getenv("VERIFICATION_POLICY_BUNDLE_FILE", "/policy/bundle.json"))
distribution_token = os.getenv("VERIFICATION_POLICY_DISTRIBUTION_TOKEN", "")
if not distribution_token:
    raise RuntimeError("VERIFICATION_POLICY_DISTRIBUTION_TOKEN must not be empty")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-policy-distributor"}


@app.get("/bundle")
async def bundle(authorization: str | None = Header(default=None)):
    expected = f"Bearer {distribution_token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Valid distribution identity is required")
    try:
        content = policy_path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Policy bundle unavailable: {exc}") from exc
    return Response(content=content, media_type="application/json")
