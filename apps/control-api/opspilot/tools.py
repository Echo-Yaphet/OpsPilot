from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx
from workload_identity import mint_identity

from .config import Settings


class OpsTools(ABC):
    """The tool seam consumed by the workflow and its tests."""

    @abstractmethod
    async def query_metric(self, query: str) -> list[dict]: ...

    @abstractmethod
    async def query_logs(self, service: str, minutes: int = 10, limit: int = 100) -> list[str]: ...

    @abstractmethod
    async def container_status(self, service: str) -> str: ...

    @abstractmethod
    async def service_health(self, service: str) -> dict: ...

    @abstractmethod
    async def restart_container(self, service: str) -> str: ...

    @abstractmethod
    async def stop_container(self, service: str) -> str: ...


class LiveOpsTools(OpsTools):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def query_metric(self, query: str) -> list[dict]:
        return await self.query_metric_at(query)

    async def query_metric_at(self, query: str, at: datetime | None = None) -> list[dict]:
        params = {"query": query}
        if at is not None:
            params["time"] = str(at.timestamp())
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.settings.prometheus_url}/api/v1/query", params=params)
            response.raise_for_status()
            payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error", "Prometheus query failed"))
        return payload["data"]["result"]

    async def query_logs(self, service: str, minutes: int = 10, limit: int = 100) -> list[str]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        return await self.query_logs_between(service, start, end, limit)

    async def query_logs_between(
        self, service: str, start: datetime, end: datetime, limit: int = 100
    ) -> list[str]:
        params = {
            "query": f'{{compose_service="{service}"}} |~ "(?i)(error|failed|refused|timeout)"',
            "start": str(int(start.timestamp() * 1e9)), "end": str(int(end.timestamp() * 1e9)),
            "limit": limit, "direction": "backward",
        }
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.settings.loki_url}/loki/api/v1/query_range", params=params)
            response.raise_for_status()
            streams = response.json().get("data", {}).get("result", [])
        return [line for stream in streams for _, line in stream.get("values", [])]

    async def _gateway(self, method: str, path: str, json: dict | None = None) -> dict:
        operation = json["operation"] if json else "container_status"
        target = json["target"] if json else path.removeprefix("/v1/containers/").removesuffix("/status")
        credential = mint_identity(
            self.settings.executor_identity_key,
            issuer=self.settings.executor_identity_issuer,
            audience=self.settings.executor_identity_audience,
            subject=self.settings.executor_identity_subject,
            ttl_seconds=self.settings.executor_identity_ttl_seconds,
            method=method,
            path=path,
            operation=operation,
            target=target,
            key_id=self.settings.executor_identity_key_id,
        )
        headers = {"Authorization": f"Bearer {credential}"}
        async with httpx.AsyncClient(timeout=self.settings.executor_gateway_timeout) as client:
            response = await client.request(
                method, f"{self.settings.executor_gateway_url.rstrip('/')}{path}", json=json, headers=headers,
            )
        response.raise_for_status()
        return response.json()

    async def container_status(self, service: str) -> str:
        return (await self._gateway("GET", f"/v1/containers/{service}/status"))["status"]

    async def service_health(self, service: str) -> dict:
        ports = {"user-service": 8001, "order-service": 8002, "payment-service": 8003}
        if service not in ports:
            raise RuntimeError(f"service health endpoint not configured: {service}")
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"http://{service}:{ports[service]}/health")
        payload = response.json()
        if response.status_code != 200:
            detail = payload.get("detail", payload)
            return {"healthy": False, "detail": detail}
        return {"healthy": payload.get("status") == "ok", "detail": payload}

    async def restart_container(self, service: str) -> str:
        return (await self._gateway(
            "POST", "/v1/actions", {"operation": "restart_container", "target": service},
        ))["result"]

    async def stop_container(self, service: str) -> str:
        return (await self._gateway(
            "POST", "/v1/actions", {"operation": "stop_container", "target": service},
        ))["result"]
