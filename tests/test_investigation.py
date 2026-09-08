import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from opspilot.investigation import (
    InvestigationBudget, InvestigationJournal, SDKInvestigator,
    compact_investigation_context,
)
from opspilot.models import AnalyzeRequest
from opspilot.workflow import IncidentWorkflow
from test_workflow import FakeTools


def harness(tmp_path, responder, tools=None, **budget):
    requests = []

    async def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        message = responder(data, len(requests))
        for item in message.get("tool_calls", []):
            item["id"] = f"call-{len(requests)}"
        return httpx.Response(200, json={
            "id": f"chat-{len(requests)}", "object": "chat.completion", "created": 1,
            "model": "test", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })

    model = OpenAIChatCompletionsModel(model="test", openai_client=AsyncOpenAI(
        api_key="test", base_url="http://model.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ))
    investigator = SDKInvestigator(tools or FakeTools(), model,
        InvestigationJournal(str(tmp_path / "runs.db")), InvestigationBudget(**budget))
    return investigator, requests


def call(name, arguments="{}"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": arguments},
    }]}


async def run(investigator):
    return await investigator.investigate(incident_id="incident-1", service="payment-service",
        symptom="Redis unavailable", incident_at=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_sdk_observes_tool_result_before_choosing_next_probe_and_persists(tmp_path):
    def respond(data, turn):
        if turn == 1:
            assert {x["function"]["name"] for x in data["tools"]} == {
                "service_health", "container_status", "dependency_metrics", "error_logs"}
            return call("service_health")
        outputs = [x for x in data["messages"] if x["role"] == "tool"]
        if turn == 2:
            assert json.loads(json.loads(outputs[-1]["content"])["result"])["healthy"] is False
            return call("dependency_metrics")
        assert "redis" in outputs[-1]["content"]
        return {"role": "assistant", "content": "Redis metric is zero; MySQL remains untested."}

    investigator, requests = harness(tmp_path, respond)
    result = await run(investigator)
    assert result["status"] == "completed", result
    assert result["tool_calls"] == 2
    assert result["usage"]["requests"] == 3
    assert len(requests) == 3
    with sqlite3.connect(investigator.journal.path) as connection:
        saved = json.loads(connection.execute("SELECT payload FROM investigation_runs").fetchone()[0])
    assert saved == result


@pytest.mark.asyncio
async def test_tool_budget_stops_repeated_calls_before_io(tmp_path):
    investigator, _ = harness(tmp_path, lambda *_: call("service_health"), max_tool_calls=1)
    result = await run(investigator)
    assert result["status"] == "degraded"
    assert result["termination_reason"] == "tool_budget_exhausted"
    assert result["tool_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("restart_container", "{}"), ("service_health", '{"service":"mysql"}'),
])
async def test_unknown_tool_and_model_target_override_cannot_execute(tmp_path, name, args):
    tools = FakeTools()
    investigator, _ = harness(tmp_path, lambda *_: call(name, args), tools=tools)
    result = await run(investigator)
    assert result["status"] == "degraded"
    assert result["tool_calls"] == 0
    assert tools.restarted is None


