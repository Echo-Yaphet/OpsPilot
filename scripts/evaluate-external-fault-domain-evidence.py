#!/usr/bin/env python3
"""Validate externally collected Stage 12 failure-domain evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/control-api"))

from opspilot.external_fault_domain_evaluation import (  # noqa: E402
    ExternalFaultDomainObservation,
    ExternalFaultDomainPlan,
    build_external_fault_domain_report,
    verify_external_evidence_files,
    write_external_fault_domain_artifacts,
)


def _load(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--observation", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument(
        "--output",
        default=str(ROOT / "work/external-fault-domain-evaluations"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plan = ExternalFaultDomainPlan.model_validate(_load(args.plan))
    observation = ExternalFaultDomainObservation.model_validate(_load(args.observation))
    verify_external_evidence_files(args.evidence_root, observation)
    report = build_external_fault_domain_report(
        plan,
        observation,
        raw_evidence_verified=True,
    )
    output = write_external_fault_domain_artifacts(args.output, plan, report)
    print(json.dumps({
        "output": str(output),
        "passed": report.passed,
        "plan_digest": report.plan_digest,
        "evidence_digest": report.evidence_digest,
        "checks": report.checks,
    }, indent=2))
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
