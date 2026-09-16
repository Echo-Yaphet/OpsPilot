"""Fail-closed reporting for real external failure-domain acceptance evidence."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


class FailureDomainNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(pattern=r"^https?://")
    failure_domain: str = Field(min_length=1, max_length=128)


class ExternalDatabaseTopology(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=128)
    deployment_id: str = Field(min_length=1, max_length=256)
    stable_endpoint: str = Field(min_length=1, max_length=512)
    failure_domains: list[str] = Field(min_length=2, max_length=16)
    mode: str = Field(pattern=r"^(managed-ha|independently-operated-ha)$")

    @model_validator(mode="after")
    def require_distinct_failure_domains(self) -> "ExternalDatabaseTopology":
        if len(set(self.failure_domains)) < 2:
            raise ValueError("database must span at least two distinct failure domains")
        return self


class ExternalIdentityTopology(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_endpoint: str = Field(pattern=r"^https?://")
    issuer_instances: list[FailureDomainNode] = Field(min_length=2, max_length=16)
    trust_bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_distinct_instances_and_domains(self) -> "ExternalIdentityTopology":
        instance_ids = {item.node_id for item in self.issuer_instances}
        domains = {item.failure_domain for item in self.issuer_instances}
        if len(instance_ids) != len(self.issuer_instances):
            raise ValueError("issuer instance IDs must be unique")
        if len(domains) < 2:
            raise ValueError("issuer must span at least two distinct failure domains")
        return self


class ExternalFaultDomainPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,95}$")
    control_nodes: list[FailureDomainNode] = Field(min_length=2, max_length=16)
    database: ExternalDatabaseTopology
    identity: ExternalIdentityTopology
    expected_scoped_incidents: int = Field(ge=6, le=1000)
    topology_claim: str = "external-independent-failure-domains"

    @model_validator(mode="after")
    def require_external_topology(self) -> "ExternalFaultDomainPlan":
        node_ids = {item.node_id for item in self.control_nodes}
        endpoints = {item.endpoint for item in self.control_nodes}
        domains = {item.failure_domain for item in self.control_nodes}
        if len(node_ids) != len(self.control_nodes):
            raise ValueError("control node IDs must be unique")
        if len(endpoints) != len(self.control_nodes):
            raise ValueError("control node endpoints must be unique")
        if len(domains) < 2:
            raise ValueError("control nodes must span at least two distinct failure domains")
        if self.topology_claim != "external-independent-failure-domains":
            raise ValueError("same-host topology cannot satisfy external acceptance")
        return self


class NetworkPartitionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    partitioned_node_id: str
    surviving_node_id: str
    partitioned_write_status: int
    surviving_write_status: int
    partitioned_write_absent_after_heal: bool
    healed_node_read_status: int


class NodeLossObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lost_node_id: str
    surviving_node_id: str
    lost_node_unreachable: bool
    surviving_write_status: int
    replacement_node_id: str
    replacement_failure_domain: str
    replacement_health_status: int
    replacement_reads_survivor_write: bool


class DatabaseFailoverObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_event_id: str = Field(min_length=1, max_length=512)
    provider_event_kind: str = Field(min_length=1, max_length=128)
    provider_started_at: datetime
    provider_completed_at: datetime
    old_primary_id: str = Field(min_length=1, max_length=256)
    new_primary_id: str = Field(min_length=1, max_length=256)
    provider_reported_complete: bool
    stable_endpoint_unchanged: bool
    primary_write_status: int
    canary_write_status: int
    cross_node_visibility: bool

    @model_validator(mode="after")
    def require_ordered_provider_event(self) -> "DatabaseFailoverObservation":
        if self.provider_completed_at <= self.provider_started_at:
            raise ValueError("provider failover completion must follow its start")
        return self


class IdentityContinuityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failed_issuer_instance_id: str
    failed_issuer_unreachable: bool
    surviving_issuer_instance_id: str
    issuance_during_instance_loss_status: int
    peer_first_use_status: int
    peer_replay_status: int
    wrong_target_status: int
    unknown_target_status: int
    replacement_or_recovered_issuer_status: int
    observed_trust_bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(
        pattern=r"^(control-node|database-provider|identity-issuer|database-audit)$"
    )
    source_id: str = Field(min_length=1, max_length=512)
    captured_at: datetime
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ExternalFaultDomainObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: datetime
    baseline_node_health: dict[str, bool]
    baseline_cross_node_visibility: bool
    network_partition: NetworkPartitionObservation
    node_loss: NodeLossObservation
    database_failover: DatabaseFailoverObservation
    identity_continuity: IdentityContinuityObservation
    evidence_references: list[EvidenceReference] = Field(min_length=4, max_length=128)
    scoped_incident_rows: int = Field(ge=0)
    execution_side_effect_rows: int = Field(ge=0)
    verification_side_effect_rows: int = Field(ge=0)

    @model_validator(mode="after")
    def require_completed_evidence_window(self) -> "ExternalFaultDomainObservation":
        if self.database_failover.provider_completed_at > self.observed_at:
            raise ValueError("observation must follow provider failover completion")
        if any(item.captured_at > self.observed_at for item in self.evidence_references):
            raise ValueError("observation must follow every evidence capture")
        return self


class ExternalFaultDomainReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str
    plan_digest: str
    evidence_digest: str
    passed: bool
    checks: dict[str, bool]
    observation: ExternalFaultDomainObservation


def build_external_fault_domain_report(
    plan: ExternalFaultDomainPlan,
    observation: ExternalFaultDomainObservation,
    *,
    raw_evidence_verified: bool,
) -> ExternalFaultDomainReport:
    nodes = {item.node_id: item for item in plan.control_nodes}
    issuer_instances = {item.node_id: item for item in plan.identity.issuer_instances}
    partition = observation.network_partition
    node_loss = observation.node_loss
    failover = observation.database_failover
    identity = observation.identity_continuity
    evidence_kinds = {item.kind for item in observation.evidence_references}
    failed_issuer = issuer_instances.get(identity.failed_issuer_instance_id)
    surviving_issuer = issuer_instances.get(identity.surviving_issuer_instance_id)
    lost_node = nodes.get(node_loss.lost_node_id)

    checks = {
        "all_control_nodes_healthy_at_baseline": (
            set(observation.baseline_node_health) == set(nodes)
            and all(observation.baseline_node_health.values())
        ),
        "baseline_cross_node_visibility": observation.baseline_cross_node_visibility,
        "partition_targets_known_distinct_nodes": (
            partition.partitioned_node_id in nodes
            and partition.surviving_node_id in nodes
            and partition.partitioned_node_id != partition.surviving_node_id
        ),
        "partitioned_node_failed_closed": partition.partitioned_write_status >= 500,
        "partition_survivor_remained_writable": partition.surviving_write_status == 200,
        "partitioned_write_not_committed_late": (
            partition.partitioned_write_absent_after_heal
        ),
        "partitioned_node_rejoined": partition.healed_node_read_status == 200,
        "node_loss_targets_known_distinct_nodes": (
            node_loss.lost_node_id in nodes
            and node_loss.surviving_node_id in nodes
            and node_loss.lost_node_id != node_loss.surviving_node_id
        ),
        "node_loss_observed": node_loss.lost_node_unreachable,
        "node_loss_survivor_remained_writable": node_loss.surviving_write_status == 200,
        "replacement_started_in_a_different_failure_domain": (
            lost_node is not None
            and node_loss.replacement_node_id != node_loss.lost_node_id
            and node_loss.replacement_failure_domain != lost_node.failure_domain
        ),
        "replacement_rejoined_shared_state": (
            node_loss.replacement_health_status == 200
            and node_loss.replacement_reads_survivor_write
        ),
        "provider_failover_completed": failover.provider_reported_complete,
        "database_primary_changed": failover.old_primary_id != failover.new_primary_id,
        "stable_database_endpoint_preserved": failover.stable_endpoint_unchanged,
        "both_nodes_writable_after_database_failover": (
            failover.primary_write_status == 200 and failover.canary_write_status == 200
        ),
        "post_failover_cross_node_visibility": failover.cross_node_visibility,
        "issuer_loss_targets_known_distinct_instances": (
            failed_issuer is not None
            and surviving_issuer is not None
            and failed_issuer.node_id != surviving_issuer.node_id
            and failed_issuer.failure_domain != surviving_issuer.failure_domain
        ),
        "issuer_instance_loss_observed": identity.failed_issuer_unreachable,
        "identity_issuance_survived_instance_loss": (
            identity.issuance_during_instance_loss_status == 200
        ),
        "peer_identity_binding_and_replay_protection_preserved": (
            identity.peer_first_use_status == 200
            and identity.peer_replay_status == 401
            and identity.wrong_target_status == 401
            and identity.unknown_target_status == 403
        ),
        "issuer_capacity_restored": identity.replacement_or_recovered_issuer_status == 200,
        "trust_bundle_continuity": (
            identity.observed_trust_bundle_digest == plan.identity.trust_bundle_digest
        ),
        "required_external_evidence_referenced": evidence_kinds == {
            "control-node", "database-provider", "identity-issuer", "database-audit"
        },
        "raw_evidence_digests_verified": raw_evidence_verified,
        "all_scoped_incidents_preserved": (
            observation.scoped_incident_rows == plan.expected_scoped_incidents
        ),
        "recommendation_only_no_execution": observation.execution_side_effect_rows == 0,
        "recommendation_only_no_verification": observation.verification_side_effect_rows == 0,
    }
    plan_payload = plan.model_dump(mode="json")
    observation_payload = observation.model_dump(mode="json")
    return ExternalFaultDomainReport(
        evaluation_id=plan.evaluation_id,
        plan_digest=_digest(plan_payload),
        evidence_digest=_digest(observation_payload),
        passed=all(checks.values()),
        checks=checks,
        observation=observation,
    )


def verify_external_evidence_files(
    evidence_root: str | Path,
    observation: ExternalFaultDomainObservation,
) -> None:
    root = Path(evidence_root).resolve(strict=True)
    for reference in observation.evidence_references:
        relative = Path(reference.source_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe evidence source path: {reference.source_id}")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular evidence file: {reference.source_id}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError(f"evidence source escapes root: {reference.source_id}")
        actual = "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != reference.digest:
            raise ValueError(f"evidence digest mismatch: {reference.source_id}")


def write_external_fault_domain_artifacts(
    output_root: str | Path,
    plan: ExternalFaultDomainPlan,
    report: ExternalFaultDomainReport,
) -> Path:
    output = Path(output_root) / plan.evaluation_id
    output.mkdir(parents=True, exist_ok=False)
    payloads = {
        "plan.json": _canonical(plan.model_dump(mode="json")) + "\n",
        "observation.json": _canonical(report.observation.model_dump(mode="json")) + "\n",
        "summary.json": _canonical(
            report.model_dump(mode="json", exclude={"observation"})
        ) + "\n",
        "report.md": _markdown(report),
    }
    for name, content in payloads.items():
        path = output / name
        path.write_text(content, encoding="utf-8")
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        path.chmod(0o444)
    output.chmod(0o555)
    return output


def _markdown(report: ExternalFaultDomainReport) -> str:
    lines = [
        f"# External fault-domain acceptance: {report.evaluation_id}",
        "",
        f"- Result: {'PASS' if report.passed else 'FAIL'}",
        f"- Plan digest: `{report.plan_digest}`",
        f"- Evidence digest: `{report.evidence_digest}`",
        "- Scope: independent control, database, and issuer failure domains",
        "- Claim boundary: acceptance evidence only; not an SLA, zero-data-loss, RPO, or RTO claim",
        "",
        "## Checks",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'}: `{name}`"
        for name, passed in report.checks.items()
    )
    lines.extend([
        "",
        "## Safety boundary",
        "",
        "All scoped incidents are recommendation-only. Infrastructure fault injection remains outside OpsPilot; the evaluator cannot approve or execute remediation and cannot set `verified`.",
        "",
    ])
    return "\n".join(lines)
