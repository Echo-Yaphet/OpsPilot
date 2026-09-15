#!/usr/bin/env python3
"""Run repeated Redis+MySQL recovery trials against the live Compose stack."""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from opspilot import main
from opspilot.models import AnalyzeRequest
from opspilot.stage6_evaluation import (
    Stage6EvaluationPlan,
    Stage6TrialObservation,
    build_stage6_report,
    write_stage6_artifacts,
)
from opspilot.workflow import IncidentWorkflow


class LiveCombinedFaultEvaluation:
    def __init__(self, service: str):
        self.service = service

    async def _wait_healthy(self, attempts: int = 30):
        last = None
        for _ in range(attempts):
            try:
                health = await main.tools.service_health(self.service)
                metrics = await main.tools.query_metric(
                    f'dependency_up{{service="{self.service}"}}'
                )
                if (
                    health.get("healthy")
                    and len(metrics) == 2
                    and all(float(item.get("value", [0, 0])[1]) >= 1 for item in metrics)
                ):
                    return
                last = {"health": health, "metrics": metrics}
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1)
        raise RuntimeError(f"healthy baseline was not restored: {last}")

    async def cleanup(self):
        for target in ("redis", "mysql", self.service):
            if await main.tools.container_status(target) != "running":
                await main.tools.restart_container(target)
        await self._wait_healthy()

    async def inject(self):
        await main.tools.stop_container("redis")
        await main.tools.stop_container("mysql")
        last = {}
        for _ in range(30):
            await main.tools.service_health(self.service)
            observed = []
            for dependency in ("redis", "mysql"):
                metrics = await main.tools.query_metric(
                    f'dependency_up{{service="{self.service}",dependency="{dependency}"}}'
                )
                last[dependency] = metrics
                observed.append(
                    bool(metrics)
                    and any(float(item.get("value", [0, 1])[1]) == 0 for item in metrics)
                )
            if all(observed):
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"Prometheus did not observe both stopped dependencies: {last}")

    @staticmethod
    def workflow():
        return IncidentWorkflow(
            main.tools,
            executor=main.workflow.executor,
            knowledge_retriever=main.knowledge_retriever,
            default_verification_policy=main.settings.default_verification_policy(),
            verification_policies=main.settings.verification_policies(),
            verification_policy_provider=main.verification_policy_provider,
        )

    async def independent_probe(self, state):
        health = await main.tools.service_health(self.service)
        target_status = {
            target: await main.tools.container_status(target)
            for target in ("redis", "mysql")
        }
        dependency_up = {}
        for dependency in ("redis", "mysql"):
            metrics = await main.tools.query_metric(
                f'dependency_up{{service="{self.service}",dependency="{dependency}"}}'
            )
            dependency_up[dependency] = bool(metrics) and all(
                float(item.get("value", [0, 0])[1]) >= 1 for item in metrics
            )
        passed = (
            state.verified is True
            and health.get("healthy") is True
            and all(value == "running" for value in target_status.values())
            and all(dependency_up.values())
        )
        return {
            "passed": passed,
            "service_healthy": bool(health.get("healthy")),
            "target_status": target_status,
            "dependency_up": dependency_up,
        }

    async def run(self, repetition: int) -> Stage6TrialObservation:
        setup_started = time.monotonic()
        measured_started = None
        state = None
        injected = False
        try:
            await self.cleanup()
            await self.inject()
            injected = True
            measured_started = time.monotonic()
            state = await self.workflow().run(AnalyzeRequest(
                service=self.service,
                symptom="payment-service cannot reach Redis or MySQL",
                execute=True,
                approved=True,
            ))
            main.store.save(state, approved=True)
            policies = [item for item in state.evidence if item.source == "execution_policy"]
            execution = next(
                (item for item in state.evidence if item.source == "execution_plan"), None
            )
            execution_targets = [
                item["target"] for item in (execution.data.get("results", []) if execution else [])
            ]
            probe = await self.independent_probe(state)
            return Stage6TrialObservation(
                repetition=repetition,
                status="completed",
                latency_seconds=round(time.monotonic() - measured_started, 3),
                incident_id=state.incident_id,
                incident_status=state.status,
                root_cause=state.root_cause,
                recommendation_commands=[item.command for item in state.recommendations],
                policy_targets=[item.data.get("target") for item in policies],
                policy_allowed=[bool(item.data.get("allowed")) for item in policies],
                execution_targets=execution_targets,
                workflow_verified=state.verified,
                independent_probe=probe,
                failure_reason=None if probe["passed"] else "independent joint probe failed",
            )
        except Exception as exc:
            return Stage6TrialObservation(
                repetition=repetition,
                status="failed" if injected else "infrastructure_error",
                latency_seconds=round(
                    time.monotonic() - (measured_started or setup_started), 3
                ),
                incident_id=state.incident_id if state else None,
                incident_status=state.status if state else None,
                root_cause=state.root_cause if state else None,
                workflow_verified=state.verified if state else None,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                await self.cleanup()
            except Exception:
                pass


async def run(args):
    plan = Stage6EvaluationPlan(
        evaluation_id=args.evaluation_id,
        repetitions=args.repetitions,
    )
    checkpoint = Path(args.output) / f".{plan.evaluation_id}.in-progress.jsonl"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.touch(exist_ok=False)
    evaluator = LiveCombinedFaultEvaluation(plan.service)
    observations = []
    for repetition in range(1, plan.repetitions + 1):
        print(f"START {plan.evaluation_id}-r{repetition}", flush=True)
        observation = await evaluator.run(repetition)
        observations.append(observation)
        with checkpoint.open("a") as stream:
            stream.write(json.dumps(
                observation.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
            ) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(
            f"DONE {plan.evaluation_id}-r{repetition} status={observation.status} "
            f"verified={observation.workflow_verified}",
            flush=True,
        )
    report = build_stage6_report(plan, observations)
    output = write_stage6_artifacts(args.output, plan, report)
    completed_checkpoint = output / "checkpoint.jsonl"
    checkpoint.rename(completed_checkpoint)
    completed_checkpoint.chmod(0o444)
    print(json.dumps({
        "output": str(output),
        "plan_digest": report.plan_digest,
        "recovery_success": report.recovery_success,
        "recovery_success_wilson_95": report.recovery_success_wilson_95,
        "check_rates": report.check_rates,
        "mean_success_latency_seconds": report.mean_success_latency_seconds,
    }, indent=2))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/app/evaluation-output")
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
