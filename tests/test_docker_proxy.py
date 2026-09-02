import asyncio
import importlib.util
import sys
import types
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from workload_identity import mint_external_identity


ISSUER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER_PRIVATE = ISSUER_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
ISSUER_PUBLIC = ISSUER_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


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


def load_proxy(monkeypatch, log_targets=None):
    public_path = Path(tempfile.gettempdir()) / "opspilot-test-issuer-public.pem"
    public_path.write_bytes(ISSUER_PUBLIC)
    database_path = Path(tempfile.gettempdir()) / f"opspilot-proxy-{id(monkeypatch)}.db"
    database_path.unlink(missing_ok=True)
    monkeypatch.setenv("WORKLOAD_IDENTITY_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.setenv("DOCKER_PROXY_IDENTITY_DATABASE_PATH", str(database_path))
    if log_targets is None:
        monkeypatch.delenv("DOCKER_PROXY_LOG_TARGETS", raising=False)
    else:
        monkeypatch.setenv("DOCKER_PROXY_LOG_TARGETS", log_targets)
    fake_docker = types.SimpleNamespace(DockerClient=lambda **kwargs: None)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    path = Path("/app/docker-proxy/app.py")
    if not path.exists():
        path = Path("apps/docker-proxy/app.py")
    spec = importlib.util.spec_from_file_location("docker_proxy_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


def auth(method="GET", path="/containers/json", operation="container_status", target="redis", subject="executor-gateway"):
    token = mint_external_identity(
        ISSUER_PRIVATE, key_id="opspilot-issuer-v1",
        issuer="opspilot-workload-identity-issuer", audience="opspilot-docker-proxy",
        subject=subject, ttl_seconds=10, method=method, path=path,
        operation=operation, target=target,
    )
    return {"Authorization": f"Bearer {token}"}


def test_proxy_requires_identity_and_exposes_no_raw_docker_api(monkeypatch):
    _, client = load_proxy(monkeypatch)
    assert client.get("/v1/containers/redis/status").status_code == 401
    assert client.get("/containers/json", headers=auth()).status_code == 404
    assert client.delete("/v1/containers/redis", headers=auth()).status_code == 404


def test_proxy_allows_only_fixed_operations_and_targets(monkeypatch):
    module, client = load_proxy(monkeypatch)
    container = FakeContainer()
    monkeypatch.setattr(module, "container_for", lambda target: container)

    status = client.get("/v1/containers/redis/status", headers=auth(path="/v1/containers/redis/status"))
    stats = client.get("/v1/containers/payment-service/stats", headers=auth(
        path="/v1/containers/payment-service/stats", operation="container_stats",
        target="payment-service", subject="container-metrics-exporter",
    ))
    restarted = client.post("/v1/containers/redis/restart", headers=auth(
        method="POST", path="/v1/containers/redis/restart", operation="restart_container",
    ))
    stopped = client.post("/v1/containers/redis/stop", headers=auth(
        method="POST", path="/v1/containers/redis/stop", operation="stop_container",
    ))

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
    assert client.post("/v1/containers/prometheus/restart", headers=auth(method="POST", path="/v1/containers/prometheus/restart", operation="restart_container", target="prometheus")).status_code == 403
    assert client.post("/v1/containers/payment-service/stop", headers=auth(method="POST", path="/v1/containers/payment-service/stop", operation="stop_container", target="payment-service")).status_code == 403
    assert client.post("/v1/containers/unknown/restart", headers=auth(method="POST", path="/v1/containers/unknown/restart", operation="restart_container", target="unknown")).status_code == 403
    assert client.get("/v1/containers/redis/stats", headers=auth(path="/v1/containers/redis/stats", operation="container_stats", target="redis", subject="container-metrics-exporter")).status_code == 403


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


def test_proxy_supports_incremental_runtime_log_forwarding(monkeypatch):
    module, _ = load_proxy(monkeypatch, "user-service,order-service")

    containers = FakeContainerCollection({
        "payment-service": [FakeDiscoveredContainer("payment-id", "opspilot-payment-service-1")],
        "order-service": [FakeDiscoveredContainer("order-id", "opspilot-order-service-1")],
        "user-service": [FakeDiscoveredContainer("user-id", "opspilot-user-service-1")],
    })
    monkeypatch.setattr(
        module.docker,
        "DockerClient",
        lambda **kwargs: types.SimpleNamespace(containers=containers),
    )

    targets = module.discover_log_targets()

    assert module.LOG_TARGETS == frozenset({"user-service", "order-service"})
    assert {target["labels"]["compose_service"] for target in targets} == {
        "user-service", "order-service",
    }
    assert all(
        "com.docker.compose.service=payment-service" not in request["label"]
        for request in containers.filters
    )


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
    log_path = "/var/lib/docker/containers/payment-id/payment-id-json.log"
    targets = [{
        "targets": ["localhost"],
        "labels": {"compose_service": "payment-service", "__path__": log_path},
    }]
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
    assert (
        'docker_proxy_log_target_info{service="payment-service",'
        f'path="{log_path}"}} 1'
    ) in response.text


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


def test_proxy_retains_last_known_good_log_target_info_after_refresh_failure(tmp_path, monkeypatch):
    destination = tmp_path / "targets.json"
    log_path = "/var/lib/docker/containers/payment-id/payment-id-json.log"
    destination.write_text(
        '[{"targets":["localhost"],"labels":{"__path__":"'
        f'{log_path}","compose_service":"payment-service"}}' + '}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_PROXY_LOG_DISCOVERY_FILE", str(destination))
    module, client = load_proxy(monkeypatch)
    monkeypatch.setattr(module, "discover_log_targets", lambda: [])

    asyncio.run(module.refresh_log_targets_once())
    response = client.get("/metrics")

    assert (
        'docker_proxy_log_target_info{service="payment-service",'
        f'path="{log_path}"}} 1'
    ) in response.text


def test_proxy_restores_last_known_good_timestamp_after_restart(tmp_path, monkeypatch):
    destination = tmp_path / "targets.json"
    destination.write_text('[{"last":"known-good"}]', encoding="utf-8")
    destination.touch()
    monkeypatch.setenv("DOCKER_PROXY_LOG_DISCOVERY_FILE", str(destination))

    module, _ = load_proxy(monkeypatch)

    assert module.LOG_TARGET_PUBLICATION_LAST_SUCCESS_TIMESTAMP == destination.stat().st_mtime
