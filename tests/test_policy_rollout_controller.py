import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from opspilot.config import create_signed_verification_policy_bundle


SIGNING_KEY = "rollout-signing-key"
KEY_ID = "policy-v1"


def load_controller_module():
    path = Path("/app/policy-rollout-controller/controller.py")
    if not path.exists():
        path = Path("apps/policy-rollout-controller/controller.py")
    spec = importlib.util.spec_from_file_location("policy_rollout_controller_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_bundle(path: Path, revision: int, attempts: int = 8) -> dict:
    bundle = create_signed_verification_policy_bundle(
        {"defaults": {"max_attempts": attempts}, "services": {}},
        KEY_ID,
        revision,
        SIGNING_KEY,
    )
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return bundle


@pytest.mark.asyncio
async def test_controller_requires_approval_and_preserves_stable(tmp_path):
    module = load_controller_module()
    candidate = tmp_path / "candidate.json"
    stable = tmp_path / "stable.json"
    canary = tmp_path / "canary.json"
    write_bundle(candidate, 105)
    write_bundle(stable, 104)
    original = stable.read_bytes()

    controller = module.VerificationPolicyRolloutController(
        signing_keys={KEY_ID: SIGNING_KEY},
        nodes={"canary": "http://canary"},
        canary_nodes=("canary",),
        quorum=1,
        status_reader=lambda *_: None,
    )
    with pytest.raises(module.RolloutError, match="explicit rollout approval"):
        await controller.rollout(
            candidate_path=str(candidate),
            canary_path=str(canary),
            stable_path=str(stable),
            approved=False,
        )

    assert stable.read_bytes() == original
    assert not canary.exists()


@pytest.mark.asyncio
async def test_canary_rejection_never_changes_stable(tmp_path):
    module = load_controller_module()
    candidate = tmp_path / "candidate.json"
    stable = tmp_path / "stable.json"
    canary = tmp_path / "canary.json"
    audit = tmp_path / "audit.jsonl"
    write_bundle(candidate, 105)
    write_bundle(stable, 104)
    original = stable.read_bytes()

    async def rejected(*_):
        return {"load_result": "rejected", "bundle_revision": 104, "content_digest": "old"}

    controller = module.VerificationPolicyRolloutController(
        signing_keys={KEY_ID: SIGNING_KEY},
        nodes={"canary": "http://canary", "primary": "http://primary"},
        canary_nodes=("canary",),
        quorum=2,
        status_reader=rejected,
        timeout_seconds=0.01,
        poll_interval_seconds=0,
        audit_file=str(audit),
    )
    with pytest.raises(module.RolloutError, match="timed out"):
        await controller.rollout(
            candidate_path=str(candidate),
            canary_path=str(canary),
            stable_path=str(stable),
            approved=True,
        )

    assert canary.read_bytes() == candidate.read_bytes()
    assert stable.read_bytes() == original
    assert json.loads(audit.read_text().splitlines()[-1])["event"] == "rollout_failed"


@pytest.mark.asyncio
async def test_canary_then_stable_reaches_quorum_and_records_audit(tmp_path):
    module = load_controller_module()
    candidate = tmp_path / "candidate.json"
    stable = tmp_path / "stable.json"
    canary = tmp_path / "canary.json"
    audit = tmp_path / "audit.jsonl"
    bundle = write_bundle(candidate, 105)
    write_bundle(stable, 104)

    async def status(node, _url):
        path = canary if node == "canary" else stable
        active = module.validate_bundle(path.read_bytes(), {KEY_ID: SIGNING_KEY})
        return {
            "load_result": "accepted",
            "bundle_revision": active.revision,
            "content_digest": active.content_digest,
        }

    controller = module.VerificationPolicyRolloutController(
        signing_keys={KEY_ID: SIGNING_KEY},
        nodes={"canary": "http://canary", "primary": "http://primary"},
        canary_nodes=("canary",),
        quorum=2,
        status_reader=status,
        timeout_seconds=0.1,
        poll_interval_seconds=0,
        audit_file=str(audit),
    )
    result = await controller.rollout(
        candidate_path=str(candidate),
        canary_path=str(canary),
        stable_path=str(stable),
        approved=True,
    )

    assert result.state == "quorum_committed"
    assert result.accepted_nodes == ("canary", "primary")
    assert result.pending_nodes == ()
    assert stable.read_bytes() == candidate.read_bytes()
    assert json.loads(stable.read_text())["content_digest"] == bundle["content_digest"]
    events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
    assert events == [
        "candidate_validated",
        "canary_published",
        "canary_accepted",
        "stable_published",
        "quorum_reached",
    ]


@pytest.mark.asyncio
async def test_quorum_can_commit_with_a_pending_minority_node(tmp_path):
    module = load_controller_module()
    candidate = tmp_path / "candidate.json"
    stable = tmp_path / "stable.json"
    canary = tmp_path / "canary.json"
    write_bundle(candidate, 105)
    write_bundle(stable, 104)

    async def status(node, _url):
        if node == "primary":
            raise ConnectionError("partitioned")
        active = module.validate_bundle(canary.read_bytes(), {KEY_ID: SIGNING_KEY})
        return {
            "load_result": "accepted",
            "bundle_revision": active.revision,
            "content_digest": active.content_digest,
        }

    controller = module.VerificationPolicyRolloutController(
        signing_keys={KEY_ID: SIGNING_KEY},
        nodes={"canary": "http://canary", "primary": "http://primary"},
        canary_nodes=("canary",),
        quorum=1,
        status_reader=status,
        timeout_seconds=0.1,
        poll_interval_seconds=0,
    )
    result = await controller.rollout(
        candidate_path=str(candidate),
        canary_path=str(canary),
        stable_path=str(stable),
        approved=True,
    )

    assert result.state == "quorum_committed"
    assert result.accepted_nodes == ("canary",)
    assert result.pending_nodes == ("primary",)


def test_invalid_signature_and_conflicting_stable_revision_are_rejected(tmp_path):
    module = load_controller_module()
    candidate = tmp_path / "candidate.json"
    stable = tmp_path / "stable.json"
    write_bundle(candidate, 105)
    raw = json.loads(candidate.read_text())
    raw["signature"] = "hmac-sha256:" + "0" * 64
    candidate.write_text(json.dumps(raw))
    with pytest.raises(module.RolloutError, match="invalid bundle signature"):
        module.validate_bundle(candidate.read_bytes(), {KEY_ID: SIGNING_KEY})

    write_bundle(candidate, 105, attempts=9)
    write_bundle(stable, 105, attempts=8)
    controller = module.VerificationPolicyRolloutController(
        signing_keys={KEY_ID: SIGNING_KEY},
        nodes={"canary": "http://canary"},
        canary_nodes=("canary",),
        quorum=1,
        status_reader=lambda *_: asyncio.sleep(0),
    )
    with pytest.raises(module.RolloutError, match="conflicts with the stable digest"):
        controller._check_revision(
            stable, module.validate_bundle(candidate.read_bytes(), {KEY_ID: SIGNING_KEY})
        )
