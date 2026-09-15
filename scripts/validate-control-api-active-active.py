#!/usr/bin/env python3
"""Load two Control API nodes while validating their shared PostgreSQL state."""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx
import psycopg

from opspilot.active_active_evaluation import (
    ActiveActiveObservation,
    ActiveActivePlan,
    DatabaseObservation,
    RequestResult,
    build_active_active_report,
    write_active_active_artifacts,
)


async def captured_request(client, semaphore, gate, *, kind, node, method, path, payload=None,
                           expected_incident_id=None):
    await gate.wait()
    started = time.monotonic()
    try:
        async with semaphore:
            response = await client.request(method, node + path, json=payload)
        incident_id = None
        if response.status_code == 200:
            body = response.json()
            if kind == "unique_write":
                incident_id = body.get("incident_id")
            elif kind == "duplicate_alert" and body.get("incidents"):
                incident_id = body["incidents"][0].get("incident_id")
        return RequestResult(
            kind=kind, node=node, status_code=response.status_code,
            latency_seconds=round(time.monotonic() - started, 3), incident_id=incident_id,
            expected_incident_id=expected_incident_id,
            error=None if response.status_code == 200 else response.text[:500],
        )
    except Exception as exc:
        return RequestResult(
            kind=kind, node=node, latency_seconds=round(time.monotonic() - started, 3),
            expected_incident_id=expected_incident_id,
            error=f"{type(exc).__name__}: {exc}",
        )


def database_observation(dsn, incident_ids, prefix, fingerprint):
    with psycopg.connect(dsn) as db:
        unique_rows = db.execute(
            "SELECT count(*) FROM incidents WHERE incident_id LIKE %s", (prefix + "%",)
        ).fetchone()[0]
        alert_rows = db.execute(
            "SELECT count(*) FROM incidents WHERE alert_key=%s", (fingerprint,)
        ).fetchone()[0]
        mismatches = db.execute(
            """SELECT count(*) FROM incidents
               WHERE (incident_id LIKE %s OR alert_key=%s)
                 AND state_json::jsonb->>'incident_id' <> incident_id""",
            (prefix + "%", fingerprint),
        ).fetchone()[0]
        orphan_rows = 0
        for table in ("evidence", "agent_events", "recommendations", "policy_decisions"):
            orphan_rows += db.execute(
                f"""SELECT count(*) FROM {table} child
                    LEFT JOIN incidents parent ON parent.incident_id=child.incident_id
                    WHERE child.incident_id=ANY(%s) AND parent.incident_id IS NULL""",
                (incident_ids,),
            ).fetchone()[0]
        side_effect_rows = sum(
            db.execute(
                f"SELECT count(*) FROM {table} WHERE incident_id=ANY(%s)", (incident_ids,)
            ).fetchone()[0]
            for table in ("approvals", "executions", "verifications")
        )
    return DatabaseObservation(
        unique_incident_rows=unique_rows,
        duplicate_alert_rows=alert_rows,
        state_id_mismatches=mismatches,
        orphan_child_rows=orphan_rows,
        execution_side_effect_rows=side_effect_rows,
    )


