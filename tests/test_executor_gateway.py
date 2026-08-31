import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from workload_identity import mint_identity


def load_gateway(
    tmp_path, monkeypatch, *, current_id="control-api-v1", current_key="test-signing-key",
    previous_id=None, previous_key=None, previous_valid_until=None,
):
    monkeypatch.setenv("EXECUTOR_IDENTITY_KEY_ID", current_id)
    monkeypatch.setenv("EXECUTOR_IDENTITY_KEY", current_key)
    for name, value in (
        ("EXECUTOR_IDENTITY_PREVIOUS_KEY_ID", previous_id),
        ("EXECUTOR_IDENTITY_PREVIOUS_KEY", previous_key),
        ("EXECUTOR_IDENTITY_PREVIOUS_KEY_VALID_UNTIL", previous_valid_until),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))
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
    secret="test-signing-key", key_id="control-api-v1",
):
    return mint_identity(
        secret,
        issuer="opspilot-control-api",
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


async def successful_runtime_request(method, path):
    return {"status": "running" if method == "GET" else "completed"}


async def failed_runtime_request(method, path):
    raise RuntimeError("restricted Docker proxy unavailable")


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
    assert row == ("restart_container", "redis", "allowed", "control-api", 1, "control-api-v1")
    assert consumed == ("control-api",)


def test_gateway_audits_restricted_proxy_failure(tmp_path, monkeypatch):
    module, client = load_gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "runtime_request", failed_runtime_request)

    response = client.post(
        "/v1/actions",
        json={"operation": "restart_container", "target": "redis"},
        headers={"Authorization": f"Bearer {credential()}"},
    )

    assert response.status_code == 502
    assert "restricted Docker proxy unavailable" in response.json()["detail"]
    with sqlite3.connect(module.DATABASE_PATH) as db:
        row = db.execute("SELECT outcome,detail FROM execution_audit").fetchone()
    assert row == ("failed", "restricted Docker proxy unavailable")


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


def test_gateway_accepts_current_and_previous_keys_only_during_overlap(tmp_path, monkeypatch):
    module, client = load_gateway(
        tmp_path,
        monkeypatch,
        current_id="control-api-v2",
        current_key="new-signing-key",
        previous_id="control-api-v1",
        previous_key="old-signing-key",
        previous_valid_until=int(time.time()) + 300,
    )
    monkeypatch.setattr(module, "runtime_request", successful_runtime_request)

    current = credential(secret="new-signing-key", key_id="control-api-v2")
    previous = credential(secret="old-signing-key", key_id="control-api-v1")
    request = {"operation": "restart_container", "target": "redis"}
    assert client.post(
        "/v1/actions", json=request, headers={"Authorization": f"Bearer {current}"}
    ).status_code == 200
    assert client.post(
        "/v1/actions", json=request, headers={"Authorization": f"Bearer {previous}"}
    ).status_code == 200

    module.IDENTITY_PREVIOUS_VALID_UNTIL = 1
    expired_overlap = credential(secret="old-signing-key", key_id="control-api-v1")
    response = client.post(
        "/v1/actions", json=request, headers={"Authorization": f"Bearer {expired_overlap}"}
    )
    assert response.status_code == 401
    assert "overlap has expired" in response.json()["detail"]


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
    assert "key ID is unknown" in response.json()["detail"]


@pytest.mark.parametrize(
    "values,error",
    [
        ({"previous_id": "control-api-v0"}, "configured together"),
        ({
            "previous_id": "control-api-v1",
            "previous_key": "old-signing-key",
            "previous_valid_until": 1,
            "current_id": "control-api-v1",
        }, "key IDs must differ"),
        ({
            "previous_id": "control-api-v0",
            "previous_key": "test-signing-key",
            "previous_valid_until": 4102444800,
        }, "keys must differ"),
        ({
            "previous_id": "control-api-v0",
            "previous_key": "old-signing-key",
            "previous_valid_until": "not-a-timestamp",
        }, "Unix timestamp"),
        ({
            "previous_id": "control-api-v0",
            "previous_key": "old-signing-key",
            "previous_valid_until": 4102444800,
        }, "exceeds the configured limit"),
    ],
)
def test_gateway_rejects_invalid_rotation_configuration(tmp_path, monkeypatch, values, error):
    with pytest.raises(RuntimeError, match=error):
        load_gateway(tmp_path, monkeypatch, **values)
