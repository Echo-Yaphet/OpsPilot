import json
from pathlib import Path

from fastapi.testclient import TestClient

from opspilot import main
from opspilot.models import AgentEvent, AgentName, AnalyzeRequest, Evidence, IncidentState, Recommendation, RiskLevel
from opspilot.knowledge import SemanticKnowledgeRetriever
from opspilot.storage import IncidentStore


class FakeWorkflow:
    calls = 0

    async def run(self, request: AnalyzeRequest):
        self.calls += 1
        return IncidentState(
            incident_id=request.incident_id or "incident-created-from-alert",
            service=request.service,
            symptom=request.symptom,
            status="recommendation_ready",
            evidence=[Evidence(source="prometheus", summary="dependency health metrics", data=[])],
            events=[AgentEvent(agent=AgentName.COORDINATOR, message="investigated")],
            root_cause="Redis dependency is unavailable",
            confidence=0.92,
            recommendations=[Recommendation(
                title="Restart redis", command="docker compose restart redis",
                risk=RiskLevel.MEDIUM, requires_approval=True,
            )],
            execution_requested=request.execute,
        )


def configure_test_app(tmp_path, monkeypatch):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    workflow = FakeWorkflow()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "workflow", workflow)
    return TestClient(main.app), store, workflow


def test_alertmanager_webhook_persists_and_deduplicates(tmp_path, monkeypatch):
    client, store, workflow = configure_test_app(tmp_path, monkeypatch)
    payload = {"status": "firing", "alerts": [{
        "status": "firing", "fingerprint": "redis-payment",
        "labels": {"alertname": "RedisDependencyDown", "service": "payment-service"},
        "annotations": {"summary": "Redis is unavailable to payment-service"},
    }]}

    first = client.post("/api/v1/alertmanager/webhook", json=payload)
    second = client.post("/api/v1/alertmanager/webhook", json=payload)

    assert first.status_code == second.status_code == 200
    assert workflow.calls == 2
    assert len(store.list()) == 1
    assert first.json()["incidents"][0]["execution_requested"] is False
    assert second.json()["incidents"][0]["incident_id"] == "incident-created-from-alert"

    payload["status"] = "resolved"
    payload["alerts"][0]["status"] = "resolved"
    resolved = client.post("/api/v1/alertmanager/webhook", json=payload)
    assert resolved.status_code == 200
    assert store.list()[0].status == "alert_resolved"
    assert workflow.calls == 2


def test_incident_list_and_detail_restore_full_timeline(tmp_path, monkeypatch):
    client, store, _ = configure_test_app(tmp_path, monkeypatch)
    incident = IncidentState(
        incident_id="persisted", service="payment-service", symptom="Redis unavailable",
        events=[AgentEvent(agent=AgentName.RCA, message="root cause found")],
        evidence=[Evidence(source="loki", summary="error logs", data=["connection refused"])],
    )
    store.save(incident)

    listed = client.get("/api/v1/incidents")
    detail = client.get("/api/v1/incidents/persisted")

    assert listed.status_code == detail.status_code == 200
    assert listed.json()[0]["incident_id"] == "persisted"
    assert detail.json()["events"][0]["message"] == "root cause found"
    assert detail.json()["evidence"][0]["data"] == ["connection refused"]
    assert client.get("/api/v1/incidents/missing").status_code == 404


def test_manual_analysis_remains_compatible_and_records_approval(tmp_path, monkeypatch):
    client, store, _ = configure_test_app(tmp_path, monkeypatch)
    response = client.post("/api/v1/incidents/analyze", json={
        "service": "payment-service", "symptom": "Redis unavailable",
        "execute": True, "approved": True,
    })
    assert response.status_code == 200
    assert response.json()["incident_id"] == "incident-created-from-alert"
    with store.connection() as db:
        approval = db.execute("SELECT approved, requested_execution FROM approvals").fetchone()
    assert tuple(approval) == (1, 1)


