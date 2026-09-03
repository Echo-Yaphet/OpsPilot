import hashlib
import hmac
import json
import re
from pathlib import Path
from threading import Lock
from typing import Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .policy_distribution import AuthenticatedPolicySource, PolicyContentSource


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


class SignedVerificationPolicyBundle(BaseModel):
    """Authenticated wrapper for a strictly validated policy document."""

    key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    revision: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy: dict
    signature: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    model_config = ConfigDict(extra="forbid")


class VerificationPolicyRevisionHistory(Protocol):
    def record_verification_policy_revision(
        self,
        revision: int | None,
        content_digest: str,
        signature_status: str,
        load_result: str,
    ) -> None: ...

    def latest_accepted_verification_policy_revision(self) -> tuple[int, str] | None: ...


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verification_policy_content_digest(policy: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(policy)).hexdigest()}"


def verification_policy_signature(
    key_id: str, revision: int, content_digest: str, key: str
) -> str:
    signed = _canonical_json({
        "content_digest": content_digest,
        "key_id": key_id,
        "revision": revision,
    })
    return f"hmac-sha256:{hmac.new(key.encode(), signed, hashlib.sha256).hexdigest()}"


def create_signed_verification_policy_bundle(
    policy: dict, key_id: str, revision: int, key: str
) -> dict:
    """Create a canonical HMAC bundle for local tooling and tests."""
    content_digest = verification_policy_content_digest(policy)
    bundle = {
        "key_id": key_id,
        "revision": revision,
        "content_digest": content_digest,
        "policy": policy,
        "signature": verification_policy_signature(key_id, revision, content_digest, key),
    }
    return SignedVerificationPolicyBundle.model_validate(bundle).model_dump()