async def run(args):
    nodes = [item.rstrip("/") for item in args.nodes.split(",") if item.strip()]
    plan = ActiveActivePlan(
        evaluation_id=args.evaluation_id, nodes=nodes, unique_writes=args.unique_writes,
        duplicate_deliveries=args.duplicate_deliveries, concurrent_reads=args.concurrent_reads,
        concurrency=args.concurrency,
    )
    limits = httpx.Limits(max_connections=plan.concurrency, max_keepalive_connections=plan.concurrency)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        health = {}
        for node in nodes:
            try:
                response = await client.get(node + "/health")
                health[node] = response.status_code == 200
            except Exception:
                health[node] = False

        prefix = f"aa-{plan.evaluation_id}-"
        expected_ids = [f"{prefix}{index:04d}" for index in range(plan.unique_writes)]
        fingerprint = f"active-active:{plan.evaluation_id}:duplicate"
        alert_payload = {
            "status": "firing",
            "alerts": [{
                "status": "firing", "fingerprint": fingerprint,
                "startsAt": datetime.now(timezone.utc).isoformat(),
                "labels": {"alertname": "ActiveActiveStoreProbe", "service": "payment-service"},
                "annotations": {"summary": f"active-active duplicate probe {plan.evaluation_id}"},
            }],
        }
        gate = asyncio.Event()
        semaphore = asyncio.Semaphore(plan.concurrency)
        tasks = []
        for index, incident_id in enumerate(expected_ids):
            tasks.append(captured_request(
                client, semaphore, gate, kind="unique_write", node=nodes[index % len(nodes)],
                method="POST", path="/api/v1/incidents/analyze", expected_incident_id=incident_id,
                payload={"incident_id": incident_id, "service": "payment-service",
                         "symptom": f"active-active store probe {incident_id}",
                         "execute": False, "approved": False},
            ))
        for index in range(plan.duplicate_deliveries):
            tasks.append(captured_request(
                client, semaphore, gate, kind="duplicate_alert", node=nodes[index % len(nodes)],
                method="POST", path="/api/v1/alertmanager/webhook", payload=alert_payload,
            ))
        for index in range(plan.concurrent_reads):
            tasks.append(captured_request(
                client, semaphore, gate, kind="concurrent_read", node=nodes[index % len(nodes)],
                method="GET", path="/api/v1/incidents?limit=20",
            ))
        started = time.monotonic()
        running = [asyncio.create_task(item) for item in tasks]
        gate.set()
        results = await asyncio.gather(*running)
        duration = max(time.monotonic() - started, 0.000001)

        duplicate_ids = {
            item.incident_id for item in results
            if item.kind == "duplicate_alert" and item.incident_id
        }
        visibility_passed = 0
        visibility_total = 0
        for index, incident_id in enumerate(expected_ids):
            opposite = nodes[(index + 1) % len(nodes)]
            visibility_total += 1
            try:
                response = await client.get(opposite + f"/api/v1/incidents/{incident_id}")
                visibility_passed += response.status_code == 200
            except Exception:
                pass
        duplicate_id = next(iter(duplicate_ids), None)
        if duplicate_id:
            for node in nodes:
                visibility_total += 1
                try:
                    response = await client.get(node + f"/api/v1/incidents/{duplicate_id}")
                    visibility_passed += response.status_code == 200
                except Exception:
                    pass

    scoped_ids = expected_ids + list(duplicate_ids)
    database = database_observation(args.database_url, scoped_ids, prefix, fingerprint)
    observation = ActiveActiveObservation(
        duration_seconds=duration, node_health=health, request_results=results,
        cross_node_visibility_passed=visibility_passed,
        cross_node_visibility_total=visibility_total, database=database,
    )
    report = build_active_active_report(plan, observation)
    output = write_active_active_artifacts(args.output, plan, report)
    print(json.dumps({
        "output": str(output), "passed": report.passed, "checks": report.checks,
        "requests": f"{report.successful_requests}/{report.request_count}",
        "throughput_requests_per_second": report.throughput_requests_per_second,
        "latency_p50_seconds": report.latency_p50_seconds,
        "latency_p95_seconds": report.latency_p95_seconds,
        "latency_max_seconds": report.latency_max_seconds,
        "database": database.model_dump(mode="json"),
    }, indent=2))
    if not report.passed:
        raise SystemExit(1)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--nodes", default="http://control-api:8080,http://control-api-canary:8080")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--output", default="/app/evaluation-output")
    parser.add_argument("--unique-writes", type=int, default=40)
    parser.add_argument("--duplicate-deliveries", type=int, default=16)
    parser.add_argument("--concurrent-reads", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
