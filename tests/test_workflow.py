from datetime import datetime, timezone
from pathlib import Path

import pytest

from opspilot.models import AnalyzeRequest
from opspilot.config import VerificationPolicy
from opspilot.execution import ExecutionPolicy
from opspilot.llm import InvestigationPlan, LLMAnalysis, VerificationExplanation
from opspilot.tools import OpsTools
from opspilot.workflow import IncidentWorkflow
from opspilot.storage import IncidentStore


class FakeTools(OpsTools):
    restarted = None

    async def query_metric(self, query):
        if "container_cpu_usage_ratio" in query:
            return []
        value = "1" if self.restarted else "0"
        return [{"metric": {"service": "payment-service", "dependency": "redis"}, "value": [1, value]}]

    async def query_logs(self, service, minutes=10, limit=100):
        return ["level=ERROR redis dependency failed error=ConnectionError"]

    async def container_status(self, service):
        return "running"

    async def service_health(self, service):
        return {"healthy": self.restarted is not None, "detail": {"status": "ok"}}

    async def restart_container(self, service):
        self.restarted = service
        return f"restarted {service}"

    async def stop_container(self, service):
        return f"stopped {service}"


class InconclusiveTools(FakeTools):
    async def query_metric(self, query):
        return []

    async def query_logs(self, service, minutes=10, limit=100):
        return []


class FailedVerificationTools(FakeTools):
    async def container_status(self, service):
        return "exited"


class SequencedVerificationTools(FakeTools):
    def __init__(self, recovered_sequence):
        self.recovered_sequence = iter(recovered_sequence)
        self.current_recovered = False

    async def container_status(self, service):
        self.current_recovered = next(self.recovered_sequence)
        return "running" if self.current_recovered else "exited"

    async def service_health(self, service):
        return {
            "healthy": self.current_recovered,
            "detail": {"status": "ok" if self.current_recovered else "degraded"},
        }

    async def query_metric(self, query):
        if "dependency_up" not in query or not self.restarted:
            return await super().query_metric(query)
        value = "0.95" if self.current_recovered else "0"
        return [{"metric": {"dependency": "redis"}, "value": [1, value]}]


class HealthyMetricsWithStaleLogsTools(FakeTools):
    async def query_metric(self, query):
        return [
            {"metric": {"service": "payment-service", "dependency": "redis"}, "value": [1, "1"]},
            {"metric": {"service": "payment-service", "dependency": "mysql"}, "value": [1, "1"]},
        ]


class IncidentTimeTools(FakeTools):
    metric_at = None
    log_window = None

    async def query_metric_at(self, query, at=None):
        self.metric_at = at
        return await self.query_metric(query)

    async def query_logs_between(self, service, start, end, limit=100):
        self.log_window = (start, end)
        return ["level=ERROR redis dependency failed inside incident window"]


class HighCPUTools(InconclusiveTools):
    async def query_metric(self, query):
        if "container_cpu_usage_ratio" in query:
            return [{
                "metric": {
                    "__name__": "container_cpu_usage_ratio", "service": "payment-service",
                },
                "value": [1, "0.95"],
            }]
        return []


class FailedExecutor:
    async def execute(self, action):
        raise RuntimeError("executor gateway timed out")


class FakeIncidentAnalyzer:
    name = "ollama/test-model"

    def __init__(self, analysis=None, error=None, plan_error=None, verification_error=None):
        self.plan_result = InvestigationPlan(
            objective="Investigate payment-service dependency failure",
            read_only_tools=["service_health", "container_status"],
            knowledge_query="payment-service Redis dependency unavailable",
            rationale="Correlate dependency metrics, errors, and live process health.",
        )
        self.analysis = analysis or LLMAnalysis(
            root_cause="Redis connection failure confirmed by dependency metric",
            confidence=0.97,
            rationale="The Redis dependency metric is zero and the error log reports a failed connection.",
            recommendation_title="Restore Redis and verify payment-service dependencies",
            candidates=[{
                "root_cause": "Redis dependency is unavailable", "confidence": 0.97,
                "supporting_evidence": ["dependency_up is zero"], "opposing_evidence": [],
            }],
            recommended_steps=["Restore the allowlisted Redis target", "Verify dependency health"],
        )
        self.error = error
        self.plan_error = plan_error
        self.verification_error = verification_error
        self.context = self.plan_context = self.verification_context = None

    async def plan(self, **context):
        self.plan_context = context
        if self.plan_error:
            raise self.plan_error
        return self.plan_result

    async def analyze(self, **context):
        self.context = context
        if self.error:
            raise self.error
        return self.analysis

    async def explain_verification(self, **context):
        self.verification_context = context
        if self.verification_error:
            raise self.verification_error
        return VerificationExplanation(
            summary="Redis and payment-service recovered according to deterministic checks.",
            observed_signals=["container running", "dependency metric recovered"],
            residual_risks=[],
        )


