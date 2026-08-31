import pytest
from pydantic import ValidationError

from opspilot.config import Settings, VerificationPolicyProvider


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
