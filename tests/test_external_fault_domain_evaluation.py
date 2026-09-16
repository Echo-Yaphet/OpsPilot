import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opspilot.external_fault_domain_evaluation import (
    DatabaseFailoverObservation,
    ExternalDatabaseTopology,
    ExternalFaultDomainObservation,
    ExternalFaultDomainPlan,
    ExternalIdentityTopology,
    EvidenceReference,
    FailureDomainNode,
    IdentityContinuityObservation,
    NetworkPartitionObservation,
    NodeLossObservation,
    build_external_fault_domain_report,
    verify_external_evidence_files,
    write_external_fault_domain_artifacts,
)


TRUST_DIGEST = "sha256:" + "a" * 64


def passing_fixture():
    plan = ExternalFaultDomainPlan(
        evaluation_id="stage12-external-test",
        control_nodes=[
            FailureDomainNode(
                node_id="control-a",
                endpoint="https://control-a.example.test",
                failure_domain="zone-a",
            ),
            FailureDomainNode(
                node_id="control-b",
                endpoint="https://control-b.example.test",
                failure_domain="zone-b",
            ),
        ],
        database=ExternalDatabaseTopology(
            provider="managed-postgres-test",
            deployment_id="database-cluster-1",
            stable_endpoint="postgres.example.test:5432",
            failure_domains=["zone-a", "zone-b"],
            mode="managed-ha",
        ),
        identity=ExternalIdentityTopology(
            issuer_endpoint="https://issuer.example.test",
            issuer_instances=[
                FailureDomainNode(
                    node_id="issuer-a",
                    endpoint="https://issuer-a.example.test",
                    failure_domain="zone-a",
                ),
                FailureDomainNode(
                    node_id="issuer-b",
                    endpoint="https://issuer-b.example.test",
                    failure_domain="zone-b",
                ),
            ],
            trust_bundle_digest=TRUST_DIGEST,
        ),
        expected_scoped_incidents=6,
    )
    started = datetime(2026, 9, 16, tzinfo=timezone.utc)
    observation = ExternalFaultDomainObservation(
        observed_at=started + timedelta(minutes=10),
        baseline_node_health={"control-a": True, "control-b": True},
        baseline_cross_node_visibility=True,
        network_partition=NetworkPartitionObservation(
            partitioned_node_id="control-a",
            surviving_node_id="control-b",
            partitioned_write_status=599,
            surviving_write_status=200,
            partitioned_write_absent_after_heal=True,
            healed_node_read_status=200,
        ),
        node_loss=NodeLossObservation(
            lost_node_id="control-a",
            surviving_node_id="control-b",
            lost_node_unreachable=True,
            surviving_write_status=200,
            replacement_node_id="control-a-replacement",
            replacement_failure_domain="zone-c",
            replacement_health_status=200,
            replacement_reads_survivor_write=True,
        ),
        database_failover=DatabaseFailoverObservation(
            provider_event_id="provider-event-123",
            provider_event_kind="automatic-primary-failover",
            provider_started_at=started,
            provider_completed_at=started + timedelta(minutes=2),
            old_primary_id="db-a",
            new_primary_id="db-b",
            provider_reported_complete=True,
            stable_endpoint_unchanged=True,
            primary_write_status=200,
            canary_write_status=200,
            cross_node_visibility=True,
        ),
        identity_continuity=IdentityContinuityObservation(
            failed_issuer_instance_id="issuer-a",
            failed_issuer_unreachable=True,
            surviving_issuer_instance_id="issuer-b",
            issuance_during_instance_loss_status=200,
            peer_first_use_status=200,
            peer_replay_status=401,
            wrong_target_status=401,
            unknown_target_status=403,
            replacement_or_recovered_issuer_status=200,
            observed_trust_bundle_digest=TRUST_DIGEST,
        ),
        evidence_references=[
            EvidenceReference(
                kind=kind,
                source_id=f"{kind}.json",
                captured_at=started + timedelta(minutes=9),
                digest="sha256:" + character * 64,
            )
            for kind, character in (
                ("control-node", "1"),
                ("database-provider", "2"),
                ("identity-issuer", "3"),
                ("database-audit", "4"),
            )
        ],
        scoped_incident_rows=6,
        execution_side_effect_rows=0,
        verification_side_effect_rows=0,
    )
    return plan, observation


