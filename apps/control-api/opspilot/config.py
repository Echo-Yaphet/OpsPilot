from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VerificationPolicy(BaseModel):
    """Validated recovery SLO used by the Verification Agent."""

    max_attempts: int = Field(default=6, ge=1, le=60)
    check_interval_seconds: float = Field(default=2, ge=0, le=300)
    service_health_condition: Literal["healthy", "status_ok"] = "healthy"
    dependency_metric_threshold: float = Field(default=1, ge=0, le=1)
    recovery_stable_checks: int = Field(default=1, ge=1, le=60)

    @model_validator(mode="after")
    def stable_checks_fit_attempt_budget(self):
        if self.recovery_stable_checks > self.max_attempts:
            raise ValueError("recovery_stable_checks must not exceed max_attempts")
        return self


class VerificationPolicyOverride(BaseModel):
    """Partial per-service policy merged over the validated default policy."""

    max_attempts: int | None = Field(default=None, ge=1, le=60)
    check_interval_seconds: float | None = Field(default=None, ge=0, le=300)
    service_health_condition: Literal["healthy", "status_ok"] | None = None
    dependency_metric_threshold: float | None = Field(default=None, ge=0, le=1)
    recovery_stable_checks: int | None = Field(default=None, ge=1, le=60)


class Settings(BaseSettings):
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    docker_host: str = "unix:///var/run/docker.sock"
    executor_gateway_url: str = "http://executor-gateway:8090"
    executor_identity_key: str = "opspilot-local-workload-signing-key"
    executor_identity_issuer: str = "opspilot-control-api"
    executor_identity_audience: str = "opspilot-executor-gateway"
    executor_identity_subject: str = "control-api"
    executor_identity_ttl_seconds: int = 10
    executor_gateway_timeout: float = 15
    database_path: str = "/data/opspilot.db"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str = ""
    embedding_timeout: float = 10
    semantic_minimum_similarity: float = 0.75
    verification_max_attempts: int = Field(default=6, ge=1, le=60)
    verification_check_interval_seconds: float = Field(default=2, ge=0, le=300)
    verification_service_health_condition: Literal["healthy", "status_ok"] = "healthy"
    verification_dependency_metric_threshold: float = Field(default=1, ge=0, le=1)
    verification_recovery_stable_checks: int = Field(default=1, ge=1, le=60)
    verification_service_policies: dict[str, VerificationPolicyOverride] = Field(default_factory=dict)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_verification_policies(self):
        default = self.default_verification_policy()
        for service, override in self.verification_service_policies.items():
            if not service.strip():
                raise ValueError("verification service policy names must not be empty")
            self._merge_verification_policy(default, override)
        return self

    def default_verification_policy(self) -> VerificationPolicy:
        return VerificationPolicy(
            max_attempts=self.verification_max_attempts,
            check_interval_seconds=self.verification_check_interval_seconds,
            service_health_condition=self.verification_service_health_condition,
            dependency_metric_threshold=self.verification_dependency_metric_threshold,
            recovery_stable_checks=self.verification_recovery_stable_checks,
        )

    @staticmethod
    def _merge_verification_policy(
        default: VerificationPolicy, override: VerificationPolicyOverride
    ) -> VerificationPolicy:
        return VerificationPolicy.model_validate({
            **default.model_dump(),
            **override.model_dump(exclude_none=True),
        })

    def verification_policies(self) -> dict[str, VerificationPolicy]:
        default = self.default_verification_policy()
        return {
            service: self._merge_verification_policy(default, override)
            for service, override in self.verification_service_policies.items()
        }


settings = Settings()