@pytest.mark.asyncio
async def test_redis_failure_produces_safe_recommendation():
    state = await IncidentWorkflow(FakeTools()).run(AnalyzeRequest())
    assert state.root_cause == "Redis dependency is unavailable"
    assert state.confidence == pytest.approx(0.92)
    assert state.status == "recommendation_ready"
    assert state.recommendations[0].requires_approval is True
    assert "restart redis" in state.recommendations[0].command


@pytest.mark.asyncio
async def test_llm_enriches_known_rca_without_replacing_safe_target_or_command():
    analyzer = FakeIncidentAnalyzer()
    state = await IncidentWorkflow(FakeTools(), incident_analyzer=analyzer).run(AnalyzeRequest())

    assert state.root_cause == "Redis dependency is unavailable"
    assert state.confidence == pytest.approx(0.92)
    assert state.recommendations[0].title == analyzer.analysis.recommendation_title
    assert state.recommendations[0].command == "docker compose restart redis"
    evidence = next(item for item in state.evidence if item.source == "llm_analysis")
    assert evidence.data["model"] == "ollama/test-model"
    assert evidence.data["used_for_state"] is False
    assert analyzer.context["deterministic_root_cause"] == "Redis dependency is unavailable"
    assert analyzer.context["investigation_plan"]["knowledge_query"].startswith("payment-service")
    investigation = next(item for item in state.evidence if item.source == "llm_investigation")
    assert {item["tool"] for item in investigation.data["tool_observations"]} == {
        "service_health", "container_status",
    }
    solution = next(item for item in state.evidence if item.source == "llm_solution")
    assert solution.data["steps"] == analyzer.analysis.recommended_steps
    assert "command" not in solution.data


@pytest.mark.asyncio
async def test_llm_can_refine_inconclusive_rca_without_becoming_trusted_execution_input():
    analysis = LLMAnalysis(
        root_cause="Application request saturation suspected",
        confidence=0.95,
        rationale="No dependency failure is visible; application saturation remains a hypothesis.",
        recommendation_title="Inspect payment-service saturation before recovery",
        candidates=[{
            "root_cause": "Application request saturation suspected", "confidence": 0.95,
            "supporting_evidence": [], "opposing_evidence": ["No CPU saturation metric"],
        }],
        recommended_steps=["Inspect request latency and saturation signals"],
    )
    state = await IncidentWorkflow(
        InconclusiveTools(), incident_analyzer=FakeIncidentAnalyzer(analysis=analysis),
    ).run(AnalyzeRequest())

    assert state.root_cause == analysis.root_cause
    assert state.confidence == pytest.approx(0.79)
    assert state.recommendations[0].command == "docker compose restart payment-service"
    evidence = next(item for item in state.evidence if item.source == "llm_analysis")
    assert evidence.data["used_for_state"] is True


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_deterministic_rca():
    state = await IncidentWorkflow(
        FakeTools(), incident_analyzer=FakeIncidentAnalyzer(error=RuntimeError("model unavailable")),
    ).run(AnalyzeRequest())

    assert state.root_cause == "Redis dependency is unavailable"
    assert state.recommendations[0].command == "docker compose restart redis"
    evidence = next(item for item in state.evidence if item.source == "llm_analysis")
    assert evidence.summary == "local LLM unavailable; deterministic RCA retained"
    assert evidence.data["error"] == "model unavailable"


@pytest.mark.asyncio
async def test_llm_planning_failure_retains_mandatory_investigation():
    analyzer = FakeIncidentAnalyzer(plan_error=RuntimeError("planner unavailable"))
    state = await IncidentWorkflow(FakeTools(), incident_analyzer=analyzer).run(AnalyzeRequest())

    assert state.root_cause == "Redis dependency is unavailable"
    evidence = next(item for item in state.evidence if item.source == "llm_investigation")
    assert evidence.summary == "local LLM planning unavailable; mandatory investigation retained"
    assert analyzer.context["investigation_plan"] is None


@pytest.mark.asyncio
async def test_llm_explains_but_cannot_change_deterministic_verification():
    analyzer = FakeIncidentAnalyzer()
    tools = FakeTools()
    state = await IncidentWorkflow(tools, incident_analyzer=analyzer).run(
        AnalyzeRequest(execute=True, approved=True)
    )

    assert state.status == "resolved"
    assert state.verified is True
    explanation = next(item for item in state.evidence if item.source == "llm_verification")
    assert explanation.data["deterministic_verified"] is True
    assert analyzer.verification_context["result"]["verified"] is True


@pytest.mark.asyncio
async def test_execution_is_blocked_without_approval():
    tools = FakeTools()
    state = await IncidentWorkflow(tools).run(AnalyzeRequest(execute=True))
    assert state.status == "awaiting_approval"
    assert tools.restarted is None


