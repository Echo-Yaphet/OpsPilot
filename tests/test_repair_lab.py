import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, prefix: str, monkeypatch):
    name = f"{prefix}_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIR_WORKSPACE", str(tmp_path / "lab"))
    monkeypatch.setenv("REPAIR_DATABASE", str(tmp_path / "repair.db"))
    monkeypatch.setenv("REPAIR_SANDBOX_TOKEN", "sandbox-token")
    monkeypatch.setenv("REPAIR_APPROVAL_KEY", "approval-key")
    monkeypatch.setenv("REPAIR_VALIDATOR_TOKEN", "validator-token")
    module = load_module(ROOT / "apps/repair-sandbox/app.py", "repair_sandbox", monkeypatch)
    module.startup()
    return module, TestClient(module.app), {"Authorization": "Bearer sandbox-token"}


def sign(module, package, **overrides):
    body = {
        "package_id": package["package_id"], "package_digest": package["package_digest"],
        "base_digest": package["base_digest"], "target": package["target"],
        "expires_at": int(time.time()) + 60, "jti": str(uuid4()),
    }
    body.update(overrides)
    body["signature"] = hmac.new(
        b"approval-key", module.canonical(body), hashlib.sha256,
    ).hexdigest()
    return body


async def validated(config):
    return {
        "passed": True, "config_digest": "sha256:candidate",
        "probe_version": "redis-config-probe-v1", "validated_at": "now",
        "observations": [{"probe": "redis_ping", "passed": True}],
    }


def test_workspace_starts_broken_and_shell_is_allowlisted(tmp_path, monkeypatch):
    _, client, headers = load_sandbox(tmp_path, monkeypatch)
    workspace = client.get("/v1/workspace", headers=headers).json()
    assert workspace["config"]["redis_url"] == "redis://redis.invalid:6379/0"
    assert client.post("/v1/shell", headers=headers, json={"command": "rm -rf /"}).status_code == 403
    result = client.post("/v1/shell", headers=headers,
                         json={"command": "resolve-configured-redis"}).json()
    assert result["exit_code"] != 0
    assert client.get("/v1/workspace").status_code == 401


def test_generated_diagnostic_script_has_server_owned_path_and_fixed_steps(tmp_path, monkeypatch):
    _, client, headers = load_sandbox(tmp_path, monkeypatch)
    invalid = client.post("/v1/scripts", headers=headers, json={
        "name": "../../escape", "steps": ["cat-shadow"],
    })
    assert invalid.status_code == 422
    script = client.post("/v1/scripts", headers=headers, json={
        "name": "redis-connectivity-check",
        "steps": ["show-config-digest", "resolve-configured-redis"],
    }).json()
    assert set(script) == {"script_id", "target", "name", "base_digest", "steps", "script_digest"}
    result = client.post(f"/v1/scripts/{script['script_id']}/run", headers=headers).json()
    assert [item["command"] for item in result["results"]] == script["steps"]
    assert client.post("/v1/scripts/../../etc/passwd/run", headers=headers).status_code in {404, 405}


def test_candidate_requires_current_base_and_independent_validation(tmp_path, monkeypatch):
    module, client, headers = load_sandbox(tmp_path, monkeypatch)
    workspace = client.get("/v1/workspace", headers=headers).json()

    async def failed(_config):
        return {"passed": False, "observations": []}
    monkeypatch.setattr(module, "validate_candidate", failed)
    request = {"base_digest": workspace["base_digest"], "config": {
        "service_version": "payment-lab-v1", "redis_url": "redis://evil:6379/0",
    }}
    assert client.post("/v1/candidates", headers=headers, json=request).status_code == 422
    request["base_digest"] = "sha256:stale"
    assert client.post("/v1/candidates", headers=headers, json=request).status_code == 409


def test_approval_is_bound_one_use_and_verified_before_apply(tmp_path, monkeypatch):
    module, client, headers = load_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "validate_candidate", validated)
    workspace = client.get("/v1/workspace", headers=headers).json()
    package = client.post("/v1/candidates", headers=headers, json={
        "base_digest": workspace["base_digest"], "config": {
            "service_version": "payment-lab-v1",
            "redis_url": "redis://repair-lab-redis:6379/0",
        },
    }).json()

    class Response:
        status_code = 200
        def json(self):
            return {"status": "ok"}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, _url): return Response()
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda timeout: Client())

    approval = sign(module, package)
    result = client.post(f"/v1/packages/{package['package_id']}/apply",
                         headers=headers, json=approval)
    assert result.status_code == 200
    assert result.json()["verified"] is True
    assert client.post(f"/v1/packages/{package['package_id']}/apply",
                       headers=headers, json=approval).status_code == 401


def test_tampered_or_stale_package_cannot_apply(tmp_path, monkeypatch):
    module, client, headers = load_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "validate_candidate", validated)
    workspace = client.get("/v1/workspace", headers=headers).json()
    package = client.post("/v1/candidates", headers=headers, json={
        "base_digest": workspace["base_digest"], "config": {
            "service_version": "payment-lab-v1",
            "redis_url": "redis://repair-lab-redis:6379/0",
        },
    }).json()
    bad = sign(module, package, package_digest="sha256:tampered")
    assert client.post(f"/v1/packages/{package['package_id']}/apply",
                       headers=headers, json=bad).status_code == 409
    module.atomic_write(module.active_path(), {
        "service_version": "payment-lab-v2", "redis_url": "redis://other:6379/0",
    })
    stale = sign(module, package)
    assert client.post(f"/v1/packages/{package['package_id']}/apply",
                       headers=headers, json=stale).status_code == 409


def test_failed_post_apply_probe_rolls_back(tmp_path, monkeypatch):
    module, client, headers = load_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "validate_candidate", validated)
    workspace = client.get("/v1/workspace", headers=headers).json()
    package = client.post("/v1/candidates", headers=headers, json={
        "base_digest": workspace["base_digest"], "config": {
            "service_version": "payment-lab-v1",
            "redis_url": "redis://repair-lab-redis:6379/0",
        },
    }).json()
    class Response:
        status_code = 503
        def json(self): return {"status": "degraded"}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, _url): return Response()
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda timeout: Client())
    response = client.post(f"/v1/packages/{package['package_id']}/apply",
                           headers=headers, json=sign(module, package))
    assert response.status_code == 409
    assert client.get("/v1/workspace", headers=headers).json()["config"] == module.BROKEN_CONFIG


def test_validator_enforces_identity_scope_and_ping(monkeypatch):
    monkeypatch.setenv("REPAIR_VALIDATOR_TOKEN", "validator-token")
    module = load_module(ROOT / "apps/repair-validator/app.py", "repair_validator", monkeypatch)
    client = TestClient(module.app)
    valid = {"service_version": "payment-lab-v1",
             "redis_url": "redis://repair-lab-redis:6379/0"}
    assert client.post("/v1/validate", json=valid).status_code == 401
    headers = {"Authorization": "Bearer validator-token"}
    invalid = {**valid, "redis_url": "redis://evil:6379/0"}
    assert client.post("/v1/validate", headers=headers, json=invalid).json()["passed"] is False
    wrong_version = {**valid, "service_version": "payment-lab-v2"}
    assert client.post("/v1/validate", headers=headers, json=wrong_version).json()["passed"] is False

    class Redis:
        async def ping(self): return True
        async def aclose(self): return None
    monkeypatch.setattr(module.redis, "from_url", lambda *_a, **_k: Redis())
    result = client.post("/v1/validate", headers=headers, json=valid).json()
    assert result["passed"] is True
    assert result["probe_version"] == "redis-config-probe-v1"