class VerificationPolicyProvider:
    """Content-addressed policy reload with an immutable last-known-good snapshot."""

    def __init__(
        self,
        default: VerificationPolicy,
        service_overrides: Mapping[str, VerificationPolicyOverride] | None = None,
        path: str | None = None,
        signing_keys: Mapping[str, str] | None = None,
        require_signature: bool = False,
        revision_history: VerificationPolicyRevisionHistory | None = None,
        source: PolicyContentSource | None = None,
    ):
        self._base_default = default
        self._base_overrides = dict(service_overrides or {})
        self._path = Path(path) if path else None
        self._signing_keys = dict(signing_keys or {})
        self._require_signature = require_signature
        self._revision_history = revision_history
        self._source = source
        self._lock = Lock()
        self._default = default
        self._policies = self._merge_services(default, self._base_overrides)
        self._observed_digest: str | None = None
        self._revision = "environment"
        self._bundle_revision: int | None = None
        self._content_digest: str | None = None
        self._key_id: str | None = None
        self._signature_status = "environment"
        self._observed_bundle_revision: int | None = None
        self._observed_content_digest: str | None = None
        self._load_result = "environment"
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
        if self._path is None and self._source is None:
            return
        try:
            content = self._source.read_bytes() if self._source else self._path.read_bytes()
        except OSError as exc:
            with self._lock:
                self._last_error = f"unable to read policy file: {exc}"
                self._load_result = "source_error"
            return
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            if digest == self._observed_digest:
                return
            self._observed_digest = digest
        revision: int | None = None
        history_digest = f"sha256:{digest}"
        signature_status = "unsigned"
        try:
            raw = json.loads(content)
            signed_fields = {"key_id", "revision", "content_digest", "policy", "signature"}
            is_bundle = isinstance(raw, dict) and bool(signed_fields.intersection(raw))
            if is_bundle:
                raw_revision = raw.get("revision")
                if (
                    isinstance(raw_revision, int)
                    and not isinstance(raw_revision, bool)
                    and raw_revision >= 1
                ):
                    revision = raw_revision
                raw_digest = raw.get("content_digest")
                if isinstance(raw_digest, str) and re.fullmatch(
                    r"sha256:[0-9a-f]{64}", raw_digest
                ):
                    history_digest = raw_digest
                bundle = SignedVerificationPolicyBundle.model_validate(raw)
                revision = bundle.revision
                history_digest = bundle.content_digest
                with self._lock:
                    self._observed_bundle_revision = revision
                    self._observed_content_digest = history_digest
                actual_digest = verification_policy_content_digest(bundle.policy)
                if not hmac.compare_digest(actual_digest, bundle.content_digest):
                    signature_status = "invalid_digest"
                    raise ValueError("signed policy content digest does not match policy content")
                key = self._signing_keys.get(bundle.key_id)
                if key is None:
                    signature_status = "unknown_key"
                    raise ValueError(f"unknown verification policy signing key ID: {bundle.key_id}")
                expected = verification_policy_signature(
                    bundle.key_id, bundle.revision, bundle.content_digest, key
                )
                if not hmac.compare_digest(expected, bundle.signature):
                    signature_status = "invalid_signature"
                    raise ValueError("invalid verification policy bundle signature")
                signature_status = "valid"
                document = VerificationPolicyDocument.model_validate(bundle.policy)
                accepted = (
                    self._revision_history.latest_accepted_verification_policy_revision()
                    if self._revision_history else None
                )
                if accepted and bundle.revision < accepted[0]:
                    raise ValueError(
                        f"verification policy revision rollback rejected: {bundle.revision} < {accepted[0]}"
                    )
                if accepted and bundle.revision == accepted[0] and bundle.content_digest != accepted[1]:
                    raise ValueError(
                        f"verification policy revision {bundle.revision} conflicts with the accepted digest"
                    )
            else:
                if self._require_signature:
                    signature_status = "required"
                    raise ValueError("signed verification policy bundle is required")
                document = VerificationPolicyDocument.model_validate(raw)
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
                self._observed_bundle_revision = revision
                self._observed_content_digest = history_digest
                self._load_result = "rejected"
            if self._revision_history:
                self._revision_history.record_verification_policy_revision(
                    revision, history_digest, signature_status, "rejected"
                )
            return
        with self._lock:
            self._default = effective_default
            self._policies = policies
            self._revision = str(revision) if revision is not None else digest[:12]
            self._bundle_revision = revision
            self._content_digest = history_digest
            self._key_id = bundle.key_id if revision is not None else None
            self._signature_status = signature_status
            self._last_error = None
            self._observed_bundle_revision = revision
            self._observed_content_digest = history_digest
            self._load_result = "accepted"
        if self._source:
            self._source.accept(content)
        if self._revision_history:
            self._revision_history.record_verification_policy_revision(
                revision, history_digest, signature_status, "accepted"
            )

    def policy_for(self, service: str) -> VerificationPolicy:
        self._reload_if_changed()
        with self._lock:
            return self._policies.get(service, self._default)

    def status(self) -> dict:
        self._reload_if_changed()
        with self._lock:
            return {
                "source": (
                    self._source.status()["url"] if self._source
                    else str(self._path) if self._path else "environment"
                ),
                "revision": self._revision,
                "bundle_revision": self._bundle_revision,
                "content_digest": self._content_digest,
                "key_id": self._key_id,
                "signature_status": self._signature_status,
                "signature_required": self._require_signature,
                "last_error": self._last_error,
                "observed_bundle_revision": self._observed_bundle_revision,
                "observed_content_digest": self._observed_content_digest,
                "load_result": self._load_result,
                "services": sorted(self._policies),
                "distribution": self._source.status() if self._source else {"mode": "local_file"},
            }


