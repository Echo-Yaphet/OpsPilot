import importlib.util
import sqlite3
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from workload_identity import mint_external_identity


ISSUER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER_PRIVATE = ISSUER_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
ISSUER_PUBLIC = ISSUER_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


def load_executor(monkeypatch):
    public_path = Path(tempfile.gettempdir()) / "opspilot-runtime-test-public.pem"
    public_path.write_bytes(ISSUER_PUBLIC)
    database_path = Path(tempfile.gettempdir()) / f"opspilot-runtime-{id(monkeypatch)}.db"
    database_path.unlink(missing_ok=True)
    monkeypatch.setenv("WORKLOAD_IDENTITY_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.setenv("RUNTIME_EXECUTOR_DATABASE_PATH", str(database_path))
    path = Path("/app/runtime-executor/app.py")
    if not path.exists():
        path = Path("apps/runtime-executor/app.py")
    spec = importlib.util.spec_from_file_location("runtime_executor_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


def auth(
    *, method="GET", path="/v1/containers/redis/status", operation="container_status",
    target="redis", subject="executor-gateway", credential_id=None,
):
    token = mint_external_identity(
        ISSUER_PRIVATE, key_id="opspilot-issuer-v1",
        issuer="opspilot-workload-identity-issuer", audience="opspilot-runtime-executor",
        subject=subject, ttl_seconds=10, method=method, path=path,
        operation=operation, target=target, placement="local-compose",
        credential_id=credential_id,
    )
    return {"Authorization": f"Bearer {token}"}


async def fake_actuator(target, method, path):
    if path == "/v1/status":
        return {"target": target, "status": "running"}
    if path == "/v1/stats":
        return {
            "target": target, "cpu_total_usage": 300,
            "previous_cpu_total_usage": 100, "system_cpu_usage": 2000,
            "previous_system_cpu_usage": 1000, "online_cpus": 4,
        }
    return {"status": "completed", "result": f"acted on {target}"}


def test_runtime_executor_requires_identity_and_exposes_no_raw_docker_api(monkeypatch):
    _, client = load_executor(monkeypatch)
    assert client.get("/v1/containers/redis/status").status_code == 401
    assert client.get("/containers/json", headers=auth()).status_code == 404
    assert client.delete("/v1/containers/redis", headers=auth()).status_code == 404


def test_runtime_executor_allows_only_fixed_operations_targets_and_subjects(monkeypatch):
    module, client = load_executor(monkeypatch)
    monkeypatch.setattr(module, "actuator_request", fake_actuator)
    status = client.get("/v1/containers/redis/status", headers=auth())
    stats_path = "/v1/containers/payment-service/stats"
    stats = client.get(stats_path, headers=auth(
        path=stats_path, operation="container_stats", target="payment-service",
        subject="container-metrics-exporter",
    ))
    restart_path = "/v1/containers/redis/restart"
    restarted = client.post(restart_path, headers=auth(
        method="POST", path=restart_path, operation="restart_container",
    ))
    denied_path = "/v1/containers/prometheus/restart"
    denied = client.post(denied_path, headers=auth(
        method="POST", path=denied_path, operation="restart_container", target="prometheus",
    ))
    wrong_subject = client.get(stats_path, headers=auth(
        path=stats_path, operation="container_stats", target="payment-service",
    ))

    assert status.json() == {"target": "redis", "status": "running"}
    assert stats.json()["cpu_total_usage"] == 300
    assert restarted.status_code == 200
    assert denied.status_code == 403
    assert wrong_subject.status_code == 401


def test_runtime_executor_persists_audit_and_replay_denial(monkeypatch):
    module, client = load_executor(monkeypatch)
    monkeypatch.setattr(module, "actuator_request", fake_actuator)
    path = "/v1/containers/redis/restart"
    headers = auth(
        method="POST", path=path, operation="restart_container",
        credential_id="runtime-one-time",
    )
    assert client.post(path, headers=headers).status_code == 200
    replay = client.post(path, headers=headers)
    assert replay.status_code == 401
    assert "already been used" in replay.json()["detail"]
    with sqlite3.connect(module.DATABASE_PATH) as db:
        audit = db.execute(
            "SELECT operation,target,outcome,identity_subject,credential_id,identity_key_id "
            "FROM runtime_audit"
        ).fetchone()
        consumed = db.execute("SELECT identity_subject FROM consumed_credentials").fetchone()
    assert audit == (
        "restart_container", "redis", "allowed", "executor-gateway",
        "runtime-one-time", "opspilot-issuer-v1",
    )
    assert consumed == ("executor-gateway",)


def test_runtime_executor_rejects_wrong_placement_before_actuator_access(monkeypatch):
    module, client = load_executor(monkeypatch)
    called = False

    async def must_not_run(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(module, "actuator_request", must_not_run)
    path = "/v1/containers/redis/restart"
    token = mint_external_identity(
        ISSUER_PRIVATE, key_id="opspilot-issuer-v1",
        issuer="opspilot-workload-identity-issuer", audience="opspilot-runtime-executor",
        subject="executor-gateway", ttl_seconds=10, method="POST", path=path,
        operation="restart_container", target="redis", placement="cluster-b/redis",
    )
    response = client.post(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "placement" in response.json()["detail"]
    assert called is False


def test_runtime_store_replay_is_shared_between_executor_instances(tmp_path):
    from runtime_store import ReplayError, RuntimeStore

    shared_path = str(tmp_path / "shared-runtime.db")
    first = RuntimeStore(database_path=shared_path)
    second = RuntimeStore(database_path=shared_path)
    identity = {"jti": "shared-jti", "sub": "executor-gateway", "exp": 4_000_000_000}
    first.consume(identity, placement="cluster-a/redis", executor_id="executor-a")
    try:
        second.consume(identity, placement="cluster-b/redis", executor_id="executor-b")
    except ReplayError as exc:
        assert "already been used" in str(exc)
    else:
        raise AssertionError("a second executor accepted a replayed shared credential")


def test_compose_actuators_have_target_scoped_kernel_capabilities_and_no_socket():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose
    assert "pid: service:runtime-actuator-redis" in compose
    assert "network_mode: none" in compose
    assert "cap_drop: [ALL]" in compose
    assert "cap_add: [KILL]" in compose
    assert "security_opt: [no-new-privileges:true]" in compose
    assert "runtime-executor:" in compose and "read_only: true" in compose
    assert "runtime-actuator-payment" in compose
    assert "RUNTIME_EXECUTOR_PLACEMENT: local-compose" in compose


def test_kubernetes_runtime_plane_preserves_workload_scoped_boundaries():
    root = Path("infra/kubernetes/runtime-plane")
    base = (root / "base/deployment.yaml").read_text(encoding="utf-8")
    placement = (root / "placement-config.yaml").read_text(encoding="utf-8")
    policies = (root / "network-policies.yaml").read_text(encoding="utf-8")
    rendered_targets = ["redis", "mysql", "user-service", "order-service", "payment-service"]

    assert "shareProcessNamespace: true" in base
    assert "automountServiceAccountToken: false" in base
    assert 'capabilities: {drop: ["ALL"], add: ["KILL"]}' in base
    assert "readOnlyRootFilesystem: true" in base
    assert "RUNTIME_EXECUTOR_DATABASE_URL" in base
    assert "runtime-audit-database" in base
    assert "port: 2375" in policies and 'opspilot.io/runtime-client: "true"' in policies
    for target in rendered_targets:
        assert f'"{target}"' in placement
        overlay = (root / f"overlays/{target}/target.yaml").read_text(encoding="utf-8")
        assert f"kubernetes/opspilot/{target}" in overlay
        assert f"RUNTIME_EXECUTOR_TARGETS, value: {target}" in overlay