@pytest.mark.asyncio
async def test_approved_execution_is_verified():
    tools = FakeTools()
    state = await IncidentWorkflow(tools).run(AnalyzeRequest(execute=True, approved=True))
    assert tools.restarted == "redis"
    assert state.status == "resolved"
    assert state.verified is True
    verification = next(item for item in state.evidence if item.source == "verification")
    assert verification.data["service_healthy"] is True
    assert verification.data["dependency_up"] is True


def test_execution_policy_allows_only_known_restart_operation():
    decision = ExecutionPolicy().evaluate("docker compose restart redis")
    assert decision.allowed is True
    assert decision.action is not None
    assert decision.action.target == "redis"


@pytest.mark.parametrize("command", [
    "docker compose stop redis",
    "docker compose restart unknown-service",
    "docker compose restart redis --no-deps",
    "sh -c 'docker compose restart redis'",
])
def test_execution_policy_rejects_non_allowlisted_commands(command):
    decision = ExecutionPolicy().evaluate(command)
    assert decision.allowed is False
    assert decision.reason.startswith("denied:")


@pytest.mark.asyncio
async def test_approved_execution_is_denied_for_non_allowlisted_target():
    tools = InconclusiveTools()
    state = await IncidentWorkflow(tools).run(AnalyzeRequest(
        service="unknown-service", execute=True, approved=True,
    ))
    assert state.status == "execution_denied"
    assert state.execution_result == "denied: restart target is not allowlisted: unknown-service"
    assert tools.restarted is None
    assert state.verified is None
    policy = next(item for item in state.evidence if item.source == "execution_policy")
    assert policy.data["allowed"] is False


@pytest.mark.asyncio
async def test_inconclusive_rca_completes_graph_without_execution():
    state = await IncidentWorkflow(InconclusiveTools()).run(AnalyzeRequest())
    assert state.root_cause.startswith("Insufficient evidence")
    assert state.confidence == pytest.approx(0.45)
    assert state.status == "recommendation_ready"
    assert [event.agent.value for event in state.events] == [
        "coordinator", "monitor", "log", "rca", "solution", "safety", "executor", "verification"
    ]


@pytest.mark.asyncio
async def test_high_container_cpu_produces_safe_service_recommendation():
    tools = HighCPUTools()
    state = await IncidentWorkflow(tools).run(AnalyzeRequest(
        service="payment-service", symptom="Container CPU usage is high on payment-service",
    ))
    assert state.root_cause == "Container CPU usage is high"
    assert state.confidence == pytest.approx(0.9)
    assert state.status == "recommendation_ready"
    assert state.execution_result is None
    assert state.recommendations[0].title == (
        "Restart payment-service and verify container CPU usage"
    )
    assert state.recommendations[0].command == "docker compose restart payment-service"
    cpu = next(item for item in state.evidence if item.summary == "container CPU usage metrics")
    assert cpu.data[0]["value"][1] == "0.95"


@pytest.mark.asyncio
async def test_graph_state_is_inspectable_after_run():
    workflow = IncidentWorkflow(FakeTools())
    state = await workflow.run(AnalyzeRequest())
    snapshot = await workflow.inspect_state(state.incident_id)
    assert snapshot.values["incident"].incident_id == state.incident_id
    assert snapshot.values["target"] == "redis"
    assert set(workflow.graph.get_graph().nodes) >= {
        "coordinator", "monitor", "log", "rca", "solution", "safety", "executor", "verification"
    }


@pytest.mark.asyncio
async def test_verification_failure_is_reported():
    state = await IncidentWorkflow(
        FailedVerificationTools(), verification_attempts=2, verification_interval=0
    ).run(AnalyzeRequest(execute=True, approved=True))
    assert state.status == "verification_failed"
    assert state.verified is False
    verification = next(item for item in state.evidence if item.source == "verification")
    assert verification.data["attempts"] == 2
    assert verification.data["container_status"] == "exited"


@pytest.mark.asyncio
async def test_service_policy_requires_consecutive_stable_checks_before_resolution():
    tools = SequencedVerificationTools([True, False, True, True])
    policy = VerificationPolicy(
        max_attempts=4,
        check_interval_seconds=0,
        service_health_condition="status_ok",
        dependency_metric_threshold=0.9,
        recovery_stable_checks=2,
    )
    state = await IncidentWorkflow(
        tools, verification_policies={"payment-service": policy}
    ).run(AnalyzeRequest(execute=True, approved=True))

    assert state.status == "resolved"
    verification = next(item for item in state.evidence if item.source == "verification")
    assert verification.data["attempts"] == 4
    assert verification.data["stable_checks"] == 2
    assert verification.data["required_stable_checks"] == 2
    assert verification.data["policy"]["dependency_metric_threshold"] == 0.9