class Settings(BaseSettings):
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    executor_gateway_url: str = "http://executor-gateway:8090"
    workload_identity_issuer_url: str = "http://workload-identity-issuer:8085"
    workload_identity_private_key_file: str = "/identity/control-private/private.pem"
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
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout: float = Field(default=90, gt=0, le=300)
    llm_think: bool = False
    verification_max_attempts: int = Field(default=6, ge=1, le=60)
    verification_check_interval_seconds: float = Field(default=2, ge=0, le=300)
    verification_service_health_condition: Literal["healthy", "status_ok"] = "healthy"
    verification_dependency_metric_threshold: float = Field(default=1, ge=0, le=1)
    verification_recovery_stable_checks: int = Field(default=1, ge=1, le=60)
    verification_service_policies: dict[str, VerificationPolicyOverride] = Field(default_factory=dict)
    verification_policy_file: str | None = None
    verification_policy_signing_keys: dict[str, str] = Field(default_factory=dict)
    verification_policy_require_signature: bool = False
    verification_policy_distribution_url: str | None = None
    verification_policy_distribution_token: str = ""
    verification_policy_cache_file: str = "/data/verification-policy-cache.json"
    verification_policy_distribution_timeout: float = Field(default=2, gt=0, le=30)
    verification_policy_distribution_poll_interval: float = Field(default=2, ge=0, le=300)
    verification_policy_node_id: str = "control-api"
    verification_policy_rollout_nodes: dict[str, str] = Field(default_factory=dict)
    verification_policy_rollout_timeout: float = Field(default=2, gt=0, le=30)
    verification_policy_rollout_max_concurrency: int = Field(default=4, ge=1, le=32)
    verification_policy_peer_identity_key: str = "opspilot-local-policy-peer-key"
    verification_policy_peer_identity_key_id: str = "verification-policy-peer-v1"
    verification_policy_peer_identity_issuer: str = "opspilot-control-api"
    verification_policy_peer_identity_audience: str = "opspilot-verification-policy-peer"
    verification_policy_peer_identity_ttl_seconds: int = Field(default=10, ge=1, le=30)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_verification_policies(self):
        if not self.workload_identity_issuer_url.startswith(("http://", "https://")):
            raise ValueError("workload identity issuer URL must be HTTP(S)")
        if not self.workload_identity_private_key_file.strip():
            raise ValueError("workload identity private key file must not be empty")
        if not 1 <= self.executor_identity_ttl_seconds <= 60:
            raise ValueError("executor identity TTL must be between 1 and 60 seconds")
        if bool(self.llm_base_url) != bool(self.llm_model):
            raise ValueError("LLM base URL and model must be configured together")
        if self.llm_base_url and not self.llm_base_url.startswith(("http://", "https://")):
            raise ValueError("LLM base URL must be HTTP(S)")
        default = self.default_verification_policy()
        for service, override in self.verification_service_policies.items():
            if not service.strip():
                raise ValueError("verification service policy names must not be empty")
            self._merge_verification_policy(default, override)
        for key_id, key in self.verification_policy_signing_keys.items():
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id):
                raise ValueError("verification policy signing key IDs must be 1-64 safe characters")
            if not key:
                raise ValueError("verification policy signing keys must not be empty")
        if self.verification_policy_require_signature and not self.verification_policy_signing_keys:
            raise ValueError("signature-required verification policy mode needs at least one signing key")
        if self.verification_policy_distribution_url:
            if not self.verification_policy_distribution_token:
                raise ValueError("verification policy distribution needs an authentication token")
            if not self.verification_policy_require_signature:
                raise ValueError("distributed verification policies must require signatures")
        elif self.verification_policy_distribution_token:
            raise ValueError("verification policy distribution URL is required with a token")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.verification_policy_node_id):
            raise ValueError("verification policy node ID must be 1-64 safe characters")
        if any(not node.strip() or not url.strip() for node, url in self.verification_policy_rollout_nodes.items()):
            raise ValueError("verification policy rollout nodes need nonempty IDs and URLs")
        if not self.verification_policy_peer_identity_key.strip():
            raise ValueError("verification policy peer identity key must not be empty")
        if not re.fullmatch(
            r"[A-Za-z0-9._-]{1,64}", self.verification_policy_peer_identity_key_id
        ):
            raise ValueError("verification policy peer identity key ID must be 1-64 safe characters")
        if (
            not self.verification_policy_peer_identity_issuer.strip()
            or not self.verification_policy_peer_identity_audience.strip()
        ):
            raise ValueError("verification policy peer identity issuer and audience must not be empty")
        return self

    def verification_policy_source(self) -> AuthenticatedPolicySource | None:
        if not self.verification_policy_distribution_url:
            return None
        return AuthenticatedPolicySource(
            self.verification_policy_distribution_url,
            self.verification_policy_distribution_token,
            self.verification_policy_cache_file,
            self.verification_policy_distribution_timeout,
            self.verification_policy_distribution_poll_interval,
        )

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
