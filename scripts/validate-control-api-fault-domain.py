#!/usr/bin/env python3
"""Run the Stage 10 same-host partition and PostgreSQL failover rehearsal."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/control-api"))

from opspilot.fault_domain_evaluation import (  # noqa: E402
    FaultDomainObservation,
    FaultDomainPlan,
    build_fault_domain_report,
    write_fault_domain_artifacts,
)

BASE_COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]
STAGE_COMPOSE = BASE_COMPOSE + [
    "-f", "infra/postgres-failover/docker-compose.stage10.yml", "--profile", "active-active",
]


class Rehearsal:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        suffix = args.evaluation_id.replace("-", "_")[-40:]
        self.environment = os.environ.copy()
        self.environment.update({
            "STAGE10_NETWORK_NAME": f"opspilot-stage10-db-{suffix}",
            "STAGE10_PRIMARY_VOLUME": f"opspilot-stage10-primary-{suffix}",
            "STAGE10_STANDBY_VOLUME": f"opspilot-stage10-standby-{suffix}",
        })
        self.events: list[dict[str, object]] = []
        self.controls_recreated = False

    def event(self, phase: str, **details: object) -> None:
        item = {
            "at": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            **details,
        }
        self.events.append(item)
        print(json.dumps(item, sort_keys=True))

    def command(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=self.environment,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def compose(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.command(STAGE_COMPOSE + list(arguments), check=check)

    def base_compose(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.command(BASE_COMPOSE + list(arguments), check=check)

    def container_id(self, service: str) -> str:
        value = self.compose("ps", "-q", service).stdout.strip()
        if not value:
            raise RuntimeError(f"missing container for {service}")
        return value

    def request(
        self,
        node: str,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        timeout: float = 30,
    ) -> tuple[int, dict[str, object] | list[object] | str | None]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            node + path,
            data=data,
            method=method,
            headers={"content-type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                body: dict[str, object] | list[object] | str | None = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return 599, None

    def analyze(self, node: str, incident_id: str) -> int:
        status, _ = self.request(node, "POST", "/api/v1/incidents/analyze", {
            "incident_id": incident_id,
            "service": "payment-service",
            "symptom": f"Stage 10 recommendation-only probe {incident_id}",
            "execute": False,
            "approved": False,
        })
        return status

    def wait_request(self, node: str, path: str, expected: int = 200, attempts: int = 30) -> int:
        last = 599
        for _ in range(attempts):
            last, _ = self.request(node, "GET", path, timeout=5)
            if last == expected:
                return last
            time.sleep(1)
        return last

    def psql(self, service: str, query: str) -> str:
        result = self.compose(
            "exec", "-T", service,
            "psql", "-U", "opspilot_memory", "-d", "opspilot_memory", "-Atc", query,
        )
        return result.stdout.strip()

    def wait_replica_rows(self, incident_ids: list[str], attempts: int = 30) -> bool:
        literals = ",".join(f"'{value}'" for value in incident_ids)
        for _ in range(attempts):
            try:
                count = int(self.psql(
                    "memory-db-stage10-standby",
                    f"SELECT count(*) FROM incidents WHERE incident_id IN ({literals})",
                ))
                if count == len(incident_ids):
                    return True
            except (subprocess.CalledProcessError, ValueError):
                pass
            time.sleep(1)
        return False

    def cleanup(self) -> None:
        self.event("cleanup_started")
        self.compose("stop", "control-api", "control-api-canary", check=False)
        self.compose(
            "stop", "memory-db-stage10-standby", "memory-db-stage10-primary", check=False,
        )
        self.compose(
            "rm", "-f", "memory-db-stage10-standby", "memory-db-stage10-primary", check=False,
        )
        restore = self.base_compose(
            "--profile", "active-active", "up", "-d", "--force-recreate", "--wait",
            "control-api", "control-api-canary", check=False,
        )
        for volume_key in ("STAGE10_PRIMARY_VOLUME", "STAGE10_STANDBY_VOLUME"):
            self.command(
                ["docker", "volume", "rm", self.environment[volume_key]], check=False,
            )
        self.command(
            ["docker", "network", "rm", self.environment["STAGE10_NETWORK_NAME"]], check=False,
        )
        self.event("cleanup_finished", default_stack_restored=restore.returncode == 0)

    def run(self) -> tuple[FaultDomainPlan, FaultDomainObservation]:
        primary_url = "http://localhost:8080"
        canary_url = "http://localhost:18080"
        incident_ids = [
            str(uuid5(NAMESPACE_URL, f"{self.args.evaluation_id}:{phase}"))
            for phase in ("baseline", "survivor", "post-primary", "post-canary")
        ]
        partition_failed_id = str(uuid5(
            NAMESPACE_URL, f"{self.args.evaluation_id}:partition-failed"
        ))
        plan = FaultDomainPlan(
            evaluation_id=self.args.evaluation_id,
            nodes=["control-api-primary", "control-api-canary"],
            partitioned_node="control-api-primary",
            database_endpoint="stage10-memory-db:5432",
        )

        self.event("topology_starting")
        self.compose(
            "up", "-d", "--wait", "memory-db-stage10-primary", "memory-db-stage10-standby",
        )
        self.compose(
            "up", "-d", "--build", "--force-recreate", "--wait",
            "control-api", "control-api-canary",
        )
        self.controls_recreated = True
        if self.wait_request(primary_url, "/health") != 200:
            raise RuntimeError("primary Control API did not become healthy")
        if self.wait_request(canary_url, "/health") != 200:
            raise RuntimeError("canary Control API did not become healthy")
        self.event("topology_ready")

        baseline_status = self.analyze(primary_url, incident_ids[0])
        baseline_visible = (
            baseline_status == 200
            and self.wait_request(canary_url, f"/api/v1/incidents/{incident_ids[0]}") == 200
        )
        self.event("baseline_verified", cross_node_visible=baseline_visible)

        primary_container = self.container_id("control-api")
        network = self.environment["STAGE10_NETWORK_NAME"]
        self.command(["docker", "network", "disconnect", network, primary_container])
        partition_status = self.analyze(primary_url, partition_failed_id)
        survivor_status = self.analyze(canary_url, incident_ids[1])
        self.event(
            "database_network_partitioned",
            isolated_node_status=partition_status,
            survivor_write_status=survivor_status,
        )

        self.command(["docker", "network", "connect", network, primary_container])
        recovered_status = self.wait_request(
            primary_url, f"/api/v1/incidents/{incident_ids[1]}", attempts=30,
        )
        time.sleep(3)
        late_status, _ = self.request(
            canary_url, "GET", f"/api/v1/incidents/{partition_failed_id}", timeout=5,
        )
        partitioned_write_absent = late_status == 404
        self.event(
            "partition_healed",
            recovered_node_status=recovered_status,
            partitioned_write_status_after_heal=late_status,
        )

        replica_caught_up = self.wait_replica_rows(incident_ids[:2])
        if not replica_caught_up:
            raise RuntimeError("standby did not replay pre-failover incidents")
        self.event("replica_caught_up")

        primary_db = self.container_id("memory-db-stage10-primary")
        standby_db = self.container_id("memory-db-stage10-standby")
        self.compose("stop", "memory-db-stage10-primary")
        self.command(["docker", "network", "disconnect", "--force", network, primary_db])
        self.compose(
            "exec", "-T", "-u", "postgres", "memory-db-stage10-standby",
            "pg_ctl", "promote", "-D", "/var/lib/postgresql/data", "-w",
        )
        self.command(["docker", "network", "disconnect", network, standby_db])
        self.command([
            "docker", "network", "connect", "--alias", "stage10-memory-db",
            "--alias", "memory-db-stage10-standby", network, standby_db,
        ])
        promoted_writable = self.psql(
            "memory-db-stage10-standby", "SELECT NOT pg_is_in_recovery()",
        ) == "t"
        self.event("standby_promoted", writable=promoted_writable)

        primary_post = 599
        canary_post = 599
        for _ in range(30):
            primary_post = self.analyze(primary_url, incident_ids[2])
            if primary_post == 200:
                break
            time.sleep(1)
        for _ in range(30):
            canary_post = self.analyze(canary_url, incident_ids[3])
            if canary_post == 200:
                break
            time.sleep(1)
        cross_visible = (
            self.wait_request(canary_url, f"/api/v1/incidents/{incident_ids[2]}") == 200
            and self.wait_request(primary_url, f"/api/v1/incidents/{incident_ids[3]}") == 200
        )
        self.event(
            "post_failover_writes_verified",
            primary_status=primary_post,
            canary_status=canary_post,
            cross_node_visible=cross_visible,
        )

        literals = ",".join(f"'{value}'" for value in incident_ids)
        scoped_rows = int(self.psql(
            "memory-db-stage10-standby",
            f"SELECT count(*) FROM incidents WHERE incident_id IN ({literals})",
        ))
        executions = int(self.psql(
            "memory-db-stage10-standby",
            f"SELECT count(*) FROM executions WHERE incident_id IN ({literals})",
        ))
        verifications = int(self.psql(
            "memory-db-stage10-standby",
            f"SELECT count(*) FROM verifications WHERE incident_id IN ({literals})",
        ))
        self.event(
            "database_audit_verified",
            scoped_incidents=scoped_rows,
            executions=executions,
            verifications=verifications,
        )

        return plan, FaultDomainObservation(
            baseline_cross_node_visible=baseline_visible,
            partitioned_node_status=partition_status,
            partitioned_write_absent_after_heal=partitioned_write_absent,
            survivor_write_status=survivor_status,
            recovered_node_status=recovered_status,
            pre_failover_replica_caught_up=replica_caught_up,
            promoted_standby_writable=promoted_writable,
            primary_post_failover_write_status=primary_post,
            canary_post_failover_write_status=canary_post,
            post_failover_cross_node_visible=cross_visible,
            scoped_incident_rows=scoped_rows,
            execution_side_effect_rows=executions,
            verification_side_effect_rows=verifications,
            stable_endpoint_target="memory-db-stage10-standby",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--output", default=str(ROOT / "work/fault-domain-evaluations"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rehearsal = Rehearsal(args)
    try:
        plan, observation = rehearsal.run()
        report = build_fault_domain_report(plan, observation)
        output = write_fault_domain_artifacts(args.output, plan, report, rehearsal.events)
        print(json.dumps({
            "output": str(output),
            "passed": report.passed,
            "plan_digest": report.plan_digest,
            "checks": report.checks,
        }, indent=2))
        if not report.passed:
            raise SystemExit(1)
    finally:
        rehearsal.cleanup()


if __name__ == "__main__":
    main()
