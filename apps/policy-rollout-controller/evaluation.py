from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from opspilot.config import create_signed_verification_policy_bundle


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def bundle_bytes(key_id: str, key: str, revision: int, max_attempts: int) -> bytes:
    bundle = create_signed_verification_policy_bundle(
        {
            "defaults": {"max_attempts": max_attempts},
            "services": {"payment-service": {"recovery_stable_checks": 2}},
        },
        key_id,
        revision,
        key,
    )
    return (json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n").encode()


def prepare(args: argparse.Namespace) -> None:
    evidence = Path(args.evidence)
    if evidence.exists():
        raise SystemExit(f"evaluation output already exists: {evidence}")
    evidence.mkdir(parents=True)
    baseline = bundle_bytes(args.key_id, args.key, args.baseline_revision, 8)
    candidate = bundle_bytes(args.key_id, args.key, args.candidate_revision, 9)
    atomic_write(Path(args.rollout) / "stable.json", baseline)
    atomic_write(Path(args.rollout) / "canary.json", baseline)
    atomic_write(evidence / "candidate.json", candidate)
    atomic_write(
        evidence / "plan.json",
        (json.dumps({
            "evaluation_id": args.evaluation_id,
            "baseline_revision": args.baseline_revision,
            "candidate_revision": args.candidate_revision,
            "canary_nodes": ["control-api-canary"],
            "nodes": ["control-api-primary", "control-api-canary"],
            "quorum": 2,
            "approved": True,
        }, sort_keys=True, indent=2) + "\n").encode(),
    )


def read_audit(path: str) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def verify_interruption(args: argparse.Namespace) -> None:
    audit = read_audit(args.audit_file)
    canary = json.loads(Path(args.canary_bundle).read_text(encoding="utf-8"))
    stable = json.loads(Path(args.stable_bundle).read_text(encoding="utf-8"))
    events = [entry.get("event") for entry in audit]
    checks = {
        "crashed_after_durable_canary_acceptance": events
        == ["candidate_validated", "canary_published", "canary_accepted"],
        "canary_has_candidate": canary.get("revision") == args.candidate_revision,
        "stable_not_advanced": stable.get("revision") == args.baseline_revision,
        "plan_digest_present": all(
            isinstance(entry.get("plan_digest"), str)
            and entry["plan_digest"].startswith("sha256:")
            for entry in audit
        ),
    }
    report = {
        "passed": all(checks.values()),
        "checks": checks,
        "events": events,
        "canary_revision": canary.get("revision"),
        "stable_revision": stable.get("revision"),
    }
    output = Path(args.evidence) / "interruption-checkpoint.json"
    atomic_write(output, (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"output": str(output), **report}, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


async def verify(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        rollout_response = await client.get("http://control-api:8080/api/v1/verification-policy/rollout")
        rollout_response.raise_for_status()
        rollout = rollout_response.json()
        status_response = await client.get("http://control-api:8080/api/v1/verification-policy/status")
        status_response.raise_for_status()
        status = status_response.json()
        incident_response = await client.post(
            "http://control-api:8080/api/v1/incidents/analyze",
            json={
                "incident_id": f"policy-rollout-{args.evaluation_id}",
                "service": "payment-service",
                "symptom": "policy rollout recommendation-only probe",
                "execute": False,
                "approved": False,
            },
        )
        incident_response.raise_for_status()
        incident = incident_response.json()

    exact_nodes = [
        node["node_id"] for node in rollout.get("nodes", [])
        if node.get("accepted_revision") == args.candidate_revision
        and node.get("accepted_digest") == status.get("content_digest")
        and node.get("load_result") == "accepted"
    ]
    checks = {
        "rollout_converged": rollout.get("rollout_state") == "converged",
        "two_exact_nodes": len(exact_nodes) == 2,
        "strict_signature": status.get("signature_required") is True
        and status.get("signature_status") == "valid",
        "candidate_revision_active": status.get("bundle_revision") == args.candidate_revision,
        "recommendation_only": incident.get("execution_requested") is False
        and incident.get("execution_result") is None
        and incident.get("verified") is None,
    }
    report = {
        "evaluation_id": args.evaluation_id,
        "verified_at": time.time(),
        "passed": all(checks.values()),
        "checks": checks,
        "rollout": rollout,
        "policy_status": status,
        "incident_id": incident.get("incident_id"),
    }
    output = Path(args.evidence) / "verification.json"
    atomic_write(output, (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"output": str(output), **report}, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


async def verify_resume(args: argparse.Namespace) -> None:
    await verify(args)
    audit = read_audit(args.audit_file)
    events = [entry.get("event") for entry in audit]
    expected = [
        "candidate_validated",
        "canary_published",
        "canary_accepted",
        "rollout_resumed",
        "canary_revalidated",
        "stable_published",
        "quorum_reached",
    ]
    stable = json.loads(Path(args.stable_bundle).read_text(encoding="utf-8"))
    checks = {
        "durable_resume_sequence": events == expected,
        "candidate_validated_once": events.count("candidate_validated") == 1,
        "canary_published_once": events.count("canary_published") == 1,
        "stable_has_candidate": stable.get("revision") == args.candidate_revision,
        "one_plan_digest": len({entry.get("plan_digest") for entry in audit}) == 1,
    }
    report = {
        "passed": all(checks.values()),
        "checks": checks,
        "events": events,
        "stable_revision": stable.get("revision"),
    }
    output = Path(args.evidence) / "resume-verification.json"
    atomic_write(output, (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"output": str(output), **report}, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--evaluation-id", required=True)
    prepare_parser.add_argument("--evidence", required=True)
    prepare_parser.add_argument("--rollout", required=True)
    prepare_parser.add_argument("--key-id", required=True)
    prepare_parser.add_argument("--key", required=True)
    prepare_parser.add_argument("--baseline-revision", required=True, type=int)
    prepare_parser.add_argument("--candidate-revision", required=True, type=int)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--evaluation-id", required=True)
    verify_parser.add_argument("--evidence", required=True)
    verify_parser.add_argument("--candidate-revision", required=True, type=int)
    interruption_parser = subparsers.add_parser("verify-interruption")
    interruption_parser.add_argument("--evidence", required=True)
    interruption_parser.add_argument("--audit-file", required=True)
    interruption_parser.add_argument("--canary-bundle", required=True)
    interruption_parser.add_argument("--stable-bundle", required=True)
    interruption_parser.add_argument("--baseline-revision", required=True, type=int)
    interruption_parser.add_argument("--candidate-revision", required=True, type=int)
    resume_parser = subparsers.add_parser("verify-resume")
    resume_parser.add_argument("--evaluation-id", required=True)
    resume_parser.add_argument("--evidence", required=True)
    resume_parser.add_argument("--audit-file", required=True)
    resume_parser.add_argument("--stable-bundle", required=True)
    resume_parser.add_argument("--candidate-revision", required=True, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.command == "prepare":
        prepare(arguments)
    elif arguments.command == "verify-interruption":
        verify_interruption(arguments)
    elif arguments.command == "verify-resume":
        asyncio.run(verify_resume(arguments))
    else:
        asyncio.run(verify(arguments))
