import pytest

from opspilot.active_active_evaluation import (
    ActiveActiveObservation,
    ActiveActivePlan,
    DatabaseObservation,
    RequestResult,
    build_active_active_report,
    write_active_active_artifacts,
)


def passing_fixture():
    plan = ActiveActivePlan(
        evaluation_id="active-active-test", nodes=["primary", "canary"],
        unique_writes=2, duplicate_deliveries=2, concurrent_reads=2, concurrency=2,
    )
    results = [
        RequestResult(kind="unique_write", node="primary", status_code=200,
                      latency_seconds=0.1, incident_id="one", expected_incident_id="one"),
        RequestResult(kind="unique_write", node="canary", status_code=200,
                      latency_seconds=0.2, incident_id="two", expected_incident_id="two"),
        RequestResult(kind="duplicate_alert", node="primary", status_code=200,
                      latency_seconds=0.3, incident_id="shared"),
        RequestResult(kind="duplicate_alert", node="canary", status_code=200,
                      latency_seconds=0.4, incident_id="shared"),
        RequestResult(kind="concurrent_read", node="primary", status_code=200,
                      latency_seconds=0.05),
        RequestResult(kind="concurrent_read", node="canary", status_code=200,
                      latency_seconds=0.05),
    ]
    observation = ActiveActiveObservation(
        duration_seconds=1, node_health={"primary": True, "canary": True},
        request_results=results, cross_node_visibility_passed=4,
        cross_node_visibility_total=4, database=DatabaseObservation(
            unique_incident_rows=2, duplicate_alert_rows=1, state_id_mismatches=0,
            orphan_child_rows=0, execution_side_effect_rows=0,
        ),
    )
    return plan, observation


def test_passing_report_requires_both_nodes_dedup_visibility_and_clean_database():
    plan, observation = passing_fixture()

    report = build_active_active_report(plan, observation)

    assert report.passed is True
    assert all(report.checks.values())
    assert report.latency_p95_seconds == 0.4
    assert report.throughput_requests_per_second == 6


def test_duplicate_alert_divergence_fails_report():
    plan, observation = passing_fixture()
    observation.request_results[3].incident_id = "split-brain"

    report = build_active_active_report(plan, observation)

    assert report.passed is False
    assert report.checks["duplicate_alert_converged"] is False


def test_database_side_effect_or_orphan_fails_report():
    plan, observation = passing_fixture()
    observation.database.orphan_child_rows = 1
    observation.database.execution_side_effect_rows = 1

    report = build_active_active_report(plan, observation)

    assert report.checks["no_orphan_children"] is False
    assert report.checks["recommendation_only_no_execution"] is False


def test_artifacts_are_read_only_and_cannot_be_overwritten(tmp_path):
    plan, observation = passing_fixture()
    report = build_active_active_report(plan, observation)

    output = write_active_active_artifacts(tmp_path, plan, report)

    assert {path.name for path in output.iterdir()} == {
        "plan.json", "requests.jsonl", "summary.json", "report.md",
    }
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in output.iterdir())
    with pytest.raises(FileExistsError):
        write_active_active_artifacts(tmp_path, plan, report)
