from pathlib import Path

import httpx

from workload_identity import sign_issuer_request


async def request_identity(
    issuer_url: str, private_key_file: str, subject: str, *, audience: str,
    ttl_seconds: int, method: str, path: str, operation: str, target: str,
    placement: str | None = None,
    timeout: float = 5,
) -> str:
    payload = {
        "audience": audience, "ttl_seconds": ttl_seconds, "method": method.upper(),
        "path": path, "operation": operation, "target": target,
    }
    if placement is not None:
        payload["placement"] = placement
    headers = sign_issuer_request(Path(private_key_file).read_bytes(), subject, payload)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{issuer_url.rstrip('/')}/v1/identity", json=payload, headers=headers)
    response.raise_for_status()
    return response.json()["token"]