def test_external_evidence_passes_only_when_every_fault_domain_check_passes():
    plan, observation = passing_fixture()

    report = build_external_fault_domain_report(
        plan, observation, raw_evidence_verified=True
    )

    assert report.passed is True
    assert all(report.checks.values())
    assert report.plan_digest.startswith("sha256:")
    assert report.evidence_digest.startswith("sha256:")


def test_same_failure_domain_topologies_are_rejected():
    plan, _ = passing_fixture()
    same_host_nodes = [
        node.model_copy(update={"failure_domain": "docker-desktop"})
        for node in plan.control_nodes
    ]

    with pytest.raises(ValidationError, match="at least two distinct failure domains"):
        ExternalFaultDomainPlan(
            **{**plan.model_dump(), "control_nodes": same_host_nodes}
        )


def test_database_and_issuer_each_require_distinct_failure_domains():
    with pytest.raises(ValidationError, match="database must span"):
        ExternalDatabaseTopology(
            provider="postgres-test",
            deployment_id="database-1",
            stable_endpoint="postgres.example.test:5432",
            failure_domains=["zone-a", "zone-a"],
            mode="managed-ha",
        )

    with pytest.raises(ValidationError, match="issuer must span"):
        ExternalIdentityTopology(
            issuer_endpoint="https://issuer.example.test",
            issuer_instances=[
                FailureDomainNode(
                    node_id="issuer-a",
                    endpoint="https://issuer-a.example.test",
                    failure_domain="zone-a",
                ),
                FailureDomainNode(
                    node_id="issuer-b",
                    endpoint="https://issuer-b.example.test",
                    failure_domain="zone-a",
                ),
            ],
            trust_bundle_digest=TRUST_DIGEST,
        )


def test_missing_provider_identity_or_safety_evidence_fails_closed():
    plan, observation = passing_fixture()
    observation = observation.model_copy(update={
        "database_failover": observation.database_failover.model_copy(update={
            "provider_reported_complete": False,
            "stable_endpoint_unchanged": False,
        }),
        "identity_continuity": observation.identity_continuity.model_copy(update={
            "issuance_during_instance_loss_status": 503,
            "observed_trust_bundle_digest": "sha256:" + "b" * 64,
        }),
        "evidence_references": observation.evidence_references[:-1],
        "execution_side_effect_rows": 1,
    })

    report = build_external_fault_domain_report(
        plan, observation, raw_evidence_verified=False
    )

    assert report.passed is False
    assert report.checks["provider_failover_completed"] is False
    assert report.checks["stable_database_endpoint_preserved"] is False
    assert report.checks["identity_issuance_survived_instance_loss"] is False
    assert report.checks["trust_bundle_continuity"] is False
    assert report.checks["required_external_evidence_referenced"] is False
    assert report.checks["raw_evidence_digests_verified"] is False
    assert report.checks["recommendation_only_no_execution"] is False


def test_external_artifacts_are_read_only_and_cannot_be_overwritten(tmp_path):
    plan, observation = passing_fixture()
    report = build_external_fault_domain_report(
        plan, observation, raw_evidence_verified=True
    )

    output = write_external_fault_domain_artifacts(tmp_path, plan, report)

    assert {path.name for path in output.iterdir()} == {
        "plan.json", "observation.json", "summary.json", "report.md",
    }
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in output.iterdir())
    with pytest.raises(FileExistsError):
        write_external_fault_domain_artifacts(tmp_path, plan, report)


def test_raw_evidence_files_must_exist_inside_root_and_match_digest(tmp_path):
    _, observation = passing_fixture()
    references = []
    for reference in observation.evidence_references:
        content = f"external evidence for {reference.kind}\n".encode()
        path = tmp_path / reference.source_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        references.append(reference.model_copy(update={
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }))
    observation = observation.model_copy(update={"evidence_references": references})

    verify_external_evidence_files(tmp_path, observation)

    (tmp_path / references[0].source_id).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_external_evidence_files(tmp_path, observation)
