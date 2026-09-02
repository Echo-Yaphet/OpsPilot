import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "scripts/runtime-log-vault-agent-controller.py"


def load_controller():
    spec = importlib.util.spec_from_file_location(
        "runtime_log_vault_agent_controller", CONTROLLER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def render_document(vault_version=7, bundle_version="runtime-log-v7"):
    data = {
        "bundle.json": json.dumps(
            {"version": bundle_version, "issuer": "test Vault PKI"}
        ),
        "ca.pem": "test-ca",
        "server-cert.pem": "test-server-cert",
        "server-key.pem": "test-server-key",
    }
    for service in ("user-service", "order-service", "payment-service"):
        data[f"{service}-cert.pem"] = f"test-{service}-cert"
        data[f"{service}-key.pem"] = f"test-{service}-key"
    return {"vault_kv_version": vault_version, "data": data}


def test_vault_agent_render_is_strict_and_materialized_as_immutable_snapshot(tmp_path):
    controller = load_controller()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(render_document()))

    parsed = controller.load_render(rendered)
    snapshot = controller.materialize_snapshot(parsed, tmp_path / "snapshots")

    assert parsed.vault_kv_version == 7
    assert parsed.bundle_version == "runtime-log-v7"
    assert (snapshot / "bundle.json").is_file()
    assert oct((snapshot / "server-key.pem").stat().st_mode & 0o777) == "0o600"
    changed = render_document()
    changed["data"]["server-cert.pem"] = "mutated"
    rendered.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="immutable Vault KV snapshot changed"):
        controller.materialize_snapshot(
            controller.load_render(rendered), tmp_path / "snapshots"
        )


def test_vault_agent_controller_rejects_rollback_and_same_revision_conflict(tmp_path):
    controller = load_controller()
    rendered = tmp_path / "rendered.json"
    rendered.write_text(json.dumps(render_document(vault_version=8)))
    accepted = controller.load_render(rendered)
    state = {
        "vault_kv_version": 8,
        "bundle_version": accepted.bundle_version,
        "digest": accepted.digest,
        "accepted_at": "2026-09-02T00:00:00+00:00",
    }

    assert controller.ensure_not_rollback(accepted, state) is False
    rendered.write_text(json.dumps(render_document(vault_version=7)))
    with pytest.raises(ValueError, match="rollback rejected"):
        controller.ensure_not_rollback(controller.load_render(rendered), state)
    conflicting = render_document(vault_version=8)
    conflicting["data"]["ca.pem"] = "different-ca"
    rendered.write_text(json.dumps(conflicting))
    with pytest.raises(ValueError, match="changed content"):
        controller.ensure_not_rollback(controller.load_render(rendered), state)


def test_vault_agent_controller_records_only_successful_apply(tmp_path, monkeypatch):
    controller = load_controller()
    rendered = tmp_path / "rendered.json"
    work_dir = tmp_path / "controller"
    secret_dir = tmp_path / "runtime-secrets"
    monkeypatch.setenv("RUNTIME_LOG_SECRET_DIR", str(secret_dir))
    rendered.write_text(json.dumps(render_document(vault_version=7)))
    commands = []
    monkeypatch.setattr(
        controller,
        "run_checked",
        lambda command, source, extra_environment=None: commands.append(command),
    )

    assert "Accepted Vault KV version 7" in controller.apply(rendered, work_dir)
    state = json.loads((work_dir / "accepted.json").read_text())
    assert state["vault_kv_version"] == 7
    assert commands[-1][-1].endswith("install-runtime-log-secrets.py")
    assert "already accepted" in controller.apply(rendered, work_dir)
    assert len(commands) == 1

    rendered.write_text(json.dumps(render_document(vault_version=8)))

    def fail_apply(*_args, **_kwargs):
        raise RuntimeError("rotation failed")

    monkeypatch.setattr(controller, "run_checked", fail_apply)
    with pytest.raises(RuntimeError, match="rotation failed"):
        controller.apply(rendered, work_dir)
    unchanged = json.loads((work_dir / "accepted.json").read_text())
    assert unchanged["vault_kv_version"] == 7


def test_vault_agent_contract_excludes_ca_private_key_and_uses_command_hook():
    template = (ROOT / "infra/vault-agent/runtime-log-bundle.ctmpl").read_text()
    config = (ROOT / "infra/vault-agent/runtime-log-agent.hcl.example").read_text()
    policy = (ROOT / "infra/vault-agent/runtime-log-policy.hcl").read_text()
    controller = CONTROLLER_PATH.read_text()

    assert ".Data.metadata.version" in template
    assert ".Data.data | toJSON" in template
    assert "apply-runtime-log-vault-agent-secret.sh" in config
    assert "remove_secret_id_file_after_reading = true" in config
    assert 'path "secret/data/opspilot/runtime-log"' in policy
    assert 'OPTIONAL_FILES = {"crl.pem"}' in controller
    assert "ca-key.pem" not in controller
