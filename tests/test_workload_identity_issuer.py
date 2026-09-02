import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import pytest

from workload_identity import IdentityError, mint_external_identity, sign_issuer_request, verify_external_identity


def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
    )


def load_issuer(tmp_path, monkeypatch):
    issuer_private, issuer_public = key_pair()
    control_private, control_public = key_pair()
    issuer_private_path = tmp_path / "issuer-private.pem"
    control_public_path = tmp_path / "control-public.pem"
    issuer_private_path.write_bytes(issuer_private)
    control_public_path.write_bytes(control_public)
    monkeypatch.setenv("WORKLOAD_IDENTITY_SIGNING_KEY_FILE", str(issuer_private_path))
    monkeypatch.setenv("WORKLOAD_IDENTITY_DATABASE_PATH", str(tmp_path / "issuer.db"))
    monkeypatch.setenv("WORKLOAD_IDENTITY_CLIENT_KEYS", json.dumps({"control-api": str(control_public_path)}))
    path = Path("/app/workload-identity-issuer/app.py")
    if not path.exists():
        path = Path("apps/workload-identity-issuer/app.py")
    spec = importlib.util.spec_from_file_location("workload_identity_issuer_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app), control_private, issuer_public


def identity_request():
    return {
        "audience": "opspilot-executor-gateway", "ttl_seconds": 10,
        "method": "POST", "path": "/v1/actions",
        "operation": "restart_container", "target": "redis",
    }


def test_external_issuer_authenticates_workload_and_mints_bound_identity(tmp_path, monkeypatch):
    module, client, control_private, issuer_public = load_issuer(tmp_path, monkeypatch)
    payload = identity_request()
    response = client.post(
        "/v1/identity", json=payload,
        headers=sign_issuer_request(control_private, "control-api", payload),
    )
    assert response.status_code == 200
    identity = verify_external_identity(
        response.json()["token"], issuer_public, key_id=module.KEY_ID,
        issuer=module.ISSUER, audience="opspilot-executor-gateway",
        method="POST", path="/v1/actions", maximum_ttl_seconds=15,
    )
    assert (identity["sub"], identity["operation"], identity["target"]) == (
        "control-api", "restart_container", "redis",
    )


def test_external_issuer_rejects_proof_replay_and_unallowed_audience(tmp_path, monkeypatch):
    _, client, control_private, _ = load_issuer(tmp_path, monkeypatch)
    payload = identity_request()
    headers = sign_issuer_request(control_private, "control-api", payload, nonce="one-use-proof")
    assert client.post("/v1/identity", json=payload, headers=headers).status_code == 200
    assert client.post("/v1/identity", json=payload, headers=headers).status_code == 401
    payload["audience"] = "opspilot-runtime-executor"
    denied_headers = sign_issuer_request(control_private, "control-api", payload)
    assert client.post("/v1/identity", json=payload, headers=denied_headers).status_code == 403


@pytest.mark.parametrize("change", ["audience", "path", "expired", "lifetime", "key_id"])
def test_external_identity_rejects_invalid_issuer_contract(change):
    private, public = key_pair()
    mint = {
        "key_id": "opspilot-issuer-v1", "issuer": "opspilot-workload-identity-issuer",
        "audience": "opspilot-executor-gateway", "subject": "control-api",
        "ttl_seconds": 10, "method": "POST", "path": "/v1/actions",
        "operation": "restart_container", "target": "redis",
    }
    verify = {
        "key_id": "opspilot-issuer-v1", "issuer": "opspilot-workload-identity-issuer",
        "audience": "opspilot-executor-gateway", "method": "POST",
        "path": "/v1/actions", "maximum_ttl_seconds": 15,
    }
    if change == "audience":
        mint["audience"] = "wrong-audience"
    elif change == "path":
        mint["path"] = "/wrong"
    elif change == "expired":
        mint["now"] = 1
    elif change == "lifetime":
        mint["ttl_seconds"] = 20
    else:
        mint["key_id"] = "unknown-issuer-key"
    token = mint_external_identity(private, **mint)
    with pytest.raises(IdentityError):
        verify_external_identity(token, public, **verify)
