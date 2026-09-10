"""Held-out, paired evaluation for advisory Skill versions.

The runner owns labels and aggregation. Trial adapters receive only executable case
inputs, so expected outcomes cannot leak into model or Skill generation context.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _rounded(value: float) -> float:
    return round(value, 3)


class EvaluationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = Field(min_length=1, max_length=200)
    max_turns: int = Field(default=5, ge=1, le=12)
    max_tool_calls: int = Field(default=6, ge=1, le=20)
    max_total_tokens: int = Field(default=4096, ge=256, le=65536)
    timeout_seconds: float = Field(default=120, gt=0, le=300)


class EvaluationArm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    arm_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    skill_version: int = Field(ge=1)


class EvaluationCase(BaseModel):
    """Server-owned case inputs and labels; never passed directly to an adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,95}$")
    split: Literal["heldout", "regression", "safety"]
    topology: str = Field(min_length=1, max_length=100)
    service: str = Field(min_length=1, max_length=100)
    fault: str = Field(min_length=1, max_length=100)
    symptom: str = Field(min_length=1, max_length=500)
    expected_root_cause: str | None = Field(default=None, max_length=200)
    expects_recovery: bool = False
    expects_blocked: bool = False


class EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evaluation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,95}$")
    arms: list[EvaluationArm] = Field(min_length=2, max_length=8)
    case_ids: list[str] = Field(min_length=1, max_length=500)
    repetitions: int = Field(default=1, ge=1, le=100)
    budget: EvaluationBudget
    seed: int = Field(default=20260910, ge=0)

    @field_validator("arms")
    @classmethod
    def arms_are_unique(cls, values: list[EvaluationArm]):
        if len({item.arm_id for item in values}) != len(values):
            raise ValueError("evaluation arm IDs must be unique")
        if len({item.skill_version for item in values}) != len(values):
            raise ValueError("evaluation Skill versions must be unique")
        return values

    @field_validator("case_ids")
    @classmethod
    def case_ids_are_unique(cls, values: list[str]):
        if len(set(values)) != len(values):
            raise ValueError("evaluation case IDs must be unique")
        return values


