import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opspilot.config import (
    Settings,
    VerificationPolicyProvider,
    create_signed_verification_policy_bundle,
)
from opspilot.policy_distribution import (
    AuthenticatedPolicySource,
    PEER_STATUS_OPERATION,
    PEER_STATUS_PATH,
    VerificationPolicyPeerAuthenticator,
    VerificationPolicyRolloutReporter,
)
from opspilot.storage import IncidentStore
from workload_identity import IdentityError, mint_identity


SIGNING_KEY = "distribution-signing-key"
KEY_ID = "policy-v1"


def bundle_bytes(revision: int, attempts: int = 8) -> bytes:
    return json.dumps(create_signed_verification_policy_bundle(
        {"defaults": {"max_attempts": attempts}, "services": {}},
        KEY_ID,
        revision,
        SIGNING_KEY,
    )).encode()


def test_remote_source_caches_only_accepted_bundle_and_survives_partition(tmp_path):
    invalid = bundle_bytes(104, attempts=9).replace(
        b'"max_attempts": 9', b'"max_attempts": 7'
    )
    responses = [bundle_bytes(103), invalid, invalid]

    def fetch():
        if responses:
            return responses.pop(0)
        raise ConnectionError("network partition")

    cache = tmp_path / "accepted.json"
    source = AuthenticatedPolicySource(
        "http://distributor/bundle", "token", str(cache), poll_interval=0, fetch=fetch
    )
    store = IncidentStore(str(tmp_path / "primary.db"))
    settings = Settings(verification_check_interval_seconds=0)
    provider = VerificationPolicyProvider(
        settings.default_verification_policy(),
        signing_keys={KEY_ID: SIGNING_KEY},
        require_signature=True,
        revision_history=store,
        source=source,
    )

    assert provider.policy_for("payment-service").max_attempts == 8
    assert json.loads(cache.read_bytes())["revision"] == 103
    assert provider.status()["load_result"] == "rejected"
    assert json.loads(cache.read_bytes())["revision"] == 103
    assert provider.policy_for("payment-service").max_attempts == 8
    assert provider.status()["load_result"] == "accepted"
    assert provider.status()["distribution"]["using_cache"] is True

    offline = AuthenticatedPolicySource(
        "http://distributor/bundle", "token", str(cache), poll_interval=0,
        fetch=lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    restarted = VerificationPolicyProvider(
        settings.default_verification_policy(),
        signing_keys={KEY_ID: SIGNING_KEY},
        require_signature=True,
        revision_history=IncidentStore(str(tmp_path / "canary.db")),
        source=offline,
    )
    assert restarted.policy_for("payment-service").max_attempts == 8
    assert restarted.status()["distribution"]["using_cache"] is True
    assert restarted.status()["bundle_revision"] == 103


@pytest.mark.parametrize("kwargs", [
    {"verification_policy_distribution_url": "http://distributor/bundle"},
    {"verification_policy_distribution_token": "token"},
    {
        "verification_policy_distribution_url": "http://distributor/bundle",
        "verification_policy_distribution_token": "token",
    },
])
def test_distribution_settings_require_complete_authenticated_signed_mode(kwargs):
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_policy_distributor_requires_bearer_identity(tmp_path, monkeypatch):
    path = tmp_path / "bundle.json"
    path.write_bytes(bundle_bytes(103))
    monkeypatch.setenv("VERIFICATION_POLICY_BUNDLE_FILE", str(path))
    monkeypatch.setenv("VERIFICATION_POLICY_DISTRIBUTION_TOKEN", "distribution-token")
    app_path = Path("/app/policy-distributor/app.py")
    if not app_path.exists():
        app_path = Path("apps/policy-distributor/app.py")
    spec = importlib.util.spec_from_file_location("policy_distributor_test", app_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = TestClient(module.app)

    assert client.get("/bundle").status_code == 401
    assert client.get("/bundle", headers={"Authorization": "Bearer wrong"}).status_code == 401
    accepted = client.get(
        "/bundle", headers={"Authorization": "Bearer distribution-token"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["revision"] == 103


@pytest.mark.asyncio
async def test_rollout_reporter_reports_convergence_and_offline_nodes(monkeypatch):
    status = {
        "bundle_revision": 103,
        "content_digest": "sha256:" + "a" * 64,
        "observed_bundle_revision": 103,
        "observed_content_digest": "sha256:" + "a" * 64,
        "load_result": "accepted",
        "last_error": None,
        "source": "http://distributor/bundle",
        "distribution": {"online": True, "using_cache": False},
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return status

    class Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            assert url.endswith(PEER_STATUS_PATH)
            assert headers["Authorization"].startswith("Bearer ")
            if "offline" in url:
                raise ConnectionError("offline")
            return Response()

    monkeypatch.setattr("opspilot.policy_distribution.httpx.AsyncClient", Client)
    converged = await VerificationPolicyRolloutReporter(
        "primary", {"canary": "http://canary:8080"}
    ).report(status)
    assert converged["converged"] is True
    assert converged["healthy"] is True
    assert converged["rollout_state"] == "converged"
    assert converged["desired"] == {
        "revision": 103,
        "digest": "sha256:" + "a" * 64,
        "conflict": False,
    }
    assert converged["online_nodes"] == converged["total_nodes"] == 2

    degraded_status = {**status, "distribution": {"online": False, "using_cache": True}}
    degraded = await VerificationPolicyRolloutReporter("primary").report(degraded_status)
    assert degraded["converged"] is True
    assert degraded["healthy"] is False
    assert degraded["degraded"] is True
    assert degraded["rollout_state"] == "degraded"

    partitioned = await VerificationPolicyRolloutReporter(
        "primary", {"canary": "http://offline:8080"}
    ).report(status)
    assert partitioned["converged"] is False
    assert partitioned["degraded"] is True
    assert partitioned["stalled"] is False
    assert partitioned["rollout_state"] == "degraded"
    assert partitioned["nodes"][1]["load_result"] == "offline"

    stalled_status = {
        **status,
        "observed_bundle_revision": 104,
        "observed_content_digest": "sha256:" + "b" * 64,
        "load_result": "rejected",
        "last_error": "invalid signature",
    }
    stalled = await VerificationPolicyRolloutReporter("primary").report(stalled_status)
    assert stalled["desired_revision"] == 104
    assert stalled["converged"] is False
    assert stalled["degraded"] is False
    assert stalled["stalled"] is True
    assert stalled["rollout_state"] == "stalled"


def peer_credential(
    *, target="canary", path=PEER_STATUS_PATH, operation=PEER_STATUS_OPERATION,
    ttl_seconds=10, credential_id=None,
):
    return mint_identity(
        "peer-identity-key",
        issuer="opspilot-control-api",
        audience="opspilot-verification-policy-peer",
        subject="primary",
        ttl_seconds=ttl_seconds,
        method="GET",
        path=path,
        operation=operation,
        target=target,
        credential_id=credential_id,
        key_id="peer-v1",
    )


def test_peer_status_identity_is_short_lived_request_bound_and_replay_safe(tmp_path):
    store = IncidentStore(str(tmp_path / "canary.db"))
    authenticator = VerificationPolicyPeerAuthenticator(
        "canary",
        "peer-identity-key",
        "peer-v1",
        "opspilot-control-api",
        "opspilot-verification-policy-peer",
        10,
        store.consume_verification_policy_peer_credential,
    )

    with pytest.raises(IdentityError, match="required"):
        authenticator.verify(None)
    with pytest.raises(IdentityError, match="bound to this request"):
        authenticator.verify(f"Bearer {peer_credential(path='/api/v1/verification-policy/status')}")
    with pytest.raises(IdentityError, match="claims do not match"):
        authenticator.verify(f"Bearer {peer_credential(target='other-node')}")
    with pytest.raises(IdentityError, match="lifetime is invalid"):
        authenticator.verify(f"Bearer {peer_credential(ttl_seconds=11)}")

    token = peer_credential(credential_id="one-time-peer-status")
    identity = authenticator.verify(f"Bearer {token}")
    assert identity["sub"] == "primary"
    with pytest.raises(IdentityError, match="already been used"):
        authenticator.verify(f"Bearer {token}")

    restarted = VerificationPolicyPeerAuthenticator(
        "canary",
        "peer-identity-key",
        "peer-v1",
        "opspilot-control-api",
        "opspilot-verification-policy-peer",
        10,
        IncidentStore(str(tmp_path / "canary.db")).consume_verification_policy_peer_credential,
    )
    with pytest.raises(IdentityError, match="already been used"):
        restarted.verify(f"Bearer {token}")


@pytest.mark.asyncio
async def test_rollout_reporter_bounds_parallel_fanout_and_keeps_partial_results(monkeypatch):
    status = {
        "bundle_revision": 103,
        "content_digest": "sha256:" + "a" * 64,
        "observed_bundle_revision": 103,
        "observed_content_digest": "sha256:" + "a" * 64,
        "load_result": "accepted",
        "last_error": None,
        "source": "http://distributor/bundle",
        "distribution": {"online": True, "using_cache": False},
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return status

    class Client:
        active = 0
        maximum_active = 0

        def __init__(self, timeout):
            assert timeout == 0.5

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            assert headers["Authorization"].startswith("Bearer ")
            Client.active += 1
            Client.maximum_active = max(Client.maximum_active, Client.active)
            try:
                await asyncio.sleep(0.01)
                if "node-3" in url:
                    raise ConnectionError("partitioned")
                return Response()
            finally:
                Client.active -= 1

    monkeypatch.setattr("opspilot.policy_distribution.httpx.AsyncClient", Client)
    peers = {f"node-{index}": f"http://node-{index}:8080" for index in range(6)}
    report = await VerificationPolicyRolloutReporter(
        "primary", peers, timeout=0.5, max_concurrency=2
    ).report(status)

    assert Client.maximum_active == 2
    assert report["online_nodes"] == 6
    assert report["total_nodes"] == 7
    assert report["rollout_state"] == "degraded"
    assert [node["node_id"] for node in report["nodes"]] == ["primary", *sorted(peers)]
