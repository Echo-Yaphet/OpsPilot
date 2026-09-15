import json

import pytest

from opspilot.stage6_evaluation import (
    EXPECTED_COMMANDS,
    EXPECTED_ROOT_CAUSE,
    Stage6EvaluationPlan,
    Stage6TrialObservation,
    build_stage6_report,
    evaluate_stage6_trial,
    write_stage6_artifacts,
)


def successful_observation(repetition=1, latency=10):
    return Stage6TrialObservation(
        repetition=repetition,
        status="completed",
        latency_seconds=latency,
        incident_id=f"incident-{repetition}",
        incident_status="resolved",
        root_cause=EXPECTED_ROOT_CAUSE,
        recommendation_commands=EXPECTED_COMMANDS,
        policy_targets=["redis", "mysql"],
        policy_allowed=[True, True],
        execution_targets=["redis", "mysql"],
        workflow_verified=True,
        independent_probe={
            "passed": True,
            "service_healthy": True,
            "target_status": {"redis": "running", "mysql": "running"},
            "dependency_up": {"redis": True, "mysql": True},
        },
    )


def test_trial_requires_the_complete_ordered_and_independently_verified_boundary():
    observation = successful_observation()
    observation.execution_targets = ["mysql", "redis"]

    result = evaluate_stage6_trial(observation)

    assert result.valid is True
    assert result.passed is False
    assert result.checks["ordered_execution"] is False
    assert result.checks["joint_verification"] is True


def test_report_keeps_infrastructure_failures_out_of_the_reliability_denominator():
    plan = Stage6EvaluationPlan(evaluation_id="stage6-repeat", repetitions=3)
    failed = successful_observation(2, 12)
    failed.status = "failed"
    invalid = Stage6TrialObservation(
        repetition=3,
        status="infrastructure_error",
        latency_seconds=1,
        failure_reason="Prometheus unavailable",
    )

    report = build_stage6_report(plan, [successful_observation(), failed, invalid])

    assert report.valid_trials == 2
    assert report.invalid_trials == 1
    assert report.recovery_success == {"passed": 1, "total": 2, "rate": 0.5}
    assert report.mean_success_latency_seconds == 10
    assert report.recovery_success_wilson_95 == {"low": 0.095, "high": 0.905}


def test_report_rejects_missing_or_duplicate_repetitions():
    plan = Stage6EvaluationPlan(evaluation_id="stage6-repeat", repetitions=2)
    with pytest.raises(ValueError, match="each configured repetition"):
        build_stage6_report(plan, [successful_observation(1), successful_observation(1)])


def test_artifacts_are_complete_read_only_and_non_overwritable(tmp_path):
    plan = Stage6EvaluationPlan(evaluation_id="stage6-artifacts", repetitions=1)
    report = build_stage6_report(plan, [successful_observation()])

    output = write_stage6_artifacts(tmp_path, plan, report)

    assert {path.name for path in output.iterdir()} == {
        "plan.json", "trials.jsonl", "summary.json", "report.md",
    }
    assert json.loads((output / "summary.json").read_text())["recovery_success"]["passed"] == 1
    assert "Whole-plan policy review" in (output / "report.md").read_text()
    assert all(path.stat().st_mode & 0o222 == 0 for path in output.iterdir())
    with pytest.raises(FileExistsError):
        write_stage6_artifacts(tmp_path, plan, report)