@pytest.mark.asyncio
async def test_service_policy_fails_when_recovery_never_stabilizes_within_budget():
    tools = SequencedVerificationTools([True, False, True])
    policy = VerificationPolicy(
        max_attempts=3,
        check_interval_seconds=0,
        dependency_metric_threshold=0.9,
        recovery_stable_checks=2,
    )
    state = await IncidentWorkflow(
        tools, verification_policies={"payment-service": policy}
    ).run(AnalyzeRequest(execute=True, approved=True))

    assert state.status == "verification_failed"
    verification = next(item for item in state.evidence if item.source == "verification")
    assert verification.data["attempts"] == 3
    assert verification.data["stable_checks"] == 1
    assert verification.data["required_stable_checks"] == 2


@pytest.mark.asyncio
async def test_unknown_service_uses_default_verification_policy():
    policy = VerificationPolicy(max_attempts=2, check_interval_seconds=0)
    workflow = IncidentWorkflow(
        FailedVerificationTools(), default_verification_policy=policy,
        verification_policies={"order-service": VerificationPolicy(max_attempts=4)},
    )

    result = await workflow._poll_recovery("payment-service", "redis")

    assert result["verified"] is False
    assert result["attempts"] == 2
    assert result["policy"]["max_attempts"] == 2


@pytest.mark.asyncio
async def test_verification_resolves_policy_once_per_recovery():
    class Provider:
        calls = 0

        def policy_for(self, service):
            self.calls += 1
            return VerificationPolicy(max_attempts=2, check_interval_seconds=0)

    provider = Provider()
    workflow = IncidentWorkflow(
        FailedVerificationTools(), verification_policy_provider=provider,
    )

    result = await workflow._poll_recovery("payment-service", "redis")

    assert result["attempts"] == 2
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_healthy_metrics_take_precedence_over_stale_error_logs():
    state = await IncidentWorkflow(HealthyMetricsWithStaleLogsTools()).run(AnalyzeRequest())
    assert state.root_cause.startswith("Insufficient evidence")
    assert state.confidence == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_evidence_is_correlated_to_alertmanager_incident_time_without_schema_changes():
    tools = IncidentTimeTools()
    request = AnalyzeRequest(service="payment-service", symptom="Redis unavailable")
    incident_at = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
    request.set_evidence_context(incident_at, "alertmanager")

    state = await IncidentWorkflow(tools).run(request)

    context = next(item for item in state.evidence if item.source == "incident_context")
    assert tools.metric_at == incident_at
    assert tools.log_window[0].isoformat() == "2026-08-31T02:58:00+00:00"
    assert tools.log_window[1].isoformat() == "2026-08-31T03:05:00+00:00"
    assert context.data["origin"] == "alertmanager"
    assert context.data["prometheus"] == {
        "mode": "incident_instant", "query_at": "2026-08-31T03:00:00+00:00",
        "series_count": 1,
    }
    assert context.data["loki"]["line_count"] == 1
    assert "incident_started_at" not in AnalyzeRequest.model_json_schema()["properties"]
    assert set(state.model_dump()) == {
        "incident_id", "service", "symptom", "status", "evidence", "events", "root_cause",
        "confidence", "recommendations", "execution_requested", "execution_result", "verified",
    }


@pytest.mark.asyncio
async def test_executor_gateway_failure_is_reported_without_verification():
    tools = FakeTools()
    state = await IncidentWorkflow(tools, executor=FailedExecutor()).run(
        AnalyzeRequest(execute=True, approved=True)
    )
    assert state.status == "execution_failed"
    assert state.execution_result == "failed: executor gateway timed out"
    assert state.verified is None
    assert tools.restarted is None
    assert state.events[-1].message == "Verification blocked by executor gateway failure"


@pytest.mark.asyncio
async def test_rca_and_solution_use_sqlite_knowledge_without_changing_public_state(tmp_path: Path):
    store = IncidentStore(str(tmp_path / "knowledge.db"))
    previous = await IncidentWorkflow(FakeTools()).run(AnalyzeRequest())
    previous.status = "resolved"
    previous.verified = True
    store.save(previous)

    state = await IncidentWorkflow(FakeTools(), knowledge_retriever=store).run(AnalyzeRequest())

    runbooks = next(item for item in state.evidence if item.source == "runbook")
    history = next(item for item in state.evidence if item.source == "incident_history")
    assert runbooks.data[0]["runbook_id"] == "redis-dependency-recovery-v1"
    assert history.data[0]["incident_id"] == previous.incident_id
    assert state.recommendations[0].title == "Restart Redis and verify dependency metrics"
    assert state.recommendations[0].command == "docker compose restart redis"
