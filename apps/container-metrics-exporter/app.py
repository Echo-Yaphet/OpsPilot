import asyncio
import os

import httpx
from fastapi import FastAPI, Response


DOCKER_PROXY_URL = os.getenv("DOCKER_PROXY_URL", "http://docker-proxy:2375").rstrip("/")
DOCKER_PROXY_TOKEN = os.getenv("DOCKER_PROXY_TOKEN", "")
TARGETS = tuple(filter(None, (
    item.strip() for item in os.getenv(
        "CONTAINER_METRICS_TARGETS", "user-service,order-service,payment-service"
    ).split(",")
)))


def cpu_usage_ratio(stats: dict) -> float:
    cpu_delta = stats["cpu_total_usage"] - stats["previous_cpu_total_usage"]
    system_delta = stats["system_cpu_usage"] - stats["previous_system_cpu_usage"]
    if cpu_delta <= 0 or system_delta <= 0:
        return 0.0
    return cpu_delta / system_delta * max(int(stats.get("online_cpus", 1)), 1)


async def collect_target_stats(target: str) -> dict:
    headers = {"Authorization": f"Bearer {DOCKER_PROXY_TOKEN}"}
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.get(
            f"{DOCKER_PROXY_URL}/v1/containers/{target}/stats", headers=headers
        )
    response.raise_for_status()
    return response.json()


app = FastAPI(title="OpsPilot Container Metrics Exporter", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-container-metrics-exporter"}


@app.get("/metrics")
async def metrics():
    results = await asyncio.gather(
        *(collect_target_stats(target) for target in TARGETS), return_exceptions=True
    )
    lines = [
        "# HELP container_cpu_usage_ratio Container CPU usage in host CPU cores.",
        "# TYPE container_cpu_usage_ratio gauge",
        "# HELP container_cpu_metrics_up Whether container CPU stats were collected.",
        "# TYPE container_cpu_metrics_up gauge",
    ]
    for target, result in zip(TARGETS, results):
        if isinstance(result, Exception):
            lines.append(f'container_cpu_metrics_up{{service="{target}"}} 0')
            continue
        lines.append(
            f'container_cpu_usage_ratio{{service="{target}"}} {cpu_usage_ratio(result):.6f}'
        )
        lines.append(f'container_cpu_metrics_up{{service="{target}"}} 1')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
