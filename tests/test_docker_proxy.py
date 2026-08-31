import importlib.util
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient


class FakeContainer:
    status = "running"

    def __init__(self):
        self.restarted = False
        self.stopped = False

    def restart(self, timeout):
        self.restarted = True

    def stop(self, timeout):
        self.stopped = True


def load_proxy(monkeypatch):
    monkeypatch.setenv("DOCKER_PROXY_TOKEN", "test-proxy-token")
    fake_docker = types.SimpleNamespace(DockerClient=lambda **kwargs: None)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    path = Path("/app/docker-proxy/app.py")
    if not path.exists():
        path = Path("apps/docker-proxy/app.py")
    spec = importlib.util.spec_from_file_location("docker_proxy_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


def auth():
    return {"Authorization": "Bearer test-proxy-token"}


def test_proxy_requires_identity_and_exposes_no_raw_docker_api(monkeypatch):
    _, client = load_proxy(monkeypatch)
    assert client.get("/v1/containers/redis/status").status_code == 401
    assert client.get("/containers/json", headers=auth()).status_code == 404
    assert client.delete("/v1/containers/redis", headers=auth()).status_code == 404


def test_proxy_allows_only_fixed_operations_and_targets(monkeypatch):
    module, client = load_proxy(monkeypatch)
    container = FakeContainer()
    monkeypatch.setattr(module, "container_for", lambda target: container)

    status = client.get("/v1/containers/redis/status", headers=auth())
    restarted = client.post("/v1/containers/redis/restart", headers=auth())
    stopped = client.post("/v1/containers/redis/stop", headers=auth())

    assert status.json()["status"] == "running"
    assert restarted.status_code == 200 and container.restarted
    assert stopped.status_code == 200 and container.stopped
    assert client.post("/v1/containers/prometheus/restart", headers=auth()).status_code == 403
    assert client.post("/v1/containers/payment-service/stop", headers=auth()).status_code == 403
    assert client.post("/v1/containers/unknown/restart", headers=auth()).status_code == 403
