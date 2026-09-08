import json
import stat

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opspilot import main
from opspilot.skill_promotion import (
    DEFAULT_SKILL,
    SkillCandidateRequest,
    SkillContent,
    SkillPromotionError,
    SkillPromotionRequest,
    SkillPromotionService,
)
from opspilot.storage import IncidentStore


def service(tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([
        {"case_id": "redis-down", "suite": "regression", "service": "payment-service",
         "trajectory": ["dependency=redis value=0"],
         "expected_root_cause": "Redis dependency is unavailable"},
        {"case_id": "mysql-down", "suite": "regression", "service": "order-service",
         "trajectory": ["dependency=mysql value=0"],
         "expected_root_cause": "MySQL dependency is unavailable"},
        {"case_id": "redis-healthy", "suite": "counterexample", "service": "payment-service",
         "trajectory": ["redis was mentioned", "dependency=redis value=1"],
         "expected_root_cause": None},
    ]))
    return SkillPromotionService(
        IncidentStore(str(tmp_path / "control.db")), str(cases), str(tmp_path / "workspaces")
    )


def changed_content():
    content = DEFAULT_SKILL.model_copy(deep=True)
    content.diagnostic_instructions[0].guidance += " Preserve counterevidence."
    return content


def candidate(registry, content=None, trigger="redis-down", parent=1):
    return registry.create_candidate(SkillCandidateRequest(
        parent_version=parent, trigger_case_id=trigger, content=content or changed_content(),
    ))


def test_candidate_freezes_trigger_diff_parent_rollback_and_isolated_workspace(tmp_path):
    registry = service(tmp_path)
    created = candidate(registry)

    assert created["status"] == "evaluated_passed"
    assert created["evaluation"]["passed"] is True
    assert created["evaluation"]["suites"] == {
        "regression": {"passed": 2, "total": 2},
        "counterexample": {"passed": 1, "total": 1},
    }
    assert created["parent_version"] == created["rollback_version"] == 1
    assert created["trigger_case"]["case_id"] == "redis-down"
    assert created["trigger_case_digest"].startswith("sha256:")
    assert created["case_set_digest"] == registry.list_cases()["case_set_digest"]
    assert created["branch"] == "codex/skill-incident-diagnosis-v2"
    manifest = tmp_path / "workspaces" / created["workspace_id"] / "candidate.json"
    skill = manifest.parent / "SKILL.md"
    assert json.loads(manifest.read_text())["candidate_id"] == created["candidate_id"]
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o400
    assert stat.S_IMODE(skill.stat().st_mode) == 0o400
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o500
    assert "authority: advisory-only" in skill.read_text()
    assert created["skill_artifact_digest"].startswith("sha256:")


def test_promotion_is_explicit_and_rejects_failed_or_stale_candidates(tmp_path):
    registry = service(tmp_path)
    passing = candidate(registry)
    with pytest.raises(SkillPromotionError, match="explicit promotion approval"):
        registry.promote(SkillPromotionRequest(
            skill_id="incident-diagnosis", version=passing["version"], approved=False,
        ))
    promoted = registry.promote(SkillPromotionRequest(
        skill_id="incident-diagnosis", version=passing["version"], approved=True,
    ))
    assert promoted["version"] == 2
    assert promoted["rollback_version"] == 1

    with pytest.raises(SkillPromotionError, match="parent must be the active"):
        candidate(registry, parent=1)

    broken = changed_content()
    broken.diagnostic_instructions = [broken.diagnostic_instructions[0]]
    failed = candidate(registry, broken, parent=2)
    assert failed["status"] == "evaluated_failed"
    with pytest.raises(SkillPromotionError, match="fully evaluated"):
        registry.promote(SkillPromotionRequest(
            skill_id="incident-diagnosis", version=failed["version"], approved=True,
        ))


def test_candidate_cannot_edit_gates_probes_labels_or_add_executable_recipe(tmp_path):
    registry = service(tmp_path)
    payload = DEFAULT_SKILL.model_dump(mode="json")
    payload["evaluation_labels"] = {"redis-down": "forged"}
    with pytest.raises(ValidationError):
        SkillContent.model_validate(payload)
    payload = DEFAULT_SKILL.model_dump(mode="json")
    payload["repair_recipes"][0]["guidance"] = ["docker compose restart redis"]
    with pytest.raises(ValidationError, match="non-executable"):
        SkillContent.model_validate(payload)
    with pytest.raises(SkillPromotionError, match="frozen server-owned"):
        candidate(registry, trigger="attacker-authored-case")


def test_promoted_diagnostic_guidance_is_versioned_but_advisory(tmp_path):
    registry = service(tmp_path)
    version, guidance = registry.active_instructions()
    assert version == 1
    assert "zero incident-time Redis" in guidance
    assert "docker compose" not in guidance


def test_api_requires_identity_and_separate_explicit_promotion(tmp_path, monkeypatch):
    registry = service(tmp_path)
    monkeypatch.setattr(main, "skill_promotion", registry)
    monkeypatch.setattr(main.settings, "skill_promotion_token", "test-promotion-token")
    client = TestClient(main.app)
    request = {
        "parent_version": 1, "trigger_case_id": "redis-down",
        "content": changed_content().model_dump(mode="json"),
    }
    assert client.post("/api/v1/skills/candidates", json=request).status_code == 401
    headers = {"Authorization": "Bearer test-promotion-token"}
    created = client.post("/api/v1/skills/candidates", headers=headers, json=request)
    assert created.status_code == 201, created.text
    assert client.get("/api/v1/skills/incident-diagnosis/active").json()["version"] == 1
    denied = client.post("/api/v1/skills/promotions", headers=headers, json={
        "skill_id": "incident-diagnosis", "version": 2, "approved": False,
    })
    assert denied.status_code == 403
    promoted = client.post("/api/v1/skills/promotions", headers=headers, json={
        "skill_id": "incident-diagnosis", "version": 2, "approved": True,
    })
    assert promoted.status_code == 200
    assert promoted.json()["version"] == 2