@pytest.mark.asyncio
async def test_timeout_preserves_partial_tool_trace(tmp_path):
    class SlowTools(FakeTools):
        async def service_health(self, service):
            await asyncio.sleep(10)
    investigator, _ = harness(tmp_path, lambda *_: call("service_health"),
        tools=SlowTools(), timeout_seconds=0.05)
    result = await run(investigator)
    assert result["error_type"] == "TimeoutError"
    assert result["observations"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_max_turns_and_no_tool_answer_are_not_successful_investigation(tmp_path):
    investigator, _ = harness(tmp_path, lambda *_: call("service_health"), max_turns=1)
    assert (await run(investigator))["error_type"] == "MaxTurnsExceeded"
    investigator, _ = harness(tmp_path, lambda *_: {"role": "assistant", "content": "All fixed"})
    assert (await run(investigator))["status"] == "no_observations"


@pytest.mark.asyncio
async def test_sdk_failure_does_not_bypass_workflow_policy_or_approval(tmp_path):
    tools = FakeTools()
    investigator, _ = harness(tmp_path, lambda *_: call("restart_container"), tools=tools)
    workflow = IncidentWorkflow(tools, investigator=investigator)
    state = await workflow.run(AnalyzeRequest(execute=True, approved=False))
    assert state.status == "awaiting_approval"
    assert state.root_cause == "Redis dependency is unavailable"
    assert state.verified is None
    assert tools.restarted is None
    assert next(e for e in state.evidence if e.source == "llm_investigation").data["status"] == "degraded"


@pytest.mark.asyncio
async def test_busy_model_degrades_without_queueing_or_io(tmp_path):
    investigator, requests = harness(tmp_path, lambda *_: call("service_health"), timeout_seconds=0.03)
    await investigator.slot.acquire()
    try:
        result = await run(investigator)
    finally:
        investigator.slot.release()
    assert result["termination_reason"] == "model_busy"
    assert requests == []
    assert result["tool_calls"] == 0


@pytest.mark.asyncio
async def test_unknown_service_rejected_before_model_call(tmp_path):
    investigator, requests = harness(tmp_path, lambda *_: call("service_health"))
    with pytest.raises(ValueError, match="allowlisted"):
        await investigator.investigate(incident_id="test", service="mysql", symptom="down",
                                      incident_at=datetime.now(timezone.utc))
    assert requests == []


@pytest.mark.asyncio
async def test_busy_investigation_skips_all_downstream_model_calls_but_can_recover(tmp_path):
    class ForbiddenAnalyzer:
        async def analyze(self, **kwargs):
            pytest.fail("busy SDK must not enqueue RCA")
        async def explain_verification(self, **kwargs):
            pytest.fail("busy SDK must not enqueue verification narrative")
    tools = FakeTools()
    investigator, _ = harness(tmp_path, lambda *_: call("service_health"), tools=tools)
    await investigator.slot.acquire()
    try:
        state = await IncidentWorkflow(tools, investigator=investigator,
            incident_analyzer=ForbiddenAnalyzer()).run(AnalyzeRequest(execute=True, approved=True))
    finally:
        investigator.slot.release()
    assert state.status == "resolved"
    assert state.verified is True
    assert tools.restarted == "redis"


def test_context_compaction_is_deterministic_and_preserves_safety_categories():
    record = {
        "observations": [
            {"tool": "service_health", "status": "completed", "result": "healthy=false"},
            {"tool": "error_logs", "status": "failed", "error_type": "TimeoutError",
             "outcome": {"retryable": True}},
        ],
        "open_questions": ["Is the Redis failure still active?"],
    }
    first = compact_investigation_context(record, maximum_characters=20)
    second = compact_investigation_context(record, maximum_characters=20)

    assert first == second
    assert first["evidence"][0]["evidence_ref"].startswith("sha256:")
    assert first["counterevidence"][0]["status"] == "failed"
    assert first["action_outcomes"][0]["outcome"] == {"retryable": True}
    assert first["open_questions"] == ["Is the Redis failure still active?"]


@pytest.mark.asyncio
async def test_running_checkpoint_resumes_same_run_with_aggregate_budgets(tmp_path):
    investigator, requests = harness(
        tmp_path, lambda *_: {"role": "assistant", "content": "Prior evidence is sufficient."}
    )
    investigator.journal.save({
        "run_id": "resume-1", "incident_id": "incident-1", "service": "payment-service",
        "status": "running", "budget": investigator.budget.model_dump(), "model": "test",
        "incident_at": datetime.now(timezone.utc).isoformat(),
        "observations": [{"tool": "service_health", "status": "completed",
                          "result": '{"healthy": false}', "truncated": False}],
        "tool_calls": 1, "summary": None, "attempt": 1,
        "aggregate_usage": {"requests": 1, "input_tokens": 20, "output_tokens": 10,
                            "total_tokens": 30},
        "open_questions": ["Which dependency failed?"],
    })

    result = await run(investigator)

    assert result["run_id"] == "resume-1"
    assert result["attempt"] == 2
    assert result["recovered_from_checkpoint"] is True
    assert result["tool_calls"] == 1
    assert result["aggregate_usage"]["total_tokens"] == 60
    model_input = json.loads(requests[0]["messages"][1]["content"])
    assert model_input["resume_context"]["evidence"][0]["tool"] == "service_health"


@pytest.mark.asyncio
async def test_aggregate_token_budget_rejects_resume_before_model_call(tmp_path):
    investigator, requests = harness(tmp_path, lambda *_: pytest.fail("model must not run"),
                                      max_total_tokens=256)
    investigator.journal.save({
        "run_id": "spent", "incident_id": "incident-1", "service": "payment-service",
        "status": "running", "budget": investigator.budget.model_dump(), "model": "test",
        "incident_at": datetime.now(timezone.utc).isoformat(), "observations": [],
        "tool_calls": 0, "summary": None, "attempt": 1,
        "aggregate_usage": {"requests": 2, "input_tokens": 200, "output_tokens": 56,
                            "total_tokens": 256},
        "open_questions": [],
    })

    result = await run(investigator)

    assert result["termination_reason"] == "token_budget_exhausted"
    assert requests == []
