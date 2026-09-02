import asyncio

import httpx

from issuer_client import request_identity


ISSUER_URL = "http://workload-identity-issuer-orchestrated:8085"
PRIVATE_KEY = "/identity/gateway-private/private.pem"
PATH = "/v1/containers/redis/restart"
PLACEMENT = "kubernetes/opspilot/redis"


async def main() -> None:
    token = await request_identity(
        ISSUER_URL, PRIVATE_KEY, "executor-gateway",
        audience="opspilot-runtime-executor", ttl_seconds=10,
        method="POST", path=PATH, operation="restart_container",
        target="redis", placement=PLACEMENT,
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        first = await client.post(f"http://runtime-executor-redis-a:2375{PATH}", headers=headers)
        replay = await client.post(f"http://runtime-executor-redis-b:2375{PATH}", headers=headers)
        try:
            await request_identity(
                ISSUER_URL, PRIVATE_KEY, "executor-gateway",
                audience="opspilot-runtime-executor", ttl_seconds=10,
                method="POST", path=PATH, operation="restart_container",
                target="redis", placement="kubernetes/other/redis",
            )
        except httpx.HTTPStatusError as exc:
            wrong_placement_status = exc.response.status_code
        else:
            wrong_placement_status = 200
    if first.status_code != 200:
        raise RuntimeError(f"placed action failed: {first.status_code} {first.text}")
    if replay.status_code != 401 or "already been used" not in replay.text:
        raise RuntimeError(f"cross-executor replay was not rejected: {replay.status_code} {replay.text}")
    if wrong_placement_status != 403:
        raise RuntimeError(f"issuer accepted a wrong placement: {wrong_placement_status}")
    print({
        "first": first.status_code, "cross_executor_replay": replay.status_code,
        "wrong_placement": wrong_placement_status, "placement": PLACEMENT,
    })


asyncio.run(main())