class TrialSpec(BaseModel):
    """Label-free input crossing the trial adapter seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    trial_id: str
    evaluation_id: str
    arm_id: str
    skill_version: int
    case_id: str
    split: Literal["heldout", "regression", "safety"]
    topology: str
    service: str
    fault: str
    symptom: str
    repetition: int
    seed: int
    budget: EvaluationBudget


class TrialObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "failed", "timeout", "infrastructure_error"]
    recovered: bool = False
    root_cause: str | None = None
    blocked: bool = False
    latency_seconds: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    incident_id: str | None = None
    failure_reason: str | None = None
    independent_probe: dict = Field(default_factory=dict)
    diagnostic_evidence: dict = Field(default_factory=dict)


class TrialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: TrialSpec
    observation: TrialObservation
    valid: bool
    passed: bool
    root_cause_correct: bool


class ArmMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trials: int
    invalid_trials: int
    recovery_success: dict
    recovery_success_wilson_95: dict | None
    root_cause_accuracy: dict
    safety_interception: dict
    tokens_per_success: float | None
    requests_per_success: float | None
    mean_success_latency_seconds: float | None


class EvaluationComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_arm: str
    candidate_arm: str
    recovery_success_delta_points: float | None
    tokens_per_success_reduction_percent: float | None
    mean_success_latency_reduction_percent: float | None
    regression_rate: dict


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_id: str
    plan_digest: str
    case_set_digest: str
    trials: list[TrialResult]
    arm_metrics: dict[str, ArmMetrics]
    comparison: EvaluationComparison
    grouped_metrics: dict


class TrialAdapter(Protocol):
    async def run_trial(self, spec: TrialSpec) -> TrialObservation: ...


def _rate(passed: int, total: int) -> dict:
    return {"passed": passed, "total": total, "rate": _rounded(passed / total) if total else None}


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
    return {"low": _rounded(max(0, centre - margin)), "high": _rounded(min(1, centre + margin))}


class EvaluationRunner:
    """Run paired trials while keeping held-out labels behind one small interface."""

    def __init__(self, cases: list[EvaluationCase], adapter: TrialAdapter, *, trial_sink=None):
        self.cases = {case.case_id: case for case in cases}
        if len(self.cases) != len(cases):
            raise ValueError("evaluation case IDs must be unique")
        self.adapter = adapter
        self.trial_sink = trial_sink

    async def run(self, plan: EvaluationPlan) -> EvaluationReport:
        missing = set(plan.case_ids) - set(self.cases)
        if missing:
            raise ValueError(f"unknown evaluation cases: {sorted(missing)}")
        selected = [self.cases[case_id] for case_id in plan.case_ids]
        plan_digest = _digest(plan.model_dump(mode="json"))
        case_set_digest = _digest([case.model_dump(mode="json") for case in selected])
        trials = []
        for case_index, case in enumerate(selected):
            for repetition in range(1, plan.repetitions + 1):
                paired_seed = plan.seed + case_index * 1000 + repetition * 10
                arms = list(plan.arms)
                if (plan.seed + case_index + repetition - 1) % 2:
                    arms.reverse()
                for arm in arms:
                    spec = TrialSpec(
                        trial_id=f"{plan.evaluation_id}-{case.case_id}-r{repetition}-{arm.arm_id}",
                        evaluation_id=plan.evaluation_id, arm_id=arm.arm_id,
                        skill_version=arm.skill_version, case_id=case.case_id,
                        split=case.split, topology=case.topology, service=case.service,
                        fault=case.fault, symptom=case.symptom, repetition=repetition,
                        seed=paired_seed,
                        budget=plan.budget,
                    )
                    observation = await self.adapter.run_trial(spec)
                    valid = observation.status != "infrastructure_error"
                    root_cause_correct = observation.root_cause == case.expected_root_cause
                    if case.split == "safety":
                        passed = valid and observation.blocked == case.expects_blocked
                    elif case.expects_recovery:
                        passed = valid and observation.recovered and root_cause_correct
                    else:
                        passed = valid and root_cause_correct
                    trial = TrialResult(
                        spec=spec, observation=observation, valid=valid, passed=passed,
                        root_cause_correct=root_cause_correct,
                    )
                    trials.append(trial)
                    if self.trial_sink is not None:
                        saved = self.trial_sink(trial)
                        if inspect.isawaitable(saved):
                            await saved

        arm_metrics = {
            arm.arm_id: self._arm_metrics(trials, arm.arm_id, selected)
            for arm in plan.arms
        }
        baseline, candidate = plan.arms[:2]
        comparison = self._compare(trials, baseline.arm_id, candidate.arm_id, arm_metrics)
        return EvaluationReport(
            evaluation_id=plan.evaluation_id, plan_digest=plan_digest,
            case_set_digest=case_set_digest, trials=trials, arm_metrics=arm_metrics,
            comparison=comparison, grouped_metrics=self._grouped(trials, selected),
        )

    @staticmethod
    def _arm_metrics(trials: list[TrialResult], arm_id: str,
                     cases: list[EvaluationCase]) -> ArmMetrics:
        by_case = {case.case_id: case for case in cases}
        arm = [item for item in trials if item.spec.arm_id == arm_id]
        valid = [item for item in arm if item.valid]
        recovery = [item for item in valid if by_case[item.spec.case_id].expects_recovery]
        recovery_passed = sum(item.passed for item in recovery)
        diagnosis = [item for item in valid if item.spec.split != "safety"]
        safety = [item for item in valid if item.spec.split == "safety"]
        successful = [item for item in recovery if item.passed]
        total_tokens = sum(
            item.observation.input_tokens + item.observation.output_tokens for item in recovery
        )
        total_requests = sum(item.observation.requests for item in recovery)
        success_count = len(successful)
        return ArmMetrics(
            trials=len(arm), invalid_trials=len(arm) - len(valid),
            recovery_success=_rate(recovery_passed, len(recovery)),
            recovery_success_wilson_95=_wilson(recovery_passed, len(recovery)),
            root_cause_accuracy=_rate(
                sum(item.root_cause_correct for item in diagnosis), len(diagnosis)
            ),
            safety_interception=_rate(sum(item.passed for item in safety), len(safety)),
            tokens_per_success=_rounded(total_tokens / success_count) if success_count else None,
            requests_per_success=_rounded(total_requests / success_count) if success_count else None,
            mean_success_latency_seconds=(
                _rounded(sum(item.observation.latency_seconds for item in successful) / success_count)
                if success_count else None
            ),
        )

    @staticmethod
    def _compare(trials: list[TrialResult], baseline: str, candidate: str,
                 metrics: dict[str, ArmMetrics]) -> EvaluationComparison:
        left, right = metrics[baseline], metrics[candidate]

        def delta_points():
            a, b = left.recovery_success["rate"], right.recovery_success["rate"]
            return _rounded((b - a) * 100) if a is not None and b is not None else None

        def reduction(a: float | None, b: float | None):
            return _rounded((a - b) / a * 100) if a not in (None, 0) and b is not None else None

        indexed = {
            (item.spec.arm_id, item.spec.case_id, item.spec.repetition): item
            for item in trials if item.valid
        }
        baseline_passes = [
            item for item in trials
            if item.valid and item.spec.arm_id == baseline
            and item.spec.split == "regression" and item.passed
        ]
        regressions = sum(
            not indexed.get((candidate, item.spec.case_id, item.spec.repetition), item).passed
            for item in baseline_passes
        )
        return EvaluationComparison(
            baseline_arm=baseline, candidate_arm=candidate,
            recovery_success_delta_points=delta_points(),
            tokens_per_success_reduction_percent=reduction(
                left.tokens_per_success, right.tokens_per_success
            ),
            mean_success_latency_reduction_percent=reduction(
                left.mean_success_latency_seconds, right.mean_success_latency_seconds
            ),
            regression_rate=_rate(regressions, len(baseline_passes)),
        )

    @staticmethod
    def _grouped(trials: list[TrialResult], cases: list[EvaluationCase]) -> dict:
        case_map = {case.case_id: case for case in cases}
        groups = {}
        for item in trials:
            if not item.valid:
                continue
            key = f"{item.spec.arm_id}:{case_map[item.spec.case_id].topology}"
            group = groups.setdefault(key, {"passed": 0, "total": 0})
            group["passed"] += int(item.passed)
            group["total"] += 1
        for value in groups.values():
            value["rate"] = _rounded(value["passed"] / value["total"])
        return groups


def _markdown_report(report: EvaluationReport) -> str:
    lines = [
        f"# Stage 5 evaluation: {report.evaluation_id}", "",
        f"- Plan digest: `{report.plan_digest}`",
        f"- Case-set digest: `{report.case_set_digest}`", "",
        "## Arm results", "",
        "| Arm | Trials | Invalid | Recovery success | 95% Wilson CI | Tokens / success | Requests / success | Mean success latency | RCA accuracy | Safety interception |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm_id, metrics in report.arm_metrics.items():
        recovery = metrics.recovery_success
        interval = metrics.recovery_success_wilson_95
        ci = f"{interval['low']:.1%}-{interval['high']:.1%}" if interval else "n/a"
        rate = recovery["rate"]
        lines.append(
            f"| {arm_id} | {metrics.trials} | {metrics.invalid_trials} | "
            f"{recovery['passed']}/{recovery['total']} ({rate:.1%}) | {ci} | "
            f"{metrics.tokens_per_success if metrics.tokens_per_success is not None else 'n/a'} | "
            f"{metrics.requests_per_success if metrics.requests_per_success is not None else 'n/a'} | "
            f"{metrics.mean_success_latency_seconds if metrics.mean_success_latency_seconds is not None else 'n/a'} s | "
            f"{metrics.root_cause_accuracy['passed']}/{metrics.root_cause_accuracy['total']} | "
            f"{metrics.safety_interception['passed']}/{metrics.safety_interception['total']} |"
        )
    comparison = report.comparison
    lines.extend([
        "", "## Paired comparison", "",
        f"- Baseline: `{comparison.baseline_arm}`",
        f"- Candidate: `{comparison.candidate_arm}`",
        f"- Recovery success delta: {comparison.recovery_success_delta_points if comparison.recovery_success_delta_points is not None else 'n/a'} percentage points",
        f"- Tokens per success reduction: {comparison.tokens_per_success_reduction_percent if comparison.tokens_per_success_reduction_percent is not None else 'n/a'}%",
        f"- Mean success latency reduction: {comparison.mean_success_latency_reduction_percent if comparison.mean_success_latency_reduction_percent is not None else 'n/a'}%",
        f"- Regression rate: {comparison.regression_rate['passed']}/{comparison.regression_rate['total']} ({comparison.regression_rate['rate']})",
        "", "## Integrity notes", "",
        "- Failed and timed-out model attempts remain in rate and cost denominators.",
        "- Infrastructure errors are retained in raw trials but excluded from model-quality denominators.",
        "- Held-out expected labels are not written to plans or trial records.",
        "- Recovery is credited only when the trial adapter reports an independent probe success.",
        "",
    ])
    return "\n".join(lines)


def write_evaluation_artifacts(root: str | Path, plan: EvaluationPlan,
                               report: EvaluationReport) -> Path:
    """Write one content-addressed, non-overwritable evaluation evidence bundle."""
    output = Path(root) / plan.evaluation_id
    output.mkdir(parents=True, exist_ok=False)
    payloads = {
        "plan.json": json.dumps({
            **plan.model_dump(mode="json"),
            "plan_digest": report.plan_digest,
            "case_set_digest": report.case_set_digest,
        }, indent=2, ensure_ascii=False) + "\n",
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
