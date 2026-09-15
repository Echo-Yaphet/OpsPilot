import pytest

from opspilot.fault_domain_evaluation import (
    FaultDomainObservation,
    FaultDomainPlan,
    build_fault_domain_report,
    write_fault_domain_artifacts,
)


def passing_fixture():
    plan = FaultDomainPlan(
        evaluation_id="fault-domain-test",
        nodes=["primary", "canary"],
        partitioned_node="primary",
        database_endpoint="stage10-memory-db:5432",
    )
    observation = FaultDomainObservation(
        baseline_cross_node_visible=True,
        partitioned_node_status=500,
        partitioned_write_absent_after_heal=True,
        survivor_write_status=200,
        recovered_node_status=200,
        pre_failover_replica_caught_up=True,
        promoted_standby_writable=True,
        primary_post_failover_write_status=200,
        canary_post_failover_write_status=200,
        post_failover_cross_node_visible=True,
        scoped_incident_rows=4,
        execution_side_effect_rows=0,
        verification_side_effect_rows=0,
        stable_endpoint_target="memory-db-stage10-standby",
    )
    return plan, observation


def test_passing_report_requires_partition_recovery_and_database_failover():
    plan, observation = passing_fixture()

    report = build_fault_domain_report(plan, observation)

    assert report.passed is True
    assert all(report.checks.values())


def test_writable_partition_or_side_effects_fail_report():
    plan, observation = passing_fixture()
    observation.partitioned_node_status = 200
    observation.partitioned_write_absent_after_heal = False
    observation.execution_side_effect_rows = 1

    report = build_fault_domain_report(plan, observation)

    assert report.passed is False
    assert report.checks["partitioned_node_failed_closed"] is False
    assert report.checks["partitioned_write_not_committed_late"] is False
    assert report.checks["recommendation_only_no_execution"] is False


def test_artifacts_are_read_only_and_cannot_be_overwritten(tmp_path):
    plan, observation = passing_fixture()
    report = build_fault_domain_report(plan, observation)

    output = write_fault_domain_artifacts(tmp_path, plan, report, [{"phase": "done"}])

    assert {path.name for path in output.iterdir()} == {
        "plan.json", "events.jsonl", "summary.json", "report.md",
    }
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in output.iterdir())
    with pytest.raises(FileExistsError):
        write_fault_domain_artifacts(tmp_path, plan, report, [])