def test_policy_allow_and_deny_decisions_are_persisted(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    for incident_id, allowed, reason in (
        ("policy-allow", True, "allowed: restart target is allowlisted: redis"),
        ("policy-deny", False, "denied: restart target is not allowlisted: unknown-service"),
    ):
        state = IncidentState(
            incident_id=incident_id,
            evidence=[Evidence(source="execution_policy", summary="execution allowlist decision", data={
                "allowed": allowed,
                "reason": reason,
                "policy": "local-compose-restart-v1",
                "operation": "restart_container" if allowed else None,
                "target": "redis" if allowed else None,
                "command": "docker compose restart redis" if allowed else None,
            })],
        )
        store.save(state)

    with store.connection() as db:
        decisions = db.execute(
            "SELECT incident_id, allowed, reason FROM policy_decisions ORDER BY incident_id"
        ).fetchall()
    assert [tuple(row) for row in decisions] == [
        ("policy-allow", 1, "allowed: restart target is allowlisted: redis"),
        ("policy-deny", 0, "denied: restart target is not allowlisted: unknown-service"),
    ]


def test_sqlite_runbook_retrieval_is_deterministic(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))

    first = store.retrieve_runbooks(
        "payment-service", "Redis unavailable", "Redis dependency is unavailable"
    )
    second = store.retrieve_runbooks(
        "payment-service", "Redis unavailable", "Redis dependency is unavailable"
    )

    assert first == second
    assert [item.runbook_id for item in first] == ["redis-dependency-recovery-v1"]
    assert first[0].command == "docker compose restart redis"
    assert first[0].score == 111
    assert first[0].score_explanation.factors == {
        "root_cause_match": 100.0, "service_match": 10.0, "symptom_match": 1.0,
    }


def test_historical_incident_retrieval_excludes_current_and_returns_compact_summary(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    previous = IncidentState(
        incident_id="previous-redis", service="payment-service", symptom="Redis unavailable",
        status="resolved", root_cause="Redis dependency is unavailable", confidence=0.92,
        recommendations=[Recommendation(
            title="Restart Redis", command="docker compose restart redis",
            risk=RiskLevel.MEDIUM, requires_approval=True,
        )], execution_result="restarted redis", verified=True,
    )
    store.save(previous)
    current = IncidentState(
        incident_id="current-redis", service="payment-service", symptom="Redis unavailable",
        root_cause="Redis dependency is unavailable", confidence=0.92,
    )
    store.save(current)

    matches = store.retrieve_incidents(current)

    assert [item.incident_id for item in matches] == ["previous-redis"]
    assert matches[0].verified is True
    assert matches[0].recommendations[0]["command"] == "docker compose restart redis"
    assert matches[0].score_explanation.factors["verified_outcome"] == 40
    assert "state_json" not in matches[0].model_dump()


def test_historical_retrieval_prioritizes_verified_resolved_outcomes(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    for incident_id, status, verified in (
        ("recent-unverified", "recommendation_ready", None),
        ("older-verified", "resolved", True),
    ):
        store.save(IncidentState(
            incident_id=incident_id, service="payment-service", symptom="Redis unavailable",
            status=status, root_cause="Redis dependency is unavailable", verified=verified,
        ))
    current = IncidentState(
        incident_id="current", service="payment-service", symptom="Redis unavailable",
        root_cause="Redis dependency is unavailable",
    )

    matches = store.retrieve_incidents(current)

    assert [item.incident_id for item in matches] == ["older-verified", "recent-unverified"]


def test_offline_retrieval_evaluation_cases_preserve_dependency_hits(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    cases = json.loads((Path(__file__).parent / "fixtures" / "retrieval_cases.json").read_text())

    for case in cases:
        matches = store.retrieve_runbooks(case["service"], case["symptom"], case["root_cause"])
        if case["expected_runbook_id"] is None:
            assert matches == []
        else:
            assert matches[0].runbook_id == case["expected_runbook_id"]
            assert matches[0].score_explanation.factors["root_cause_match"] == 100


class FakeEmbeddings:
    def embed(self, texts):
        return [[1.0, 0.0]] + [
            [1.0, 0.0] if "MySQL dependency" in text else [0.0, 1.0]
            for text in texts[1:]
        ]


class FailedEmbeddings:
    def embed(self, texts):
        raise RuntimeError("embedding service unavailable")


def test_semantic_retriever_adds_fuzzy_match_without_displacing_deterministic_baseline(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    retriever = SemanticKnowledgeRetriever(store, store, FakeEmbeddings(), minimum_similarity=0.8)

    fuzzy = retriever.retrieve_runbooks(
        "order-service", "primary datastore is unreachable", "Unknown upstream failure"
    )
    baseline = retriever.retrieve_runbooks(
        "payment-service", "Redis unavailable", "Redis dependency is unavailable"
    )

    assert fuzzy[0].runbook_id == "mysql-dependency-recovery-v1"
    assert fuzzy[0].score_explanation.factors == {"semantic_similarity": 1.0}
    assert baseline[0].runbook_id == "redis-dependency-recovery-v1"
    assert baseline[0].score == 111


def test_semantic_retriever_falls_back_when_embedding_service_is_unavailable(tmp_path):
    store = IncidentStore(str(tmp_path / "incidents.db"))
    retriever = SemanticKnowledgeRetriever(store, store, FailedEmbeddings())

    assert retriever.retrieve_runbooks(
        "payment-service", "Redis unavailable", "Redis dependency is unavailable"
    ) == store.retrieve_runbooks(
        "payment-service", "Redis unavailable", "Redis dependency is unavailable"
    )
