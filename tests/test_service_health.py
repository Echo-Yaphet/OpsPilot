import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException


def load_service(monkeypatch):
    class Metric:
        def __init__(self, *_args, **_kwargs):
            pass

        def labels(self, *_args, **_kwargs):
            return self

        def set(self, *_args, **_kwargs):
            pass

        def inc(self, *_args, **_kwargs):
            pass

        def observe(self, *_args, **_kwargs):
            pass

    monkeypatch.setitem(sys.modules, "aiomysql", SimpleNamespace(connect=None))
    monkeypatch.setitem(
        sys.modules,
        "prometheus_client",
        SimpleNamespace(
            Counter=Metric, Gauge=Metric, Histogram=Metric,
            make_asgi_app=lambda: SimpleNamespace(),
        ),
    )
    path = Path("/app/shared-service/app.py")
    if not path.exists():
        path = Path("apps/shared-service/app.py")
    spec = importlib.util.spec_from_file_location("shared_service_health", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_marks_mysql_down_when_handshake_never_responds(monkeypatch):
    module = load_service(monkeypatch)

    async def redis_healthy():
        return True, "ok"

    async def stalled_mysql_handshake(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "redis_ok", redis_healthy)
    monkeypatch.setattr(module.aiomysql, "connect", stalled_mysql_handshake)

    async def request_health():
        try:
            await asyncio.wait_for(module.health(), timeout=2)
        except HTTPException as exc:
            return exc

    response = asyncio.run(request_health())

    assert response.status_code == 503
    assert response.detail["dependencies"] == {"redis": True, "mysql": False}
