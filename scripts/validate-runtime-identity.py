import asyncio

import httpx

from issuer_client import request_identity


ISSUER_URL = "http://workload-identity-issuer:8085"
RUNTIME_URL = "http://runtime-executor:2375"
PRIVATE_KEY = "/identity/gateway-private/private.pem"
PATH = "/v1/containers/redis/status"


async def main() -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        missing = await client.get(f"{RUNTIME_URL}{PATH}")
        raw = await client.get(f"{RUNTIME_URL}/containers/json")
        token = await request_identity(
            ISSUER_URL, PRIVATE_KEY, "executor-gateway",
            audience="opspilot-runtime-executor", ttl_seconds=10,
            method="GET", path=PATH, operation="container_status",
            target="redis", placement="local-compose",
        )
        headers = {"Authorization": f"Bearer {token}"}
        first = await client.get(f"{RUNTIME_URL}{PATH}", headers=headers)
        replay = await client.get(f"{RUNTIME_URL}{PATH}", headers=headers)
        try:
            await request_identity(
                ISSUER_URL, PRIVATE_KEY, "executor-gateway",
                audience="opspilot-runtime-executor", ttl_seconds=10,
                method="GET", path="/v1/containers/unknown/status",
                operation="container_status", target="unknown", placement="local-compose",
            )
        except httpx.HTTPStatusError as exc:
            unknown = exc.response.status_code
        else:
            unknown = 200
    observed = {
        "missing_identity": missing.status_code, "unknown_target": unknown,
        "first_use": first.status_code, "replay": replay.status_code,
        "raw_route": raw.status_code,
    }
    expected = {
        "missing_identity": 401, "unknown_target": 403,
        "first_use": 200, "replay": 401, "raw_route": 404,
    }
    if observed != expected:
        raise RuntimeError(f"runtime identity boundary mismatch: {observed}")
    print(observed)


asyncio.run(main())
