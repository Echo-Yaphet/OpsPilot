import asyncio
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

    def stats(self, stream):
        assert stream is False
        return {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 300},
                "system_cpu_usage": 2000,
                "online_cpus": 4,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 1000,
            },
        }


class FakeDiscoveredContainer:
    def __init__(self, container_id, name):
        self.id = container_id
        self.name = name


class FakeContainerCollection:
    def __init__(self, services):
        self.services = services
        self.filters = []

    def list(self, *, all, filters):
        assert all is True
        self.filters.append(filters)
        labels = filters["label"]
        service_label = next(label for label in labels if label.startswith("com.docker.compose.service="))
        return self.services.get(service_label.split("=", 1)[1], [])


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
    stats = client.get("/v1/containers/payment-service/stats", headers=auth())
    restarted = client.post("/v1/containers/redis/restart", headers=auth())
    stopped = client.post("/v1/containers/redis/stop", headers=auth())

    assert status.json()["status"] == "running"
    assert stats.json() == {
        "target": "payment-service",
        "cpu_total_usage": 300,
        "previous_cpu_total_usage": 100,
        "system_cpu_usage": 2000,
        "previous_system_cpu_usage": 1000,
        "online_cpus": 4,
    }
    assert restarted.status_code == 200 and container.restarted
    assert stopped.status_code == 200 and container.stopped
    assert client.post("/v1/containers/prometheus/restart", headers=auth()).status_code == 403
    assert client.post("/v1/containers/payment-service/stop", headers=auth()).status_code == 403
    assert client.post("/v1/containers/unknown/restart", headers=auth()).status_code == 403
    assert client.get("/v1/containers/redis/stats", headers=auth()).status_code == 403


def test_proxy_discovers_only_allowlisted_project_log_targets(monkeypatch):
    module, _ = load_proxy(monkeypatch)
    containers = FakeContainerCollection({
        "payment-service": [FakeDiscoveredContainer("payment-id", "opspilot-payment-service-1")],
        "user-service": [FakeDiscoveredContainer("user-id", "opspilot-user-service-1")],
    })
    monkeypatch.setattr(
        module.docker,
        "DockerClient",
        lambda **kwargs: types.SimpleNamespace(containers=containers),
    )

    targets = module.discover_log_targets()

    assert targets == [
        {
            "targets": ["localhost"],
            "labels": {
                "__path__": "/var/lib/docker/containers/payment-id/payment-id-json.log",
                "compose_service": "payment-service",
                "container": "/opspilot-payment-service-1",
            },
        },
        {
            "targets": ["localhost"],
            "labels": {
                "__path__": "/var/lib/docker/containers/user-id/user-id-json.log",
                "compose_service": "user-service",
                "container": "/opspilot-user-service-1",
            },
        },
    ]
    assert all(
        "com.docker.compose.project=opspilot" in request["label"]
        for request in containers.filters
    )
    assert {label.split("=", 1)[1] for request in containers.filters for label in request["label"]
            if label.startswith("com.docker.compose.service=")} == module.LOG_TARGETS


def test_proxy_publishes_promtail_targets_atomically(tmp_path, monkeypatch):
    module, _ = load_proxy(monkeypatch)
    destination = tmp_path / "targets.json"
    targets = [{"targets": ["localhost"], "labels": {"compose_service": "payment-service"}}]

    module.publish_log_targets(str(destination), targets)

    assert destination.read_text(encoding="utf-8") == (
        '[{"targets":["localhost"],"labels":{"compose_service":"payment-service"}}]'
    )
    assert not (tmp_path / "targets.json.tmp").exists()


def test_proxy_exposes_successful_log_target_publication_metrics(tmp_path, monkeypatch):
    module, client = load_proxy(monkeypatch)
    destination = tmp_path / "targets.json"
    targets = [{"targets": ["localhost"], "labels": {"compose_service": "payment-service"}}]
    monkeypatch.setattr(module, "LOG_DISCOVERY_FILE", str(destination))
    monkeypatch.setattr(module, "discover_log_targets", lambda: targets)
    monkeypatch.setattr(module.time, "time", lambda: 456.0)

    asyncio.run(module.refresh_log_targets_once())
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "docker_proxy_log_target_publication_up 1" in response.text
    assert "docker_proxy_log_target_publication_last_success_timestamp_seconds 456.000" in response.text
    assert "docker_proxy_log_targets 1" in response.text
    assert "docker_proxy_log_target_publication_failures_total 0" in response.text


def test_proxy_reports_failed_publication_and_retains_last_known_good(tmp_path, monkeypatch):
    module, client = load_proxy(monkeypatch)
    destination = tmp_path / "targets.json"
    destination.write_text('[{"last":"known-good"}]', encoding="utf-8")
    monkeypatch.setattr(module, "LOG_DISCOVERY_FILE", str(destination))
    monkeypatch.setattr(module, "discover_log_targets", lambda: [])
    module.LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP = 123.0

    asyncio.run(module.refresh_log_targets_once())
    response = client.get("/metrics")

    assert destination.read_text(encoding="utf-8") == '[{"last":"known-good"}]'
    assert "docker_proxy_log_target_publication_up 0" in response.text
    assert "docker_proxy_log_target_publication_last_success_timestamp_seconds 123.000" in response.text
    assert "docker_proxy_log_targets 0" in response.text
    assert "docker_proxy_log_target_publication_failures_total 1" in response.text


def test_proxy_restores_last_known_good_timestamp_after_restart(tmp_path, monkeypatch):
    destination = tmp_path / "targets.json"
    destination.write_text('[{"last":"known-good"}]', encoding="utf-8")
    destination.touch()
    monkeypatch.setenv("DOCKER_PROXY_LOG_DISCOVERY_FILE", str(destination))

    module, _ = load_proxy(monkeypatch)

    assert module.LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP == destination.stat().st_mtime
