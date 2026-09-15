"""Repeated reliability evaluation for deterministic combined-fault recovery."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EXPECTED_ROOT_CAUSE = "Redis and MySQL dependencies are unavailable"
EXPECTED_TARGETS = ["redis", "mysql"]
EXPECTED_COMMANDS = [
    "docker compose restart redis",
    "docker compose restart mysql",
]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _rate(passed: int, total: int) -> dict:
    return {
        "passed": passed,
        "total": total,
        "rate": round(passed / total, 3) if total else None,
    }


def _wilson(passed: int, total: int) -> dict | None:
    if not total:
        return None
    z = 1.959963984540054
    proportion = passed / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return {
        "low": round(max(0, centre - margin), 3),
        "high": round(min(1, centre + margin), 3),
    }


class Stage6EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,95}$")
    repetitions: int = Field(default=5, ge=1, le=100)
    service: Literal["payment-service"] = "payment-service"
    fault: Literal["redis-mysql-down"] = "redis-mysql-down"


class Stage6TrialObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repetition: int = Field(ge=1)
    status: Literal["completed", "failed", "infrastructure_error"]
    latency_seconds: float = Field(ge=0)
    incident_id: str | None = None
    incident_status: str | None = None
    root_cause: str | None = None
    recommendation_commands: list[str] = Field(default_factory=list)
    policy_targets: list[str] = Field(default_factory=list)
    policy_allowed: list[bool] = Field(default_factory=list)
    execution_targets: list[str] = Field(default_factory=list)
    workflow_verified: bool | None = None
    independent_probe: dict = Field(default_factory=dict)
    failure_reason: str | None = None


class Stage6TrialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: Stage6TrialObservation
    valid: bool
    passed: bool
    checks: dict[str, bool]


class Stage6EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    plan_digest: str
    trials: list[Stage6TrialResult]
    valid_trials: int
    invalid_trials: int
    recovery_success: dict
    recovery_success_wilson_95: dict | None
    check_rates: dict[str, dict]
    mean_success_latency_seconds: float | None


def evaluate_stage6_trial(observation: Stage6TrialObservation) -> Stage6TrialResult:
    checks = {
        "combined_root_cause": observation.root_cause == EXPECTED_ROOT_CAUSE,
        "ordered_recommendations": observation.recommendation_commands == EXPECTED_COMMANDS,
        "full_plan_policy_review": (
            observation.policy_targets == EXPECTED_TARGETS
            and observation.policy_allowed == [True, True]
        ),
        "ordered_execution": observation.execution_targets == EXPECTED_TARGETS,
        "joint_verification": (
            observation.incident_status == "resolved"
            and observation.workflow_verified is True
            and observation.independent_probe.get("passed") is True
        ),
    }
    valid = observation.status != "infrastructure_error"
    return Stage6TrialResult(
        observation=observation,
        valid=valid,
        passed=valid and observation.status == "completed" and all(checks.values()),
        checks=checks,
    )


def build_stage6_report(
    plan: Stage6EvaluationPlan, observations: list[Stage6TrialObservation]
) -> Stage6EvaluationReport:
    if [item.repetition for item in observations] != list(range(1, plan.repetitions + 1)):
        raise ValueError("observations must cover each configured repetition exactly once")
    trials = [evaluate_stage6_trial(item) for item in observations]
    valid = [item for item in trials if item.valid]
    passed = [item for item in valid if item.passed]
    check_names = next(iter(trials)).checks if trials else {}
    check_rates = {
        name: _rate(sum(item.checks[name] for item in valid), len(valid))
        for name in check_names
    }
    return Stage6EvaluationReport(
        evaluation_id=plan.evaluation_id,
        plan_digest=_digest(plan.model_dump(mode="json")),
        trials=trials,
        valid_trials=len(valid),
        invalid_trials=len(trials) - len(valid),
        recovery_success=_rate(len(passed), len(valid)),
        recovery_success_wilson_95=_wilson(len(passed), len(valid)),
        check_rates=check_rates,
        mean_success_latency_seconds=(
            round(sum(item.observation.latency_seconds for item in passed) / len(passed), 3)
            if passed else None
        ),
    )


def _markdown_report(report: Stage6EvaluationReport) -> str:
    success = report.recovery_success
    interval = report.recovery_success_wilson_95
    ci = f"{interval['low']:.1%}-{interval['high']:.1%}" if interval else "n/a"
    lines = [
        f"# Stage 6 repeated evaluation: {report.evaluation_id}",
        "",
        f"- Plan digest: `{report.plan_digest}`",
        f"- Trials: {len(report.trials)} ({report.invalid_trials} infrastructure-invalid)",
        f"- Combined recovery: {success['passed']}/{success['total']} "
        f"({success['rate']:.1%})" if success["rate"] is not None else "- Combined recovery: n/a",
        f"- Wilson 95% CI: {ci}",
        f"- Mean successful latency: {report.mean_success_latency_seconds} s",
        "",
        "## Boundary checks",
        "",
        "| Check | Passed |",
        "|---|---:|",
    ]
    labels = {
        "combined_root_cause": "Deterministic combined root cause",
        "ordered_recommendations": "Redis then MySQL recommendations",
        "full_plan_policy_review": "Whole-plan policy review",
        "ordered_execution": "Redis then MySQL execution",
        "joint_verification": "Workflow plus independent joint verification",
    }
    for name, rate in report.check_rates.items():
        lines.append(f"| {labels[name]} | {rate['passed']}/{rate['total']} |")
    lines.extend([
        "",
        "## Scope",
        "",
        "- This evaluates one enumerated Redis+MySQL fault topology on the local Compose stack.",
        "- Infrastructure-invalid trials remain in raw evidence and are excluded from the reliability denominator.",
        "- Success requires deterministic RCA, the complete allowed plan, ordered execution, workflow Verification, and fresh independent probes.",
        "- The model does not select targets, approve actions, execute repairs, or set `verified`.",
        "",
    ])
    return "\n".join(lines)


def write_stage6_artifacts(
    root: str | Path, plan: Stage6EvaluationPlan, report: Stage6EvaluationReport
) -> Path:
    output = Path(root) / plan.evaluation_id
    output.mkdir(parents=True, exist_ok=False)
    payloads = {
        "plan.json": json.dumps(
            {**plan.model_dump(mode="json"), "plan_digest": report.plan_digest},
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        "trials.jsonl": "".join(
            json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n"
            for item in report.trials
        ),
        "summary.json": json.dumps(
            report.model_dump(mode="json", exclude={"trials"}), indent=2, ensure_ascii=False
        ) + "\n",
        "report.md": _markdown_report(report),
    }
    for name, content in payloads.items():
        path = output / name
        path.write_text(content)
        path.chmod(0o444)
    return output
