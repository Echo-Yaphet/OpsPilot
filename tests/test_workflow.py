import pytest
from pathlib import Path

from opspilot.models import AnalyzeRequest
from opspilot.execution import ExecutionPolicy
from opspilot.tools import OpsTools
from opspilot.workflow import IncidentWorkflow
from opspilot.storage import IncidentStore


class FakeTools(OpsTools):
    restarted = None

    async def query_metric(self, query):
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


class HealthyMetricsWithStaleLogsTools(FakeTools):
    async def query_metric(self, query):
        return [
            {"metric": {"service": "payment-service", "dependency": "redis"}, "value": [1, "1"]},
            {"metric": {"service": "payment-service", "dependency": "mysql"}, "value": [1, "1"]},
        ]


class FailedExecutor:
    async def execute(self, action):
        raise RuntimeError("executor gateway timed out")


@pytest.mark.asyncio
async def test_redis_failure_produces_safe_recommendation():
    state = await IncidentWorkflow(FakeTools()).run(AnalyzeRequest())
    assert state.root_cause == "Redis dependency is unavailable"
    assert state.confidence == pytest.approx(0.92)
    assert state.status == "recommendation_ready"
    assert state.recommendations[0].requires_approval is True
    assert "restart redis" in state.recommendations[0].command


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
async def test_healthy_metrics_take_precedence_over_stale_error_logs():
    state = await IncidentWorkflow(HealthyMetricsWithStaleLogsTools()).run(AnalyzeRequest())
    assert state.root_cause.startswith("Insufficient evidence")
    assert state.confidence == pytest.approx(0.45)


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
