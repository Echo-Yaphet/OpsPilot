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
    VerificationPolicyRolloutReporter,
)
from opspilot.storage import IncidentStore


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

        async def get(self, url):
            if "offline" in url:
                raise ConnectionError("offline")
            return Response()

    monkeypatch.setattr("opspilot.policy_distribution.httpx.AsyncClient", Client)
    converged = await VerificationPolicyRolloutReporter(
        "primary", {"canary": "http://canary:8080"}
    ).report(status)
    assert converged["converged"] is True
    assert converged["healthy"] is True
    assert converged["online_nodes"] == converged["total_nodes"] == 2

    degraded_status = {**status, "distribution": {"online": False, "using_cache": True}}
    degraded = await VerificationPolicyRolloutReporter("primary").report(degraded_status)
    assert degraded["converged"] is True
    assert degraded["healthy"] is False

    partitioned = await VerificationPolicyRolloutReporter(
        "primary", {"canary": "http://offline:8080"}
    ).report(status)
    assert partitioned["converged"] is False
    assert partitioned["nodes"][1]["load_result"] == "offline"
