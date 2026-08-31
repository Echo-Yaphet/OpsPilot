import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    model_config = ConfigDict(extra="forbid")


class VerificationPolicyDocument(BaseModel):
    """Strict, centrally managed policy document that can be hot-reloaded."""

    defaults: VerificationPolicyOverride = Field(default_factory=VerificationPolicyOverride)
    services: dict[str, VerificationPolicyOverride] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def service_names_are_not_empty(self):
        if any(not service.strip() for service in self.services):
            raise ValueError("verification service policy names must not be empty")
        return self


class VerificationPolicyProvider:
    """Content-addressed policy reload with an immutable last-known-good snapshot."""

    def __init__(
        self,
        default: VerificationPolicy,
        service_overrides: Mapping[str, VerificationPolicyOverride] | None = None,
        path: str | None = None,
    ):
        self._base_default = default
        self._base_overrides = dict(service_overrides or {})
        self._path = Path(path) if path else None
        self._lock = Lock()
        self._default = default
        self._policies = self._merge_services(default, self._base_overrides)
        self._observed_digest: str | None = None
        self._revision = "environment"
        self._last_error: str | None = None
        self._reload_if_changed()

    @staticmethod
    def _merge(default: VerificationPolicy, override: VerificationPolicyOverride) -> VerificationPolicy:
        return VerificationPolicy.model_validate({
            **default.model_dump(),
            **override.model_dump(exclude_none=True),
        })

    @classmethod
    def _merge_services(
        cls,
        default: VerificationPolicy,
        overrides: Mapping[str, VerificationPolicyOverride],
    ) -> dict[str, VerificationPolicy]:
        return {service: cls._merge(default, override) for service, override in overrides.items()}

    def _reload_if_changed(self) -> None:
        if self._path is None:
            return
        try:
            content = self._path.read_bytes()
        except OSError as exc:
            with self._lock:
                self._last_error = f"unable to read policy file: {exc}"
            return
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            if digest == self._observed_digest:
                return
            self._observed_digest = digest
        try:
            document = VerificationPolicyDocument.model_validate(json.loads(content))
            effective_default = self._merge(self._base_default, document.defaults)
            combined = dict(self._base_overrides)
            for service, override in document.services.items():
                previous = combined.get(service, VerificationPolicyOverride())
                combined[service] = VerificationPolicyOverride.model_validate({
                    **previous.model_dump(exclude_none=True),
                    **override.model_dump(exclude_none=True),
                })
            policies = self._merge_services(effective_default, combined)
        except Exception as exc:
            with self._lock:
                self._last_error = f"invalid policy file: {exc}"
            return
        with self._lock:
            self._default = effective_default
            self._policies = policies
            self._revision = digest[:12]
            self._last_error = None

    def policy_for(self, service: str) -> VerificationPolicy:
        self._reload_if_changed()
        with self._lock:
            return self._policies.get(service, self._default)

    def status(self) -> dict:
        self._reload_if_changed()
        with self._lock:
            return {
                "source": str(self._path) if self._path else "environment",
                "revision": self._revision,
                "last_error": self._last_error,
                "services": sorted(self._policies),
            }


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
    verification_policy_file: str | None = None
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
