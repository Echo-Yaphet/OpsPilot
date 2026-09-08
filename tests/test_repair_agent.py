import hashlib
import hmac
import json

import httpx
import pytest
from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from fastapi.testclient import TestClient

from opspilot import main
from opspilot.repair import (
    RepairBudget,
    RepairError,
    RepairProposalStore,
    SDKRepairAgent,
)


def tool_call(name, arguments="{}"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": arguments},
    }]}


class FakeSandbox:
    def __init__(self):
        self.shell_calls = []
        self.scripts = []
        self.script_runs = []
        self.candidates = []
        self.approvals = []
        self.package = {
            "package_id": "package-1", "target": "repair-lab-payment",
            "base_digest": "sha256:base", "candidate_digest": "sha256:candidate",
            "candidate": {"service_version": "payment-lab-v1",
                          "redis_url": "redis://repair-lab-redis:6379/0"},
            "validation": {"passed": True}, "package_digest": "sha256:package",
        }

    async def workspace(self):
        return {"target": "repair-lab-payment", "base_digest": "sha256:base", "config": {
            "service_version": "payment-lab-v1", "redis_url": "redis://redis.invalid:6379/0",
        }}

    async def shell(self, command):
        self.shell_calls.append(command)
        return {"command": command, "exit_code": 2, "stderr": "not found"}

    async def create_script(self, name, steps):
        self.scripts.append((name, steps))
        return {"script_id": "11111111-1111-4111-8111-111111111111",
                "script_digest": "sha256:script", "name": name, "steps": steps}

    async def run_script(self, script_id):
        self.script_runs.append(script_id)
        return {"script_id": script_id, "results": [
            {"command": "show-config-digest", "exit_code": 0},
            {"command": "resolve-configured-redis", "exit_code": 2},
        ]}

    async def candidate(self, base_digest, config):
        self.candidates.append((base_digest, config))
        return self.package

    async def apply(self, package_id, approval):
        self.approvals.append((package_id, approval))
        return {"package_id": package_id, "status": "applied", "verified": True}


def harness(tmp_path, responder):
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        message = responder(payload, len(requests))
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
    sandbox = FakeSandbox()
    store = RepairProposalStore(str(tmp_path / "repair.db"))
    return SDKRepairAgent(sandbox, model, store, "approval-key", RepairBudget()), sandbox, requests


@pytest.mark.asyncio
async def test_sdk_repair_reads_diagnoses_and_proposes_without_applying(tmp_path):
    def respond(payload, turn):
        outputs = [message for message in payload["messages"] if message["role"] == "tool"]
        if turn == 1:
            return tool_call("read_workspace")
        if turn == 2:
            assert "redis.invalid" in outputs[-1]["content"]
            return tool_call("write_diagnostic_script", json.dumps({
                "name": "redis-connectivity-check",
                "steps": ["show-config-digest", "resolve-configured-redis"],
            }))
        if turn == 3:
            script_id = json.loads(outputs[-1]["content"])["script_id"]
            return tool_call("run_diagnostic_script", json.dumps({"script_id": script_id}))
        if turn == 4:
            assert '"exit_code": 2' in outputs[-1]["content"]
            return tool_call("write_candidate", json.dumps({
                "service_version": "payment-lab-v1",
                "redis_url": "redis://repair-lab-redis:6379/0",
            }))
        return {"role": "assistant", "content": "Validated package is ready for approval."}

    agent, sandbox, requests = harness(tmp_path, respond)
    result = await agent.propose("Redis DNS fails")
    assert result["status"] == "awaiting_approval"
    assert result["package"]["package_id"] == "package-1"
    assert len(result["trace"]) == 4
    assert sandbox.approvals == []
    assert sandbox.candidates == [("sha256:base", {
        "service_version": "payment-lab-v1",
        "redis_url": "redis://repair-lab-redis:6379/0",
    })]
    assert sandbox.scripts == [("redis-connectivity-check", [
        "show-config-digest", "resolve-configured-redis",
    ])]
    assert sandbox.script_runs == ["11111111-1111-4111-8111-111111111111"]
    assert len(requests) == 5


@pytest.mark.asyncio
async def test_human_approval_is_required_bound_and_one_use(tmp_path):
    agent, sandbox, _ = harness(tmp_path, lambda *_: {"role": "assistant", "content": "unused"})
    agent.proposals.save(sandbox.package)
    with pytest.raises(RepairError, match="explicit human approval"):
        await agent.approve("package-1", False)
    result = await agent.approve("package-1", True)
    assert result["verified"] is True
    _, approval = sandbox.approvals[0]
    unsigned = {key: value for key, value in approval.items() if key != "signature"}
    assert approval["target"] == "repair-lab-payment"
    assert approval["package_digest"] == "sha256:package"
    assert hmac.compare_digest(
        approval["signature"],
        hmac.new(b"approval-key", json.dumps(
            unsigned, sort_keys=True, separators=(",", ":")
        ).encode(), hashlib.sha256).hexdigest(),
    )
    with pytest.raises(RepairError, match="already applied"):
        await agent.approve("package-1", True)


@pytest.mark.asyncio
async def test_model_cannot_run_unlisted_diagnostic_or_apply(tmp_path):
    agent, sandbox, _ = harness(
        tmp_path, lambda *_: tool_call("write_diagnostic_script", json.dumps({
            "name": "redis-connectivity-check", "steps": ["rm -rf /"],
        })),
    )
    with pytest.raises(RepairError, match="repair agent failed"):
        await agent.propose("test")
    assert sandbox.shell_calls == []
    assert sandbox.approvals == []


@pytest.mark.asyncio
async def test_model_answer_without_validated_package_is_rejected(tmp_path):
    agent, sandbox, _ = harness(
        tmp_path, lambda *_: {"role": "assistant", "content": "Looks repaired."},
    )
    with pytest.raises(RepairError, match="no validated package"):
        await agent.propose("test")
    assert sandbox.approvals == []


def test_repair_http_routes_preserve_disabled_default_and_delegate(monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(main, "repair_agent", None)
    assert client.post("/api/v1/repair-lab/proposals", json={"symptom": "down"}).status_code == 503

    class Agent:
        async def propose(self, symptom):
            return {"status": "awaiting_approval", "symptom": symptom}
        async def approve(self, package_id, approved):
            if not approved:
                raise RepairError("explicit human approval is required", 403)
            return {"package_id": package_id, "verified": True}

    monkeypatch.setattr(main, "repair_agent", Agent())
    proposal = client.post("/api/v1/repair-lab/proposals", json={"symptom": "down"})
    assert proposal.status_code == 200
    assert proposal.json()["status"] == "awaiting_approval"
    denied = client.post("/api/v1/repair-lab/approvals", json={
        "package_id": "package-1", "approved": False,
    })
    assert denied.status_code == 403
    applied = client.post("/api/v1/repair-lab/approvals", json={
        "package_id": "package-1", "approved": True,
    })
    assert applied.json()["verified"] is True
