import asyncio
import logging
import os
import time

import aiomysql
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

SERVICE = os.getenv("SERVICE_NAME", "sample-service")
PORT = int(os.getenv("PORT", "8000"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s level=%(levelname)s service=" + SERVICE + " %(message)s")
log = logging.getLogger(SERVICE)
app = FastAPI(title=SERVICE)
app.mount("/metrics", make_asgi_app())

REQUESTS = Counter("http_requests_total", "Requests", ["service", "path", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Latency", ["service", "path"])
DEPENDENCY = Gauge("dependency_up", "Dependency health", ["service", "dependency"])


async def redis_ok() -> tuple[bool, str]:
    try:
        client = redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        await client.ping()
        await client.aclose()
        DEPENDENCY.labels(SERVICE, "redis").set(1)
        return True, "ok"
    except Exception as exc:
        DEPENDENCY.labels(SERVICE, "redis").set(0)
        log.error("redis dependency failed error=%s detail=%s", type(exc).__name__, exc)
        return False, str(exc)


async def mysql_ok() -> tuple[bool, str]:
    try:
        conn = await aiomysql.connect(
            host=os.getenv("MYSQL_HOST", "mysql"), port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "opspilot"), password=os.getenv("MYSQL_PASSWORD", "opspilot"),
            db=os.getenv("MYSQL_DATABASE", "opspilot"), connect_timeout=1,
        )
        conn.close()
        DEPENDENCY.labels(SERVICE, "mysql").set(1)
        return True, "ok"
    except Exception as exc:
        DEPENDENCY.labels(SERVICE, "mysql").set(0)
        log.error("mysql dependency failed error=%s detail=%s", type(exc).__name__, exc)
        return False, str(exc)


@app.get("/health")
async def health():
    started = time.monotonic()
    redis_result, mysql_result = await asyncio.gather(redis_ok(), mysql_ok())
    healthy = redis_result[0] and mysql_result[0]
    status = "200" if healthy else "503"
    REQUESTS.labels(SERVICE, "/health", status).inc()
    LATENCY.labels(SERVICE, "/health").observe(time.monotonic() - started)
    body = {"service": SERVICE, "status": "ok" if healthy else "degraded", "dependencies": {"redis": redis_result[0], "mysql": mysql_result[0]}}
    if not healthy:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.get("/work")
async def work(seconds: float = 0):
    seconds = min(max(seconds, 0), 30)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sum(i * i for i in range(20_000))
    return {"service": SERVICE, "cpu_seconds": seconds}
