import json

import pytest
from pydantic import ValidationError

from opspilot.llm import OllamaIncidentAnalyzer


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self.content}}


class FakeClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, json):
        self.captured.update({"url": url, "payload": json})
        return self.response


@pytest.mark.asyncio
async def test_ollama_planner_uses_schema_and_fixed_read_only_tool_catalog(monkeypatch):
    captured = {}
    response = FakeResponse(json.dumps({
        "objective": "Investigate Redis availability",
        "read_only_tools": ["service_health"],
        "knowledge_query": "payment-service Redis unavailable",
        "rationale": "Correlate dependency and health evidence.",
    }))
    monkeypatch.setattr(
        "opspilot.llm.httpx.AsyncClient", lambda timeout: FakeClient(response, captured)
    )

    analyzer = OllamaIncidentAnalyzer("http://ollama:11434", "gemma3:latest")
    plan = await analyzer.plan(service="payment-service", symptom="Redis unavailable")

    assert plan.read_only_tools == ["service_health"]
    assert captured["url"] == "http://ollama:11434/api/chat"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["think"] is False
    assert captured["payload"]["format"]["properties"]["read_only_tools"]
    user_context = json.loads(captured["payload"]["messages"][1]["content"])
    assert user_context["mandatory_baseline_tools"] == [
        "dependency_metrics", "cpu_metrics", "error_logs",
    ]
    assert user_context["available_additional_read_only_tools"] == [
        "service_health", "container_status",
    ]


@pytest.mark.asyncio
async def test_ollama_planner_rejects_tools_outside_the_typed_catalog(monkeypatch):
    response = FakeResponse(json.dumps({
        "objective": "Restart Redis",
        "read_only_tools": ["restart_container"],
        "knowledge_query": "Redis",
        "rationale": "Attempt an unsafe action.",
    }))
    monkeypatch.setattr(
        "opspilot.llm.httpx.AsyncClient", lambda timeout: FakeClient(response, {})
    )

    with pytest.raises(ValidationError):
        await OllamaIncidentAnalyzer("http://ollama:11434", "test").plan(
            service="payment-service", symptom="Redis unavailable"
        )


@pytest.mark.asyncio
async def test_ollama_analysis_exposes_current_metric_values_without_timestamps(monkeypatch):
    captured = {}
    response = FakeResponse(json.dumps({
        "root_cause": "Insufficient evidence",
        "confidence": 0.4,
        "rationale": "All supplied current dependency values are healthy.",
        "recommendation_title": "Continue observation",
        "candidates": [{
            "root_cause": "Insufficient evidence",
            "confidence": 0.4,
            "supporting_evidence": ["No failed dependency metric"],
            "opposing_evidence": [],
        }],
        "recommended_steps": ["Continue observing dependency health"],
    }))
    monkeypatch.setattr(
        "opspilot.llm.httpx.AsyncClient", lambda timeout: FakeClient(response, captured)
    )

    await OllamaIncidentAnalyzer("http://ollama:11434", "test").analyze(
        service="payment-service",
        symptom="dependency unavailable",
        metrics=[{"metric": {"dependency": "mysql"}, "value": [1234567890.0, "1"]}],
        cpu_metrics=[{"metric": {"service": "payment-service"}, "value": [1234567890.0, "0.01"]}],
        logs=[],
        deterministic_root_cause="Insufficient evidence",
        deterministic_confidence=0.45,
        runbooks=[],
        incident_history=[],
        investigation_plan=None,
        tool_observations=[],
    )

    user_context = json.loads(captured["payload"]["messages"][1]["content"])
    assert user_context["dependency_metrics"] == [{
        "labels": {"dependency": "mysql"}, "current_value": "1",
    }]
    assert user_context["cpu_metrics"] == [{
        "labels": {"service": "payment-service"}, "current_value": "0.01",
    }]


@pytest.mark.asyncio
async def test_ollama_thinking_mode_is_explicitly_configurable(monkeypatch):
    captured = {}
    response = FakeResponse(json.dumps({
        "objective": "Inspect service health",
        "read_only_tools": ["service_health"],
        "knowledge_query": "payment-service health",
        "rationale": "Check the supplied health signal.",
    }))
    monkeypatch.setattr(
        "opspilot.llm.httpx.AsyncClient", lambda timeout: FakeClient(response, captured)
    )

    analyzer = OllamaIncidentAnalyzer(
        "http://ollama:11434", "qwen3.5:9b", think=True,
    )
    await analyzer.plan(service="payment-service", symptom="degraded")

    assert captured["payload"]["think"] is True
