import json

import pytest
from pydantic import ValidationError

from opspilot.config import (
    Settings,
    VerificationPolicyProvider,
    create_signed_verification_policy_bundle,
)
from opspilot.storage import IncidentStore


def test_verification_service_policy_merges_over_defaults():
    settings = Settings(
        verification_max_attempts=8,
        verification_check_interval_seconds=0.5,
        verification_service_policies={
            "payment-service": {"recovery_stable_checks": 2},
            "order-service": {
                "max_attempts": 4,
                "service_health_condition": "status_ok",
                "dependency_metric_threshold": 0.9,
            },
        },
    )

    policies = settings.verification_policies()
    assert policies["payment-service"].max_attempts == 8
    assert policies["payment-service"].check_interval_seconds == 0.5
    assert policies["payment-service"].recovery_stable_checks == 2
    assert policies["order-service"].max_attempts == 4
    assert policies["order-service"].service_health_condition == "status_ok"
    assert policies["order-service"].dependency_metric_threshold == 0.9


@pytest.mark.parametrize("overrides", [
    {"payment-service": {"max_attempts": 0}},
    {"payment-service": {"dependency_metric_threshold": 1.1}},
    {"payment-service": {"service_health_condition": "anything"}},
    {"payment-service": {"max_attempts": 2, "recovery_stable_checks": 3}},
    {"": {"max_attempts": 2}},
])
def test_invalid_verification_policy_is_rejected(overrides):
    with pytest.raises(ValidationError):
        Settings(verification_service_policies=overrides)


@pytest.mark.parametrize("key_id", ["", "has space", "x" * 65])
def test_invalid_executor_identity_key_id_is_rejected(key_id):
    with pytest.raises(ValidationError):
        Settings(executor_identity_key_id=key_id)


@pytest.mark.parametrize("kwargs", [
    {"verification_policy_peer_identity_key": ""},
    {"verification_policy_peer_identity_key_id": "bad key"},
    {"verification_policy_peer_identity_issuer": ""},
    {"verification_policy_peer_identity_audience": ""},
    {"verification_policy_peer_identity_ttl_seconds": 31},
    {"verification_policy_rollout_max_concurrency": 0},
])
def test_invalid_verification_policy_peer_identity_settings_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_policy_file_hot_reload_and_last_known_good(tmp_path):
    path = tmp_path / "verification-policies.json"
    path.write_text('{"defaults":{"max_attempts":4},"services":{}}')
    settings = Settings(verification_check_interval_seconds=0)
    provider = VerificationPolicyProvider(
        settings.default_verification_policy(), settings.verification_service_policies, str(path)
    )

    assert provider.policy_for("payment-service").max_attempts == 4
    first_revision = provider.status()["revision"]

    path.write_text(
        '{"defaults":{"max_attempts":5},"services":'
        '{"payment-service":{"recovery_stable_checks":2}}}'
    )
    policy = provider.policy_for("payment-service")
    assert policy.max_attempts == 5
    assert policy.recovery_stable_checks == 2
    assert provider.status()["revision"] != first_revision

    path.write_text('{"defaults":{"max_attempts":1},"services":'
                    '{"payment-service":{"recovery_stable_checks":2}}}')
    assert provider.policy_for("payment-service") == policy
    assert provider.status()["last_error"].startswith("invalid policy file:")


def test_policy_file_rejects_unknown_fields_without_losing_environment_fallback(tmp_path):
    path = tmp_path / "verification-policies.json"
    path.write_text('{"defaultz":{"max_attempts":2}}')
    settings = Settings(verification_max_attempts=3)
    provider = VerificationPolicyProvider(
        settings.default_verification_policy(), settings.verification_service_policies, str(path)
    )

    assert provider.policy_for("unknown-service").max_attempts == 3
    assert provider.status()["revision"] == "environment"
    assert provider.status()["last_error"].startswith("invalid policy file:")


def write_bundle(path, policy, revision, key_id="policy-v1", key="test-signing-key"):
    path.write_text(json.dumps(create_signed_verification_policy_bundle(
        policy, key_id, revision, key
    )))


