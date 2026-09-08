import json
import os

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse


CONFIG_FILE = os.getenv("REPAIR_CONFIG_FILE", "/lab/active/config.json")
app = FastAPI(title="OpsPilot repair fault replica")


@app.get("/health")
async def health():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            config = json.load(handle)
        client = redis.from_url(config["redis_url"], socket_connect_timeout=1, socket_timeout=1)
        await client.ping()
        await client.aclose()
        return {"service": "repair-lab-payment", "status": "ok", "config": config}
    except Exception as exc:
        return JSONResponse(status_code=503, content={
            "service": "repair-lab-payment", "status": "degraded",
            "error_type": type(exc).__name__,
        })
