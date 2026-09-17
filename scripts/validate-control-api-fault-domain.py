#!/usr/bin/env python3
"""Run the Stage 10 same-host partition and PostgreSQL failover rehearsal."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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

    def timed_request(self, node: str, method: str, path: str) -> tuple[int, float]:
        started = time.monotonic()
        status, _ = self.request(node, method, path, timeout=5)
        return status, time.monotonic() - started

    def timed_analyze(self, node: str, incident_id: str) -> tuple[int, float]:
        started = time.monotonic()
        status = self.analyze(node, incident_id)
        return status, time.monotonic() - started

    def probe_isolated_node_from_survivor(
        self, survivor_container: str, incident_ids: list[str]
    ) -> dict[str, object]:
        """Probe the isolated API over the unaffected shared API network.

        Docker Desktop can temporarily drop host-port forwarding when a container
        network is detached. Running from the survivor keeps ingress on the shared
        `opspilot` network, so this measures database isolation instead of host NAT.
        """
        program = r'''
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request

base_url = "http://control-api:8080"
incident_ids = json.loads(sys.argv[1])

def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    item = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(item, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        status = 599
    return status, time.monotonic() - started

def analyze(incident_id):
    return request("POST", "/api/v1/incidents/analyze", {
        "incident_id": incident_id,
        "service": "payment-service",
        "symptom": f"database isolation probe {incident_id}",
        "execute": False,
        "approved": False,
    })

with concurrent.futures.ThreadPoolExecutor(max_workers=len(incident_ids) + 1) as executor:
    writes = [executor.submit(analyze, incident_id) for incident_id in incident_ids]
    health = executor.submit(request, "GET", "/health")
    write_results = [future.result() for future in writes]
    health_result = health.result()

print(json.dumps({"writes": write_results, "health": health_result}))
'''
        result = subprocess.run(
            ["docker", "exec", "-i", survivor_container, "python", "-", json.dumps(incident_ids)],
            cwd=ROOT,
            env=self.environment,
            check=True,
            text=True,
            input=program,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        return json.loads(result.stdout)

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
        partition_failed_ids = [
            str(uuid5(NAMESPACE_URL, f"{self.args.evaluation_id}:partition-failed:{index}"))
            for index in range(8)
        ]
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
        canary_container = self.container_id("control-api-canary")
        network = self.environment["STAGE10_NETWORK_NAME"]
        self.command(["docker", "network", "disconnect", network, primary_container])
        with ThreadPoolExecutor(max_workers=2) as executor:
            isolated_probe_future = executor.submit(
                self.probe_isolated_node_from_survivor,
                canary_container,
                partition_failed_ids,
            )
            survivor_future = executor.submit(self.analyze, canary_url, incident_ids[1])
            isolated_probe = isolated_probe_future.result()
            survivor_status = survivor_future.result()
        partition_results = isolated_probe["writes"]
        isolated_health_status, isolated_health_seconds = isolated_probe["health"]
        concurrent_partition_statuses = [status for status, _ in partition_results]
        partition_status = concurrent_partition_statuses[0]
        partition_failure_seconds = max(elapsed for _, elapsed in partition_results)
        self.event(
            "database_network_partitioned",
            isolated_node_status=partition_status,
            isolated_node_failure_seconds=round(partition_failure_seconds, 6),
            failure_target_seconds=3.0,
            concurrent_isolated_statuses=concurrent_partition_statuses,
            isolated_health_status=isolated_health_status,
            isolated_health_seconds=round(isolated_health_seconds, 6),
            ingress_probe="survivor-over-shared-api-network",
            survivor_write_status=survivor_status,
        )

        self.command(["docker", "network", "connect", network, primary_container])
        recovered_status = self.wait_request(
            primary_url, f"/api/v1/incidents/{incident_ids[1]}", attempts=30,
        )
        # The first failed batch exposed a 15-second lower-level resolver wait.
        # Wait beyond it before checking absence so a cancelled worker cannot be
        # mistaken for a safely rolled-back request.
        time.sleep(16)
        late_statuses = [
            self.request(
                canary_url, "GET", f"/api/v1/incidents/{incident_id}", timeout=5,
            )[0]
            for incident_id in partition_failed_ids
        ]
        partitioned_write_absent = all(status == 404 for status in late_statuses)
        self.event(
            "partition_healed",
            recovered_node_status=recovered_status,
            partitioned_write_statuses_after_heal=late_statuses,
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
            partitioned_node_failure_seconds=partition_failure_seconds,
            partitioned_node_failure_target_seconds=3.0,
            concurrent_partition_statuses=concurrent_partition_statuses,
            isolated_health_status=isolated_health_status,
            isolated_health_seconds=isolated_health_seconds,
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
