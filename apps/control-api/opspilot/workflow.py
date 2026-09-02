import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .config import VerificationPolicy
from .execution import ExecutionPolicy, Executor, PolicyDecision, RestrictedExecutor
from .knowledge import KnowledgeRetriever, NoopKnowledgeRetriever
from .llm import IncidentAnalyzer, InvestigationPlan, LLMAnalysis
from .models import AgentEvent, AgentName, AnalyzeRequest, Evidence, IncidentState, Recommendation, RiskLevel
from .tools import OpsTools


class VerificationPolicyResolver(Protocol):
    def policy_for(self, service: str) -> VerificationPolicy: ...


class WorkflowState(TypedDict):
    """Internal, inspectable graph state; IncidentState remains the public API."""

    incident: IncidentState
    request: AnalyzeRequest
    metrics: list[dict]
    cpu_metrics: list[dict]
    logs: list[str]
    target: str
    investigation_plan: InvestigationPlan | None
    tool_observations: list[dict]
    llm_analysis: LLMAnalysis | None
    policy_decision: PolicyDecision | None
    evidence_context: dict


class IncidentWorkflow:
    """LangGraph orchestration behind the stable IncidentWorkflow seam."""

    def __init__(
        self,
        tools: OpsTools,
        verification_attempts: int = 6,
        verification_interval: float = 2,
        execution_policy: ExecutionPolicy | None = None,
        executor: Executor | None = None,
        knowledge_retriever: KnowledgeRetriever | None = None,
        incident_analyzer: IncidentAnalyzer | None = None,
        verification_policies: Mapping[str, VerificationPolicy] | None = None,
        default_verification_policy: VerificationPolicy | None = None,
        verification_policy_provider: VerificationPolicyResolver | None = None,
    ):
        self.tools = tools
        # Keep the original constructor knobs compatible while moving runtime
        # configuration to validated per-service policies.
        self.verification_attempts = verification_attempts
        self.verification_interval = verification_interval
        self.default_verification_policy = default_verification_policy or VerificationPolicy(
            max_attempts=verification_attempts,
            check_interval_seconds=verification_interval,
        )
        self.verification_policies = dict(verification_policies or {})
        self.verification_policy_provider = verification_policy_provider
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.executor = executor or RestrictedExecutor(tools)
        self.knowledge_retriever = knowledge_retriever or NoopKnowledgeRetriever()
        self.incident_analyzer = incident_analyzer
        self.graph = self._build_graph()

    def event(self, state: IncidentState, agent: AgentName, message: str) -> None:
        state.events.append(AgentEvent(agent=agent, message=message))

    def _build_graph(self):
        builder = StateGraph(WorkflowState)
        builder.add_node("coordinator", self._coordinator)
        builder.add_node("monitor", self._monitor)
        builder.add_node("log", self._log)
        builder.add_node("rca", self._rca)
        builder.add_node("solution", self._solution)
        builder.add_node("safety", self._safety)
        builder.add_node("executor", self._executor)
        builder.add_node("verification", self._verification)

        builder.add_edge(START, "coordinator")
        builder.add_edge("coordinator", "monitor")
        builder.add_edge("monitor", "log")
        builder.add_edge("log", "rca")
        builder.add_conditional_edges(
            "rca",
            self._route_after_rca,
            {"conclusive": "solution", "insufficient_evidence": "solution"},
        )
        builder.add_edge("solution", "safety")
        builder.add_conditional_edges(
            "safety",
            self._route_after_safety,
            {
                "recommendation_only": "executor",
                "approval_required": "executor",
                "approved_execution": "executor",
            },
        )
        builder.add_conditional_edges(
            "executor",
            self._route_after_executor,
            {"verify": "verification", "deferred": "verification", "blocked": "verification"},
        )
        builder.add_conditional_edges(
            "verification",
            self._route_after_verification,
            {"resolved": END, "failed": END, "not_executed": END},
        )
        return builder.compile(checkpointer=InMemorySaver())

    async def _coordinator(self, graph_state: WorkflowState) -> dict:
        state, request = graph_state["incident"], graph_state["request"]
        plan = None
        observations = []
        if self.incident_analyzer is not None:
            try:
                plan = await self.incident_analyzer.plan(
                    service=request.service, symptom=request.symptom,
                )
                for tool_name in plan.read_only_tools:
                    if tool_name == "service_health":
                        try:
                            result = await self.tools.service_health(request.service)
                        except Exception as exc:
                            result = {"error": str(exc)[:500]}
                        observations.append({"tool": tool_name, "result": result})
                    elif tool_name == "container_status":
                        try:
                            result = await self.tools.container_status(request.service)
                        except Exception as exc:
                            result = {"error": str(exc)[:500]}
                        observations.append({"tool": tool_name, "result": result})
                state.evidence.append(Evidence(
                    source="llm_investigation",
                    summary=f"bounded investigation plan from {self.incident_analyzer.name}",
                    data={
                        "model": self.incident_analyzer.name,
                        "plan": plan.model_dump(mode="json"),
                        "tool_observations": observations,
                        "mandatory_baseline_tools": [
                            "dependency_metrics", "cpu_metrics", "error_logs",
                        ],
                    },
                ))
                self.event(
                    state, AgentName.COORDINATOR,
                    f"Local LLM planned {len(plan.read_only_tools)} bounded read-only probe(s)",
                )
            except Exception as exc:
                state.evidence.append(Evidence(
                    source="llm_investigation",
                    summary="local LLM planning unavailable; mandatory investigation retained",
                    data={
                        "model": self.incident_analyzer.name,
                        "error_type": type(exc).__name__, "error": str(exc)[:500],
                    },
                ))
                self.event(state, AgentName.COORDINATOR, "Started mandatory metrics and log investigation")
        else:
            self.event(state, AgentName.COORDINATOR, "Started metrics and log investigation")
        return {
            "incident": state, "investigation_plan": plan,
            "tool_observations": observations,
        }

    async def _monitor(self, graph_state: WorkflowState) -> dict:
        state, request = graph_state["incident"], graph_state["request"]
        metric_query = f'dependency_up{{service="{request.service}"}}'
        cpu_query = f'container_cpu_usage_ratio{{service="{request.service}"}}'
        query_at = graph_state["evidence_context"]["incident_at_value"]
        query_metric_at = getattr(self.tools, "query_metric_at", None)

        async def query(query: str, summary: str) -> list[dict]:
            try:
                if query_metric_at is None:
                    graph_state["evidence_context"]["prometheus"]["mode"] = "current_fallback"
                    return await self.tools.query_metric(query)
                return await query_metric_at(query, query_at)
            except Exception as exc:
                state.evidence.append(Evidence(
                    source="prometheus", summary=f"{summary} query unavailable", data=str(exc),
                ))
                return []

        metrics, cpu_metrics = await asyncio.gather(
            query(metric_query, "dependency metrics"),
            query(cpu_query, "container CPU metrics"),
        )
        state.evidence.append(Evidence(source="prometheus", summary="dependency health metrics", data=metrics))
        state.evidence.append(Evidence(
            source="prometheus", summary="container CPU usage metrics", data=cpu_metrics,
        ))
        self.event(
            state, AgentName.MONITOR,
            f"Collected {len(metrics)} dependency and {len(cpu_metrics)} container CPU series",
        )
        return {"incident": state, "metrics": metrics, "cpu_metrics": cpu_metrics}

    async def _log(self, graph_state: WorkflowState) -> dict:
        state, request = graph_state["incident"], graph_state["request"]
        try:
            context = graph_state["evidence_context"]
            query_logs_between = getattr(self.tools, "query_logs_between", None)
            if query_logs_between is None:
                logs = await self.tools.query_logs(request.service)
                context["loki"]["mode"] = "recent_fallback"
            else:
                logs = await query_logs_between(
                    request.service, context["window_start_value"], context["window_end_value"]
                )
        except Exception as exc:
            logs = [f"log query unavailable: {exc}"]
        state.evidence.append(Evidence(source="loki", summary="recent error logs", data=logs[:20]))
        public_context = {
            key: value for key, value in graph_state["evidence_context"].items()
            if not key.endswith("_value")
        }
        public_context["prometheus"]["series_count"] = (
            len(graph_state["metrics"]) + len(graph_state["cpu_metrics"])
        )
        public_context["loki"]["line_count"] = len(logs)
        state.evidence.append(Evidence(
            source="incident_context", summary="incident-time evidence correlation",
            data=public_context,
        ))
        self.event(state, AgentName.LOG, f"Collected {len(logs)} relevant log lines")
        return {"incident": state, "logs": logs}

    async def _rca(self, graph_state: WorkflowState) -> dict:
        state, request = graph_state["incident"], graph_state["request"]
        metrics, logs = graph_state["metrics"], graph_state["logs"]
        dependency_metrics = {
            dependency: [
                item
                for item in metrics
                if item.get("metric", {}).get("dependency") == dependency
            ]
            for dependency in ("redis", "mysql")
        }

        def dependency_is_down(dependency: str) -> bool:
            matching_metrics = dependency_metrics[dependency]
            if matching_metrics:
                return any(float(item.get("value", [0, 1])[1]) == 0 for item in matching_metrics)
            return any(
                dependency in line.lower()
                and any(word in line.lower() for word in ("failed", "refused", "error"))
                for line in logs
            )

        redis_down = dependency_is_down("redis")
        mysql_down = dependency_is_down("mysql")
        high_cpu = any(
            item.get("metric", {}).get("__name__") == "container_cpu_usage_ratio"
            and float(item.get("value", [0, 0])[1]) > 0.8
            for item in graph_state["cpu_metrics"]
        )

        if redis_down:
            state.root_cause, state.confidence = "Redis dependency is unavailable", 0.92
            target = "redis"
        elif mysql_down:
            state.root_cause, state.confidence = "MySQL dependency is unavailable", 0.9
            target = "mysql"
        elif high_cpu:
            state.root_cause, state.confidence = "Container CPU usage is high", 0.9
            target = request.service
        else:
            state.root_cause, state.confidence = "Insufficient evidence; dependency or application degradation suspected", 0.45
            target = request.service
        runbooks = self.knowledge_retriever.retrieve_runbooks(
            request.service, request.symptom, state.root_cause
        )
        plan = graph_state.get("investigation_plan")
        if plan is not None and plan.knowledge_query != request.symptom:
            expanded = self.knowledge_retriever.retrieve_runbooks(
                request.service, plan.knowledge_query, state.root_cause
            )
            seen = {item.runbook_id for item in runbooks}
            runbooks.extend(item for item in expanded if item.runbook_id not in seen)
            runbooks = runbooks[:3]
        history = self.knowledge_retriever.retrieve_incidents(state)
        state.evidence.append(Evidence(
            source="runbook", summary="deterministic runbook retrieval",
            data=[item.model_dump(mode="json") for item in runbooks],
        ))
        state.evidence.append(Evidence(
            source="incident_history", summary="similar historical incident retrieval",
            data=[item.model_dump(mode="json") for item in history],
        ))
        llm_analysis = None
        if self.incident_analyzer is not None:
            baseline_root_cause = state.root_cause
            baseline_confidence = state.confidence
            try:
                llm_analysis = await self.incident_analyzer.analyze(
                    service=request.service,
                    symptom=request.symptom,
                    metrics=metrics,
                    cpu_metrics=graph_state["cpu_metrics"],
                    logs=logs,
                    deterministic_root_cause=baseline_root_cause,
                    deterministic_confidence=baseline_confidence,
                    runbooks=[item.model_dump(mode="json") for item in runbooks],
                    incident_history=[item.model_dump(mode="json") for item in history],
                    investigation_plan=plan.model_dump(mode="json") if plan else None,
                    tool_observations=graph_state.get("tool_observations", []),
                )
                # Known failure signatures remain authoritative. For an inconclusive
                # baseline, the model may improve the user-facing candidate but cannot
                # promote it into an automatically trusted conclusion.
                used_for_state = baseline_confidence < 0.8
                if used_for_state:
                    state.root_cause = llm_analysis.root_cause
                    state.confidence = min(llm_analysis.confidence, 0.79)
                state.evidence.append(Evidence(
                    source="llm_analysis",
                    summary=f"local LLM-assisted RCA from {self.incident_analyzer.name}",
                    data={
                        **llm_analysis.model_dump(mode="json"),
                        "model": self.incident_analyzer.name,
                        "used_for_state": used_for_state,
                        "deterministic_baseline": {
                            "root_cause": baseline_root_cause,
                            "confidence": baseline_confidence,
                        },
                    },
                ))
            except Exception as exc:
                state.evidence.append(Evidence(
                    source="llm_analysis",
                    summary="local LLM unavailable; deterministic RCA retained",
                    data={
                        "model": self.incident_analyzer.name,
                        "error_type": type(exc).__name__, "error": str(exc)[:500],
                    },
                ))
        self.event(state, AgentName.RCA, f"Root cause: {state.root_cause} ({state.confidence:.0%})")
        return {"incident": state, "target": target, "llm_analysis": llm_analysis}

    def _route_after_rca(self, graph_state: WorkflowState) -> Literal["conclusive", "insufficient_evidence"]:
        return "conclusive" if graph_state["incident"].confidence >= 0.8 else "insufficient_evidence"

    async def _solution(self, graph_state: WorkflowState) -> dict:
        state, target = graph_state["incident"], graph_state["target"]
        runbook_evidence = next((item for item in state.evidence if item.source == "runbook"), None)
        matched_runbook = runbook_evidence.data[0] if runbook_evidence and runbook_evidence.data else None
        command = matched_runbook.get("command") if matched_runbook else None
        if command is None:
            command = f"docker compose restart {target}"
        llm_analysis = graph_state.get("llm_analysis")
        title = llm_analysis.recommendation_title if llm_analysis else None
        if title is None:
            title = matched_runbook.get("title") if matched_runbook else None
        if title is None and state.root_cause == "Container CPU usage is high":
            title = f"Restart {target} and verify container CPU usage"
        if llm_analysis is not None:
            state.evidence.append(Evidence(
                source="llm_solution",
                summary=f"non-executable remediation plan from {self.incident_analyzer.name}",
                data={
                    "model": self.incident_analyzer.name,
                    "title": llm_analysis.recommendation_title,
                    "steps": llm_analysis.recommended_steps,
                    "execution_target_source": "deterministic_policy",
                    "command_source": "deterministic_runbook_or_allowlist",
                },
            ))
        state.recommendations.append(Recommendation(
            title=title or f"Restart {target} and verify dependency metrics", command=command,
            risk=RiskLevel.MEDIUM, requires_approval=True,
        ))
        self.event(state, AgentName.SOLUTION, f"Prepared recovery plan for {target}")
        return {"incident": state}

    async def _safety(self, graph_state: WorkflowState) -> dict:
        state = graph_state["incident"]
        command = state.recommendations[0].command if state.recommendations else None
        decision = self.execution_policy.evaluate(command)
        state.evidence.append(Evidence(
            source="execution_policy", summary="execution allowlist decision", data=decision.audit_data(),
        ))
        self.event(
            state,
            AgentName.SAFETY,
            f"Restart is medium risk and requires explicit approval; policy {decision.reason}",
        )
        return {"incident": state, "policy_decision": decision}

    def _route_after_safety(
        self, graph_state: WorkflowState
    ) -> Literal["recommendation_only", "approval_required", "approved_execution"]:
        request = graph_state["request"]
        if not request.execute:
            return "recommendation_only"
        return "approved_execution" if request.approved else "approval_required"

    async def _executor(self, graph_state: WorkflowState) -> dict:
        state, request, target = graph_state["incident"], graph_state["request"], graph_state["target"]
        if request.execute:
            if not request.approved:
                state.status = "awaiting_approval"
                state.execution_result = "blocked: approval required"
                self.event(state, AgentName.EXECUTOR, state.execution_result)
            else:
                decision = graph_state["policy_decision"]
                if decision is None or not decision.allowed or decision.action is None:
                    reason = decision.reason if decision else "denied: no policy decision"
                    state.status = "execution_denied"
                    state.execution_result = reason
                    self.event(state, AgentName.EXECUTOR, reason)
                    return {"incident": state}
                try:
                    state.execution_result = await self.executor.execute(decision.action)
                    self.event(state, AgentName.EXECUTOR, state.execution_result)
                except Exception as exc:
                    state.status = "execution_failed"
                    state.execution_result = f"failed: {exc}"
                    self.event(state, AgentName.EXECUTOR, state.execution_result)
        else:
            state.status = "recommendation_ready"
            self.event(state, AgentName.EXECUTOR, "No action executed in recommendation-only mode")
        return {"incident": state}

    def _route_after_executor(self, graph_state: WorkflowState) -> Literal["verify", "deferred", "blocked"]:
        request = graph_state["request"]
        if not request.execute:
            return "deferred"
        decision = graph_state["policy_decision"]
        return "verify" if (
            request.approved and decision and decision.allowed
            and graph_state["incident"].status != "execution_failed"
        ) else "blocked"

    async def _verification(self, graph_state: WorkflowState) -> dict:
        state, request, target = graph_state["incident"], graph_state["request"], graph_state["target"]
        decision = graph_state["policy_decision"]
        if (
            request.execute and request.approved and decision and decision.allowed
            and state.status != "execution_failed"
        ):
            result = await self._poll_recovery(request.service, target)
            state.verified = result["verified"]
            state.status = "resolved" if state.verified else "verification_failed"
            state.evidence.append(Evidence(
                source="verification", summary="bounded service recovery check", data=result,
            ))
            if self.incident_analyzer is not None:
                try:
                    explanation = await self.incident_analyzer.explain_verification(
                        service=request.service, target=target, result=result,
                    )
                    state.evidence.append(Evidence(
                        source="llm_verification",
                        summary=f"verification explanation from {self.incident_analyzer.name}",
                        data={
                            **explanation.model_dump(mode="json"),
                            "model": self.incident_analyzer.name,
                            "deterministic_verified": result["verified"],
                        },
                    ))
                except Exception as exc:
                    state.evidence.append(Evidence(
                        source="llm_verification",
                        summary="local LLM verification explanation unavailable",
                        data={
                            "model": self.incident_analyzer.name,
                            "error_type": type(exc).__name__, "error": str(exc)[:500],
                        },
                    ))
            self.event(state, AgentName.VERIFICATION, result["message"])
        elif request.execute:
            if state.status == "execution_failed":
                reason = "executor gateway failure"
            else:
                reason = "execution approval" if not request.approved else "execution policy"
            self.event(state, AgentName.VERIFICATION, f"Verification blocked by {reason}")
        else:
            self.event(state, AgentName.VERIFICATION, "Verification deferred until execution")
        return {"incident": state}

    async def _poll_recovery(self, service: str, target: str) -> dict:
        """Require the repaired container, service health, and dependency metric to recover."""
        # Resolve once so one incident uses an immutable policy snapshot even if
        # the centrally managed file changes while checks are in progress.
        policy = (
            self.verification_policy_provider.policy_for(service)
            if self.verification_policy_provider
            else self.verification_policies.get(service, self.default_verification_policy)
        )
        last = {"container_status": "unknown", "service_healthy": False, "dependency_up": False}
        stable_checks = 0
        for attempt in range(1, policy.max_attempts + 1):
            try:
                last["container_status"] = await self.tools.container_status(target)
                health = await self.tools.service_health(service)
                if policy.service_health_condition == "status_ok":
                    last["service_healthy"] = health.get("detail", {}).get("status") == "ok"
                else:
                    last["service_healthy"] = bool(health.get("healthy"))
                dependency_filter = f',dependency="{target}"' if target in ("redis", "mysql") else ""
                metrics = await self.tools.query_metric(
                    f'dependency_up{{service="{service}"{dependency_filter}}}'
                )
                last["dependency_up"] = bool(metrics) and all(
                    float(item.get("value", [0, 0])[1]) >= policy.dependency_metric_threshold
                    for item in metrics
                )
            except Exception as exc:
                last["error"] = str(exc)
            recovered = all((
                last["container_status"] == "running",
                last["service_healthy"],
                last["dependency_up"],
            ))
            stable_checks = stable_checks + 1 if recovered else 0
            if stable_checks >= policy.recovery_stable_checks:
                return {
                    **last, "verified": True, "attempts": attempt,
                    "stable_checks": stable_checks,
                    "required_stable_checks": policy.recovery_stable_checks,
                    "policy": policy.model_dump(mode="json"),
                    "message": (
                        f"Service health and {target} dependency metric were stable for "
                        f"{stable_checks} check(s) after {attempt} total check(s)"
                    ),
                }
            if attempt < policy.max_attempts:
                await asyncio.sleep(policy.check_interval_seconds)
        return {
            **last, "verified": False, "attempts": policy.max_attempts,
            "stable_checks": stable_checks,
            "required_stable_checks": policy.recovery_stable_checks,
            "policy": policy.model_dump(mode="json"),
            "message": f"Recovery verification timed out after {policy.max_attempts} check(s)",
        }

    def _route_after_verification(self, graph_state: WorkflowState) -> Literal["resolved", "failed", "not_executed"]:
        state = graph_state["incident"]
        if state.verified is True:
            return "resolved"
        if state.verified is False:
            return "failed"
        return "not_executed"

    async def run(self, request: AnalyzeRequest) -> IncidentState:
        now = datetime.now(timezone.utc)
        incident_at = request.incident_started_at or now
        if incident_at.tzinfo is None:
            incident_at = incident_at.replace(tzinfo=timezone.utc)
        incident_at = incident_at.astimezone(timezone.utc)
        window_start = incident_at - timedelta(minutes=2)
        window_end = min(now, incident_at + timedelta(minutes=5))
        if window_end < window_start:
            window_end = window_start
        evidence_context = {
            "origin": request.evidence_origin,
            "incident_at": incident_at.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "prometheus": {"mode": "incident_instant", "query_at": incident_at.isoformat()},
            "loki": {
                "mode": "incident_window", "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            "incident_at_value": incident_at,
            "window_start_value": window_start,
            "window_end_value": window_end,
        }
        incident = IncidentState(
            incident_id=request.incident_id or IncidentState().incident_id,
            service=request.service,
            symptom=request.symptom,
            execution_requested=request.execute,
        )
        result = await self.graph.ainvoke(
            {
                "incident": incident,
                "request": request,
                "metrics": [],
                "cpu_metrics": [],
                "logs": [],
                "target": request.service,
                "investigation_plan": None,
                "tool_observations": [],
                "llm_analysis": None,
                "policy_decision": None,
                "evidence_context": evidence_context,
            },
            config={"configurable": {"thread_id": incident.incident_id}},
        )
        return result["incident"]

    async def inspect_state(self, incident_id: str):
        """Return LangGraph's latest state snapshot for an incident."""
        return await self.graph.aget_state({"configurable": {"thread_id": incident_id}})
