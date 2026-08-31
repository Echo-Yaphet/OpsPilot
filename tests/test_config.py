import pytest
from pydantic import ValidationError

from opspilot.config import Settings


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
