import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient


def load_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("EXECUTOR_DATABASE_PATH", str(tmp_path / "executor.db"))
    fake_docker = types.SimpleNamespace(DockerClient=lambda **kwargs: None)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    path = Path("/app/executor-gateway/app.py")
    if not path.exists():
        path = Path("apps/executor-gateway/app.py")
    spec = importlib.util.spec_from_file_location("executor_gateway_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


class FakeContainer:
    status = "running"

    def restart(self, timeout):
        return None

    def stop(self, timeout):
        return None


def test_gateway_requires_identity_and_rejects_unknown_target(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    unauthorized = client.post("/v1/actions", json={
        "operation": "restart_container", "target": "redis",
    })
    denied = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "unknown-service"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert unauthorized.status_code == 401
    assert denied.status_code == 403
    with sqlite3.connect(module.DATABASE_PATH) as db:
        row = db.execute("SELECT outcome, target FROM execution_audit").fetchone()
    assert row == ("denied", "unknown-service")


def test_gateway_executes_typed_allowlisted_action_and_audits(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "container_for", lambda target: FakeContainer())
    response = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "redis"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == "restarted redis"
    with sqlite3.connect(module.DATABASE_PATH) as db:
        row = db.execute("SELECT operation, target, outcome FROM execution_audit").fetchone()
    assert row == ("restart_container", "redis", "allowed")
