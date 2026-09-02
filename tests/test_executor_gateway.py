import importlib.util
import sqlite3
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


def load_gateway(tmp_path, monkeypatch):
    public_path = tmp_path / "issuer-public.pem"
    public_path.write_bytes(ISSUER_PUBLIC)
    monkeypatch.setenv("WORKLOAD_IDENTITY_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.setenv("EXECUTOR_DATABASE_PATH", str(tmp_path / "executor.db"))
    path = Path("/app/executor-gateway/app.py")
    if not path.exists():
        path = Path("apps/executor-gateway/app.py")
    spec = importlib.util.spec_from_file_location("executor_gateway_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


def credential(
    *, operation="restart_container", target="redis", path="/v1/actions",
    audience="opspilot-executor-gateway", ttl_seconds=10, now=None, credential_id=None,
    key_id="opspilot-issuer-v1",
):
    return mint_external_identity(
        ISSUER_PRIVATE,
        issuer="opspilot-workload-identity-issuer",
        audience=audience,
        subject="control-api",
        ttl_seconds=ttl_seconds,
        method="POST",
        path=path,
        operation=operation,
        target=target,
        now=now,
        credential_id=credential_id,
        key_id=key_id,
    )


async def successful_runtime_request(method, path, operation, target):
    return {"status": "running" if method == "GET" else "completed"}


async def failed_runtime_request(method, path, operation, target):
    raise RuntimeError("OS-isolated runtime executor unavailable")


def test_gateway_requires_identity_and_rejects_unknown_target(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    unauthorized = client.post("/v1/actions", json={
        "operation": "restart_container", "target": "redis",
    })
    denied = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "unknown-service"},
        headers={"Authorization": f"Bearer {credential(target='unknown-service')}"},
    )
    assert unauthorized.status_code == 401
    assert denied.status_code == 403
    with sqlite3.connect(module.DATABASE_PATH) as db:
        row = db.execute("SELECT outcome, target FROM execution_audit").fetchone()
    assert row == ("denied", "unknown-service")


def test_gateway_executes_typed_allowlisted_action_and_audits(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "runtime_request", successful_runtime_request)
    response = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "redis"},
        headers={"Authorization": f"Bearer {credential()}"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == "restarted redis"
    with sqlite3.connect(module.DATABASE_PATH) as db:
        row = db.execute(
            "SELECT operation,target,outcome,identity_subject,credential_id IS NOT NULL,identity_key_id "
            "FROM execution_audit"
        ).fetchone()
        consumed = db.execute("SELECT identity_subject FROM consumed_credentials").fetchone()
    assert row == ("restart_container", "redis", "allowed", "control-api", 1, "opspilot-issuer-v1")
    assert consumed == ("control-api",)


def test_gateway_audits_runtime_executor_failure(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "runtime_request", failed_runtime_request)

    response = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "redis"},
        headers={"Authorization": f"Bearer {credential()}"},
    )

    assert response.status_code == 502
    assert "OS-isolated runtime executor unavailable" in response.json()["detail"]
    with sqlite3.connect(module.DATABASE_PATH) as db:
        row = db.execute("SELECT outcome,detail FROM execution_audit").fetchone()
    assert row == ("failed", "OS-isolated runtime executor unavailable")


def test_gateway_rejects_expired_wrong_audience_and_mismatched_credentials(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    expired = credential(now=1)
    wrong_audience = credential(audience="some-other-service")
    wrong_target = credential(target="mysql")
    excessive_lifetime = credential(ttl_seconds=20)

    for token in (expired, wrong_audience, wrong_target, excessive_lifetime):
        response = client.post(
            "/v1/actions",
            json={"operation": "restart_container", "target": "redis"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401


def test_gateway_rejects_replayed_credential(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "runtime_request", successful_runtime_request)
    token = credential(credential_id="one-time-credential")
    request = {
        "json": {"operation": "restart_container", "target": "redis"},
        "headers": {"Authorization": f"Bearer {token}"},
    }

    assert client.post("/v1/actions", **request).status_code == 200
    replay = client.post("/v1/actions", **request)

    assert replay.status_code == 401
    assert "already been used" in replay.json()["detail"]


def test_gateway_rejects_unknown_key_id_before_action(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "runtime_request", successful_runtime_request)
    token = credential(key_id="unknown-key")
    response = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "redis"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert "key ID" in response.json()["detail"]


def test_gateway_uses_trusted_workload_placement_route(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_EXECUTOR_PLACEMENTS",
        '{"redis":{"url":"http://redis-runtime:2375","placement":"prod/redis"}}',
    )
    module, _ = load_gateway(tmp_path, monkeypatch)
    assert module.runtime_route("redis") == ("http://redis-runtime:2375", "prod/redis")
    assert module.runtime_route("mysql") == ("http://runtime-executor:2375", "local-compose")
