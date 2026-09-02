import asyncio
import json
import math
import os
import re
import time

import httpx
from fastapi import FastAPI, Response
from issuer_client import request_identity


RUNTIME_EXECUTOR_URL = os.getenv("RUNTIME_EXECUTOR_URL", "http://runtime-executor:2375").rstrip("/")
ISSUER_URL = os.getenv("WORKLOAD_IDENTITY_ISSUER_URL", "http://workload-identity-issuer:8085")
WORKLOAD_PRIVATE_KEY_FILE = os.getenv("WORKLOAD_IDENTITY_PRIVATE_KEY_FILE", "/identity/metrics-private/private.pem")
RUNTIME_AUDIENCE = os.getenv("RUNTIME_EXECUTOR_IDENTITY_AUDIENCE", "opspilot-runtime-executor")


def load_runtime_placements() -> dict[str, dict[str, str]]:
    raw = os.getenv("RUNTIME_EXECUTOR_PLACEMENTS", "")
    if not raw:
        return {}
    try:
        placements = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RUNTIME_EXECUTOR_PLACEMENTS must be valid JSON") from exc
    if not isinstance(placements, dict):
        raise ValueError("RUNTIME_EXECUTOR_PLACEMENTS must be a JSON object")
    validated = {}
    for target, route in placements.items():
        if not isinstance(route, dict) or set(route) != {"url", "placement"}:
            raise ValueError(f"runtime placement for {target} must contain url and placement")
        if not all(isinstance(route[key], str) and route[key] for key in ("url", "placement")):
            raise ValueError(f"runtime placement for {target} is invalid")
        validated[target] = route
    return validated


RUNTIME_PLACEMENTS = load_runtime_placements()


def runtime_route(target: str) -> tuple[str, str]:
    route = RUNTIME_PLACEMENTS.get(target)
    if route:
        return route["url"].rstrip("/"), route["placement"]
    return RUNTIME_EXECUTOR_URL, "local-compose"


def load_targets() -> tuple[str, ...]:
    targets = tuple(filter(None, (
        item.strip() for item in os.getenv(
            "CONTAINER_METRICS_TARGETS", "user-service,order-service,payment-service"
        ).split(",")
    )))
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("CONTAINER_METRICS_TARGETS must contain unique service names")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", target) for target in targets):
        raise ValueError("CONTAINER_METRICS_TARGETS contains an invalid service name")
    return targets


def load_cpu_thresholds(targets: tuple[str, ...]) -> dict[str, float]:
    raw = os.getenv(
        "CONTAINER_CPU_THRESHOLDS",
        '{"user-service":0.8,"order-service":0.8,"payment-service":0.8}',
    )
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CONTAINER_CPU_THRESHOLDS must be valid JSON") from exc
    if not isinstance(configured, dict) or set(configured) != set(targets):
        raise ValueError("CONTAINER_CPU_THRESHOLDS must configure every metrics target exactly once")
    thresholds = {}
    for target, value in configured.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"CPU threshold for {target} must be numeric")
        value = float(value)
        if not math.isfinite(value) or value <= 0 or value > 1024:
            raise ValueError(f"CPU threshold for {target} must be in (0, 1024]")
        thresholds[target] = value
    return thresholds


TARGETS = load_targets()
if os.getenv("RUNTIME_EXECUTOR_PLACEMENTS_REQUIRED", "false").lower() == "true":
    if set(RUNTIME_PLACEMENTS) != set(TARGETS):
        raise ValueError("RUNTIME_EXECUTOR_PLACEMENTS must configure every metrics target")
CPU_THRESHOLDS = load_cpu_thresholds(TARGETS)
LAST_SUCCESS_TIMESTAMPS = {target: 0.0 for target in TARGETS}


def cpu_usage_ratio(stats: dict) -> float:
    cpu_delta = stats["cpu_total_usage"] - stats["previous_cpu_total_usage"]
    system_delta = stats["system_cpu_usage"] - stats["previous_system_cpu_usage"]
    if cpu_delta <= 0 or system_delta <= 0:
        return 0.0
    return cpu_delta / system_delta * max(int(stats.get("online_cpus", 1)), 1)


async def collect_target_stats(target: str) -> dict:
    path = f"/v1/containers/{target}/stats"
    runtime_url, placement = runtime_route(target)
    credential = await request_identity(
        ISSUER_URL, WORKLOAD_PRIVATE_KEY_FILE, "container-metrics-exporter",
        audience=RUNTIME_AUDIENCE, ttl_seconds=10, method="GET", path=path,
        operation="container_stats", target=target,
        placement=placement,
    )
    headers = {"Authorization": f"Bearer {credential}"}
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.get(
            f"{runtime_url}{path}", headers=headers
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
        "# HELP container_cpu_metrics_last_success_timestamp_seconds Unix time of the last successful stats collection.",
        "# TYPE container_cpu_metrics_last_success_timestamp_seconds gauge",
        "# HELP container_cpu_alert_threshold_ratio Configured per-service CPU alert threshold in host CPU cores.",
        "# TYPE container_cpu_alert_threshold_ratio gauge",
    ]
    for target, result in zip(TARGETS, results):
        if isinstance(result, Exception):
            lines.append(f'container_cpu_metrics_up{{service="{target}"}} 0')
        else:
            LAST_SUCCESS_TIMESTAMPS[target] = time.time()
            lines.append(
                f'container_cpu_usage_ratio{{service="{target}"}} {cpu_usage_ratio(result):.6f}'
            )
            lines.append(f'container_cpu_metrics_up{{service="{target}"}} 1')
        lines.append(
            f'container_cpu_metrics_last_success_timestamp_seconds{{service="{target}"}} '
            f'{LAST_SUCCESS_TIMESTAMPS.get(target, 0.0):.3f}'
        )
        lines.append(
            f'container_cpu_alert_threshold_ratio{{service="{target}"}} '
            f'{CPU_THRESHOLDS[target]:.6f}'
        )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