def test_signed_policy_bundle_loads_and_records_minimal_revision_history(tmp_path):
    path = tmp_path / "verification-policies.json"
    store = IncidentStore(str(tmp_path / "incidents.db"))
    write_bundle(path, {"defaults": {"max_attempts": 4}, "services": {}}, 7)
    settings = Settings(verification_check_interval_seconds=0)

    provider = VerificationPolicyProvider(
        settings.default_verification_policy(), path=str(path),
        signing_keys={"policy-v1": "test-signing-key"}, require_signature=True,
        revision_history=store,
    )

    assert provider.policy_for("payment-service").max_attempts == 4
    assert provider.status()["bundle_revision"] == 7
    assert provider.status()["key_id"] == "policy-v1"
    assert provider.status()["signature_status"] == "valid"
    with store.connection() as db:
        row = db.execute("SELECT * FROM verification_policy_revisions").fetchone()
    assert set(row.keys()) == {
        "id", "revision", "content_digest", "signature_status", "load_result", "observed_at"
    }
    assert (row["revision"], row["signature_status"], row["load_result"]) == (7, "valid", "accepted")


@pytest.mark.parametrize("mutation, expected", [
    ("tamper", "content digest does not match"),
    ("unknown-key", "unknown verification policy signing key ID"),
    ("bad-signature", "invalid verification policy bundle signature"),
    ("bad-schema", "Extra inputs are not permitted"),
])
def test_signed_policy_rejects_authentication_and_schema_failures(tmp_path, mutation, expected):
    path = tmp_path / "verification-policies.json"
    policy = {"defaults": {"max_attempts": 4}, "services": {}}
    bundle = create_signed_verification_policy_bundle(policy, "policy-v1", 8, "test-signing-key")
    if mutation == "tamper":
        bundle["policy"]["defaults"]["max_attempts"] = 5
    elif mutation == "unknown-key":
        bundle = create_signed_verification_policy_bundle(policy, "unknown", 8, "other-key")
    elif mutation == "bad-signature":
        bundle["signature"] = "hmac-sha256:" + "0" * 64
    else:
        bundle = create_signed_verification_policy_bundle(
            {"defaults": {}, "services": {}, "unexpected": True}, "policy-v1", 8, "test-signing-key"
        )
    path.write_text(json.dumps(bundle))
    settings = Settings(verification_max_attempts=3)

    provider = VerificationPolicyProvider(
        settings.default_verification_policy(), path=str(path),
        signing_keys={"policy-v1": "test-signing-key"}, require_signature=True,
    )

    assert provider.policy_for("payment-service").max_attempts == 3
    assert expected in provider.status()["last_error"]


def test_signed_policy_rejects_rollback_and_retains_last_known_good_across_restart(tmp_path):
    path = tmp_path / "verification-policies.json"
    store = IncidentStore(str(tmp_path / "incidents.db"))
    settings = Settings(verification_check_interval_seconds=0)
    args = (settings.default_verification_policy(), {}, str(path), {"policy-v1": "test-signing-key"}, True, store)
    write_bundle(path, {"defaults": {"max_attempts": 5}, "services": {}}, 9)
    first = VerificationPolicyProvider(*args)
    assert first.policy_for("payment-service").max_attempts == 5

    write_bundle(path, {"defaults": {"max_attempts": 4}, "services": {}}, 9)
    assert first.policy_for("payment-service").max_attempts == 5
    assert "conflicts with the accepted digest" in first.status()["last_error"]

    write_bundle(path, {"defaults": {"max_attempts": 2}, "services": {}}, 8)
    assert first.policy_for("payment-service").max_attempts == 5
    assert "revision rollback rejected" in first.status()["last_error"]

    restarted = VerificationPolicyProvider(*args)
    assert restarted.policy_for("payment-service").max_attempts == 6
    assert "revision rollback rejected" in restarted.status()["last_error"]


def test_signature_required_mode_rejects_legacy_document_but_compatibility_accepts_it(tmp_path):
    path = tmp_path / "verification-policies.json"
    path.write_text('{"defaults":{"max_attempts":4},"services":{}}')
    settings = Settings(verification_max_attempts=3)

    compatible = VerificationPolicyProvider(settings.default_verification_policy(), path=str(path))
    strict = VerificationPolicyProvider(
        settings.default_verification_policy(), path=str(path),
        signing_keys={"policy-v1": "test-signing-key"}, require_signature=True,
    )

    assert compatible.policy_for("payment-service").max_attempts == 4
    assert strict.policy_for("payment-service").max_attempts == 3
    assert "signed verification policy bundle is required" in strict.status()["last_error"]


def test_signature_required_settings_need_a_valid_nonempty_keyring():
    with pytest.raises(ValidationError):
        Settings(verification_policy_require_signature=True)
    with pytest.raises(ValidationError):
        Settings(verification_policy_signing_keys={"bad key": "secret"})
    with pytest.raises(ValidationError):
        Settings(verification_policy_signing_keys={"policy-v1": ""})
