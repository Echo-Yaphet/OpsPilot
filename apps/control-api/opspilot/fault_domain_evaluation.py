"""Deterministic reporting for network-partition and PostgreSQL failover validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FaultDomainPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,95}$")
    nodes: list[str] = Field(min_length=2, max_length=8)
    partitioned_node: str
    database_endpoint: str
    replication_mode: str = "physical-streaming"
    topology_claim: str = "same-host-fault-domain-emulation"


class FaultDomainObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_cross_node_visible: bool
    partitioned_node_status: int
    partitioned_node_failure_seconds: float = Field(ge=0)
    partitioned_node_failure_target_seconds: float = Field(gt=0)
    concurrent_partition_statuses: list[int] = Field(min_length=2)
    isolated_health_status: int
    isolated_health_seconds: float = Field(ge=0)
    partitioned_write_absent_after_heal: bool
    survivor_write_status: int
    recovered_node_status: int
    pre_failover_replica_caught_up: bool
    promoted_standby_writable: bool
    primary_post_failover_write_status: int
    canary_post_failover_write_status: int
    post_failover_cross_node_visible: bool
    scoped_incident_rows: int = Field(ge=0)
    execution_side_effect_rows: int = Field(ge=0)
    verification_side_effect_rows: int = Field(ge=0)
    stable_endpoint_target: str


class FaultDomainReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    plan_digest: str
    passed: bool
    checks: dict[str, bool]
    observation: FaultDomainObservation


def build_fault_domain_report(
    plan: FaultDomainPlan, observation: FaultDomainObservation
) -> FaultDomainReport:
    checks = {
        "baseline_cross_node_visibility": observation.baseline_cross_node_visible,
        "partitioned_node_failed_closed": observation.partitioned_node_status >= 500,
        "partitioned_node_failed_within_target": (
            observation.partitioned_node_failure_seconds
            <= observation.partitioned_node_failure_target_seconds
        ),
        "concurrent_partition_requests_failed_closed": all(
            status >= 500 for status in observation.concurrent_partition_statuses
        ),
        "isolated_node_health_remained_responsive": (
            observation.isolated_health_status == 200
            and observation.isolated_health_seconds
            <= observation.partitioned_node_failure_target_seconds
        ),
        "partitioned_write_not_committed_late": observation.partitioned_write_absent_after_heal,
        "surviving_node_remained_writable": observation.survivor_write_status == 200,
        "partitioned_node_rejoined": observation.recovered_node_status == 200,
        "replica_caught_up_before_failover": observation.pre_failover_replica_caught_up,
        "standby_promoted_writable": observation.promoted_standby_writable,
        "both_nodes_resumed_after_failover": (
            observation.primary_post_failover_write_status == 200
            and observation.canary_post_failover_write_status == 200
        ),
        "post_failover_cross_node_visibility": observation.post_failover_cross_node_visible,
        "all_scoped_incidents_preserved": observation.scoped_incident_rows == 4,
        "recommendation_only_no_execution": observation.execution_side_effect_rows == 0,
        "recommendation_only_no_verification": observation.verification_side_effect_rows == 0,
        "stable_endpoint_moved_to_promoted_standby": (
            observation.stable_endpoint_target == "memory-db-stage10-standby"
        ),
    }
    digest = "sha256:" + hashlib.sha256(
        _canonical(plan.model_dump(mode="json")).encode()
    ).hexdigest()
    return FaultDomainReport(
        evaluation_id=plan.evaluation_id,
        plan_digest=digest,
        passed=all(checks.values()),
        checks=checks,
        observation=observation,
    )


def write_fault_domain_artifacts(
    output_root: str | Path,
    plan: FaultDomainPlan,
    report: FaultDomainReport,
    events: list[dict[str, object]],
) -> Path:
    output = Path(output_root) / plan.evaluation_id
    output.mkdir(parents=True, exist_ok=False)
    files = {
        "plan.json": _canonical(plan.model_dump(mode="json")) + "\n",
        "events.jsonl": "".join(_canonical(event) + "\n" for event in events),
        "summary.json": _canonical(report.model_dump(mode="json")) + "\n",
        "report.md": _markdown(report),
    }
    for name, content in files.items():
        path = output / name
        path.write_text(content, encoding="utf-8")
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        path.chmod(0o444)
    output.chmod(0o555)
    return output


def _markdown(report: FaultDomainReport) -> str:
    lines = [
        f"# Control API fault-domain evaluation: {report.evaluation_id}",
        "",
        f"- Result: {'PASS' if report.passed else 'FAIL'}",
        f"- Plan digest: `{report.plan_digest}`",
        "- Scope: same-host Docker network fault-domain emulation with PostgreSQL physical streaming replication",
        "- Claim boundary: not real cross-host HA, automatic leader election, or a production SLA",
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
        "## Observed failure timing",
        "",
        (
            f"- Partitioned write failure: {report.observation.partitioned_node_failure_seconds:.3f}s "
            f"(target: <= {report.observation.partitioned_node_failure_target_seconds:.3f}s)"
        ),
        f"- Isolated node `/health`: {report.observation.isolated_health_seconds:.3f}s",
        (
            "- Concurrent partitioned writes: "
            f"{len(report.observation.concurrent_partition_statuses)}"
        ),
        "",
        "## Safety boundary",
        "",
        "All incident requests used recommendation-only mode. The validation does not approve or execute remediation and does not set `verified`.",
        "",
    ])
    return "\n".join(lines)
