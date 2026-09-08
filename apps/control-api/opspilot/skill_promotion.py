"""Approval-gated promotion for diagnostic Skills.

Candidate content is evaluated in an isolated filesystem workspace.  It can add
diagnostic instructions and non-executable repair guidance, but cannot change the
server-owned probes, gates, case inputs, or expected labels.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _non_executable(value: str) -> str:
    forbidden = re.compile(
        r"(?:docker\s|kubectl\s|curl\s|sudo\s|/bin/|restart_container|stop_container|"
        r"approved\s*=|verified\s*=|target\s*=)",
        re.IGNORECASE,
    )
    cleaned = value.strip()
    if not cleaned or forbidden.search(cleaned):
        raise ValueError("Skill guidance must remain non-executable")
    return cleaned


class DiagnosticInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    when_all: list[str] = Field(min_length=1, max_length=8)
    conclude: str = Field(min_length=1, max_length=200)
    guidance: str = Field(min_length=1, max_length=1000)

    @field_validator("guidance")
    @classmethod
    def guidance_is_non_executable(cls, value: str) -> str:
        return _non_executable(value)

    @field_validator("when_all")
    @classmethod
    def terms_are_bounded(cls, terms: list[str]) -> list[str]:
        cleaned = [term.strip().lower() for term in terms]
        if any(not term or len(term) > 120 for term in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("diagnostic match terms must be unique and bounded")
        return cleaned


class RepairRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_cause: str = Field(min_length=1, max_length=200)
    guidance: list[str] = Field(min_length=1, max_length=8)

    @field_validator("guidance")
    @classmethod
    def guidance_is_non_executable(cls, values: list[str]) -> list[str]:
        return [_non_executable(value) for value in values]


class SkillContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostic_instructions: list[DiagnosticInstruction] = Field(min_length=1, max_length=16)
    repair_recipes: list[RepairRecipe] = Field(min_length=1, max_length=16)

    @field_validator("diagnostic_instructions")
    @classmethod
    def instruction_ids_are_unique(cls, values: list[DiagnosticInstruction]):
        if len({value.instruction_id for value in values}) != len(values):
            raise ValueError("diagnostic instruction IDs must be unique")
        return values


class FrozenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    suite: Literal["regression", "counterexample"]
    service: str
    trajectory: list[str]
    expected_root_cause: str | None


class SkillCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: str = Field(default="incident-diagnosis", pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    parent_version: int = Field(ge=1)
    trigger_case_id: str
    content: SkillContent


class SkillPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: str
    version: int = Field(ge=1)
    approved: bool


class SkillPromotionError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


DEFAULT_SKILL = SkillContent(
    diagnostic_instructions=[
        DiagnosticInstruction(
            instruction_id="redis-dependency-down",
            when_all=["dependency=redis", "value=0"],
            conclude="Redis dependency is unavailable",
            guidance="Treat a zero incident-time Redis dependency metric as supporting evidence.",
        ),
        DiagnosticInstruction(
            instruction_id="mysql-dependency-down",
            when_all=["dependency=mysql", "value=0"],
            conclude="MySQL dependency is unavailable",
            guidance="Treat a zero incident-time MySQL dependency metric as supporting evidence.",
        ),
    ],
    repair_recipes=[
        RepairRecipe(root_cause="Redis dependency is unavailable", guidance=[
            "Restore the allowlisted Redis dependency through the independent execution path.",
            "Confirm service health and the Redis dependency metric independently.",
        ]),
        RepairRecipe(root_cause="MySQL dependency is unavailable", guidance=[
            "Restore the allowlisted MySQL dependency through the independent execution path.",
            "Confirm service health and the MySQL dependency metric independently.",
        ]),
    ],
)


class SkillPromotionService:
    def __init__(self, store, cases_file: str, workspace_root: str):
        self.store = store
        self.cases_file = Path(cases_file)
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        raw = json.loads(self.cases_file.read_text())
        self.cases = [FrozenCase.model_validate(item) for item in raw]
        if not self.cases or {case.suite for case in self.cases} != {"regression", "counterexample"}:
            raise ValueError("Skill evaluation requires regression and counterexample cases")
        self.case_set_digest = _digest([case.model_dump(mode="json") for case in self.cases])
        self._seed_default()

    @property
    def _postgres(self) -> bool:
        return self.store.__class__.__name__ == "PostgresIncidentStore"

    def _encode(self, payload: dict):
        if self._postgres:
            from psycopg.types.json import Jsonb
            return Jsonb(payload)
        return _canonical(payload)

    @staticmethod
    def _decode(payload) -> dict:
        return json.loads(payload) if isinstance(payload, str) else dict(payload)

    def _seed_default(self):
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "status": "promoted", "content": DEFAULT_SKILL.model_dump(mode="json"),
            "content_digest": _digest(DEFAULT_SKILL.model_dump(mode="json")),
            "case_set_digest": self.case_set_digest, "origin": "server-baseline",
        }
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO skill_versions(skill_id,version,payload,parent_version,rollback_version,promoted_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(skill_id,version) DO NOTHING",
                ("incident-diagnosis", 1, self._encode(payload), None, None, now),
            )

    def list_cases(self) -> dict:
        return {
            "case_set_digest": self.case_set_digest,
            "cases": [case.model_dump(mode="json") for case in self.cases],
        }

    def _rows(self, skill_id: str) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT skill_id,version,payload,parent_version,rollback_version,promoted_at "
                "FROM skill_versions WHERE skill_id=? ORDER BY version", (skill_id,),
            ).fetchall()
        decoded = []
        for row in rows:
            payload = self._decode(row["payload"])
            promoted_at = str(row["promoted_at"]) if row["promoted_at"] else None
            decoded.append({
                **payload,
                "status": "promoted" if promoted_at else payload["status"],
                "skill_id": row["skill_id"], "version": row["version"],
                "parent_version": row["parent_version"],
                "rollback_version": row["rollback_version"], "promoted_at": promoted_at,
            })
        return decoded

    def list_versions(self, skill_id: str) -> list[dict]:
        return self._rows(skill_id)

    def active(self, skill_id: str = "incident-diagnosis") -> dict:
        promoted = [row for row in self._rows(skill_id) if row["promoted_at"]]
        if not promoted:
            raise SkillPromotionError("Skill has no promoted version", 404)
        return max(promoted, key=lambda row: row["version"])

    @staticmethod
    def _predict(content: SkillContent, case: FrozenCase) -> str | None:
        evidence = "\n".join(case.trajectory).lower()
        matches = [item.conclude for item in content.diagnostic_instructions
                   if all(term in evidence for term in item.when_all)]
        return matches[0] if len(set(matches)) == 1 else None

    @staticmethod
    def _render_skill(skill_id: str, version: int, parent_version: int | None,
                      content: SkillContent) -> str:
        lines = [
            "---", f"name: {skill_id}", f"version: {version}",
            f"parent_version: {parent_version if parent_version is not None else 'null'}",
            "authority: advisory-only", "---", "", "# Diagnostic instructions", "",
        ]
        for instruction in content.diagnostic_instructions:
            lines.extend([
                f"## {instruction.instruction_id}", "",
                f"Match all: {', '.join(instruction.when_all)}",
                f"Conclusion: {instruction.conclude}", instruction.guidance, "",
            ])
        lines.extend(["# Repair guidance", ""])
        for recipe in content.repair_recipes:
            lines.extend([f"## {recipe.root_cause}", ""])
            lines.extend(f"- {item}" for item in recipe.guidance)
            lines.append("")
        return "\n".join(lines)

    def _evaluate(self, content: SkillContent) -> dict:
        recipe_causes = {recipe.root_cause for recipe in content.repair_recipes}
        results = []
        for case in self.cases:
            actual = self._predict(content, case)
            passed = actual == case.expected_root_cause
            if actual is not None:
                passed = passed and actual in recipe_causes
            results.append({
                "case_id": case.case_id, "suite": case.suite, "passed": passed,
                "expected_root_cause": case.expected_root_cause, "actual_root_cause": actual,
            })
        suites = {name: {"passed": sum(1 for item in results if item["suite"] == name and item["passed"]),
                         "total": sum(1 for item in results if item["suite"] == name)}
                  for name in ("regression", "counterexample")}
        return {"passed": all(item["passed"] for item in results), "suites": suites,
                "results": results, "case_set_digest": self.case_set_digest,
                "evaluator": {"name": "deterministic-skill-evaluator", "version": 1,
                              "case_count": len(results)}}

    def create_candidate(self, request: SkillCandidateRequest) -> dict:
        active = self.active(request.skill_id)
        if request.parent_version != active["version"]:
            raise SkillPromotionError("candidate parent must be the active Skill version", 409)
        trigger = next((case for case in self.cases if case.case_id == request.trigger_case_id), None)
        if trigger is None:
            raise SkillPromotionError("trigger case is not in the frozen server-owned case set", 404)
        versions = self._rows(request.skill_id)
        version = max(row["version"] for row in versions) + 1
        candidate_id = str(uuid4())
        branch = f"codex/skill-{request.skill_id}-v{version}"
        content = request.content.model_dump(mode="json")
        if _digest(content) == active["content_digest"]:
            raise SkillPromotionError("candidate content must differ from its parent")
        workspace = self.workspace_root / candidate_id
        workspace.mkdir(mode=0o700)
        evaluation = self._evaluate(request.content)
        before_skill = self._render_skill(
            request.skill_id, active["version"], active["parent_version"],
            SkillContent.model_validate(active["content"]),
        )
        skill_artifact = self._render_skill(
            request.skill_id, version, active["version"], request.content,
        )
        before = before_skill.splitlines(keepends=True)
        after = skill_artifact.splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, fromfile=f"v{active['version']}",
                                            tofile=f"v{version}"))
        manifest = {
            "candidate_id": candidate_id, "skill_id": request.skill_id, "version": version,
            "parent_version": active["version"], "rollback_version": active["version"],
            "branch": branch, "workspace_id": candidate_id,
            "trigger_case": trigger.model_dump(mode="json"),
            "trigger_case_digest": _digest(trigger.model_dump(mode="json")),
            "case_set_digest": self.case_set_digest, "content": content,
            "content_digest": _digest(content), "skill_artifact_digest": _digest(skill_artifact),
            "diff": diff, "evaluation": evaluation,
            "status": "evaluated_passed" if evaluation["passed"] else "evaluated_failed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = workspace / "candidate.json"
        skill_path = workspace / "SKILL.md"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        skill_path.write_text(skill_artifact)
        manifest_path.chmod(0o400)
        skill_path.chmod(0o400)
        workspace.chmod(0o500)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO skill_versions(skill_id,version,payload,parent_version,rollback_version,promoted_at) "
                "VALUES(?,?,?,?,?,?)",
                (request.skill_id, version, self._encode(manifest), active["version"],
                 active["version"], None),
            )
        return manifest

    def promote(self, request: SkillPromotionRequest) -> dict:
        if not request.approved:
            raise SkillPromotionError("explicit promotion approval is required", 403)
        rows = self._rows(request.skill_id)
        candidate = next((row for row in rows if row["version"] == request.version), None)
        if candidate is None:
            raise SkillPromotionError("Skill candidate not found", 404)
        if candidate.get("status") != "evaluated_passed" or not candidate.get("evaluation", {}).get("passed"):
            raise SkillPromotionError("only a fully evaluated candidate can be promoted", 409)
        active = self.active(request.skill_id)
        if candidate["parent_version"] != active["version"]:
            raise SkillPromotionError("candidate parent is stale; regenerate and re-evaluate", 409)
        promoted_at = datetime.now(timezone.utc).isoformat()
        with self.store.connection() as db:
            db.execute("UPDATE skill_versions SET promoted_at=? WHERE skill_id=? AND version=?",
                       (promoted_at, request.skill_id, request.version))
        return self.active(request.skill_id)

    def active_instructions(self) -> tuple[int, str]:
        active = self.active()
        content = SkillContent.model_validate(active["content"])
        rendered = "\n".join(f"- {item.guidance}" for item in content.diagnostic_instructions)
        return active["version"], rendered
