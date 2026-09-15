"""Deterministic reporting for shared-store active-active load validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return round(ordered[index], 3)


class ActiveActivePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,95}$")
    nodes: list[str] = Field(min_length=2, max_length=8)
    unique_writes: int = Field(default=40, ge=2, le=1000)
    duplicate_deliveries: int = Field(default=16, ge=2, le=200)
    concurrent_reads: int = Field(default=20, ge=2, le=1000)
    concurrency: int = Field(default=8, ge=2, le=100)


class RequestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    node: str
    status_code: int | None = None
    latency_seconds: float = Field(ge=0)
    incident_id: str | None = None
    expected_incident_id: str | None = None
    error: str | None = None


class DatabaseObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unique_incident_rows: int = Field(ge=0)
    duplicate_alert_rows: int = Field(ge=0)
    state_id_mismatches: int = Field(ge=0)
    orphan_child_rows: int = Field(ge=0)
    execution_side_effect_rows: int = Field(ge=0)


class ActiveActiveObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: float = Field(gt=0)
    node_health: dict[str, bool]
    request_results: list[RequestResult]
    cross_node_visibility_passed: int = Field(ge=0)
    cross_node_visibility_total: int = Field(ge=0)
    database: DatabaseObservation


class ActiveActiveReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    plan_digest: str
    passed: bool
    checks: dict[str, bool]
    request_count: int
    successful_requests: int
    throughput_requests_per_second: float
    latency_p50_seconds: float | None
    latency_p95_seconds: float | None
    latency_max_seconds: float | None
    observation: ActiveActiveObservation


def build_active_active_report(
    plan: ActiveActivePlan, observation: ActiveActiveObservation
) -> ActiveActiveReport:
    writes = [item for item in observation.request_results if item.kind == "unique_write"]
    duplicates = [item for item in observation.request_results if item.kind == "duplicate_alert"]
    reads = [item for item in observation.request_results if item.kind == "concurrent_read"]
    successful = [item for item in observation.request_results if item.status_code == 200]
    write_latencies = [item.latency_seconds for item in writes + duplicates]
    duplicate_ids = {item.incident_id for item in duplicates if item.status_code == 200}
    used_nodes = {item.node for item in successful}
    checks = {
        "both_nodes_healthy": all(observation.node_health.get(node) for node in plan.nodes),
        "all_nodes_served_requests": set(plan.nodes).issubset(used_nodes),
        "unique_writes_succeeded": len(writes) == plan.unique_writes and all(
            item.status_code == 200 and item.incident_id == item.expected_incident_id
            for item in writes
        ),
        "duplicate_deliveries_succeeded": len(duplicates) == plan.duplicate_deliveries and all(
            item.status_code == 200 for item in duplicates
        ),
        "duplicate_alert_converged": len(duplicate_ids) == 1,
        "concurrent_reads_succeeded": len(reads) == plan.concurrent_reads and all(
            item.status_code == 200 for item in reads
        ),
        "cross_node_visibility": (
            observation.cross_node_visibility_total == plan.unique_writes + len(plan.nodes)
            and observation.cross_node_visibility_passed == observation.cross_node_visibility_total
        ),
        "database_cardinality": (
            observation.database.unique_incident_rows == plan.unique_writes
            and observation.database.duplicate_alert_rows == 1
        ),
        "state_identity_consistent": observation.database.state_id_mismatches == 0,
        "no_orphan_children": observation.database.orphan_child_rows == 0,
        "recommendation_only_no_execution": observation.database.execution_side_effect_rows == 0,
    }
    plan_digest = "sha256:" + hashlib.sha256(
        _canonical(plan.model_dump(mode="json")).encode()
    ).hexdigest()
    return ActiveActiveReport(
        evaluation_id=plan.evaluation_id,
        plan_digest=plan_digest,
        passed=all(checks.values()),
        checks=checks,
        request_count=len(observation.request_results),
        successful_requests=len(successful),
        throughput_requests_per_second=round(
            len(observation.request_results) / observation.duration_seconds, 3
        ),
        latency_p50_seconds=_percentile(write_latencies, 0.50),
        latency_p95_seconds=_percentile(write_latencies, 0.95),
        latency_max_seconds=round(max(write_latencies), 3) if write_latencies else None,
        observation=observation,
    )


def _markdown(report: ActiveActiveReport) -> str:
    lines = [
        f"# Active-active Control API evaluation: {report.evaluation_id}",
        "",
        f"- Result: {'PASS' if report.passed else 'FAIL'}",
        f"- Plan digest: `{report.plan_digest}`",
        f"- Requests: {report.successful_requests}/{report.request_count} HTTP 200",
        f"- Observed throughput: {report.throughput_requests_per_second} requests/s",
        f"- Write latency p50/p95/max: {report.latency_p50_seconds}/"
        f"{report.latency_p95_seconds}/{report.latency_max_seconds} s",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---:|",
    ]
    for name, passed in report.checks.items():
        lines.append(f"| {name.replace('_', ' ')} | {'PASS' if passed else 'FAIL'} |")
    lines.extend([
        "",
        "## Scope",
        "",
        "- This is a bounded local Compose test of two Control API processes sharing one PostgreSQL store.",
        "- Requests are recommendation-only; the test never approves or executes remediation.",
        "- Throughput and latency describe this batch only and are not a production SLA.",
        "",
    ])
    return "\n".join(lines)


def write_active_active_artifacts(
    root: str | Path, plan: ActiveActivePlan, report: ActiveActiveReport
) -> Path:
    output = Path(root) / plan.evaluation_id
    output.mkdir(parents=True, exist_ok=False)
    payloads = {
        "plan.json": json.dumps(
            {**plan.model_dump(mode="json"), "plan_digest": report.plan_digest},
            indent=2,
        ) + "\n",
        "requests.jsonl": "".join(
            json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n"
            for item in report.observation.request_results
        ),
        "summary.json": json.dumps(
            report.model_dump(mode="json", exclude={"observation": {"request_results"}}),
            indent=2,
        ) + "\n",
        "report.md": _markdown(report),
    }
    for name, content in payloads.items():
        path = output / name
        path.write_text(content)
        path.chmod(0o444)
    return output
