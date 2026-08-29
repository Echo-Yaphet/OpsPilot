import asyncio
from typing import Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .execution import ExecutionPolicy, Executor, PolicyDecision, RestrictedExecutor
from .knowledge import KnowledgeRetriever, NoopKnowledgeRetriever
from .models import AgentEvent, AgentName, AnalyzeRequest, Evidence, IncidentState, Recommendation, RiskLevel
from .tools import OpsTools


class WorkflowState(TypedDict):
    """Internal, inspectable graph state; IncidentState remains the public API."""

    incident: IncidentState
    request: AnalyzeRequest
    metrics: list[dict]
    logs: list[str]
    target: str
    policy_decision: PolicyDecision | None


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
    ):
        self.tools = tools
        self.verification_attempts = verification_attempts
        self.verification_interval = verification_interval
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.executor = executor or RestrictedExecutor(tools)
        self.knowledge_retriever = knowledge_retriever or NoopKnowledgeRetriever()
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
        state = graph_state["incident"]
        self.event(state, AgentName.COORDINATOR, "Started metrics and log investigation")
        return {"incident": state}

    async def _monitor(self, graph_state: WorkflowState) -> dict:
        state, request = graph_state["incident"], graph_state["request"]
        metric_query = f'dependency_up{{service="{request.service}"}}'
        try:
            metrics = await self.tools.query_metric(metric_query)
        except Exception as exc:
            metrics = []
            state.evidence.append(Evidence(source="prometheus", summary="metrics query unavailable", data=str(exc)))
        state.evidence.append(Evidence(source="prometheus", summary="dependency health metrics", data=metrics))
        self.event(state, AgentName.MONITOR, f"Collected {len(metrics)} dependency series")
        return {"incident": state, "metrics": metrics}

    async def _log(self, graph_state: WorkflowState) -> dict:
        state, request = graph_state["incident"], graph_state["request"]
        try:
            logs = await self.tools.query_logs(request.service)
        except Exception as exc:
            logs = [f"log query unavailable: {exc}"]
        state.evidence.append(Evidence(source="loki", summary="recent error logs", data=logs[:20]))
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

        if redis_down:
            state.root_cause, state.confidence = "Redis dependency is unavailable", 0.92
            target = "redis"
        elif mysql_down:
            state.root_cause, state.confidence = "MySQL dependency is unavailable", 0.9
            target = "mysql"
        else:
            state.root_cause, state.confidence = "Insufficient evidence; dependency or application degradation suspected", 0.45
            target = request.service
        runbooks = self.knowledge_retriever.retrieve_runbooks(
            request.service, request.symptom, state.root_cause
        )
        history = self.knowledge_retriever.retrieve_incidents(state)
        state.evidence.append(Evidence(
            source="runbook", summary="deterministic runbook retrieval",
            data=[item.model_dump(mode="json") for item in runbooks],
        ))
        state.evidence.append(Evidence(
            source="incident_history", summary="similar historical incident retrieval",
            data=[item.model_dump(mode="json") for item in history],
        ))
        self.event(state, AgentName.RCA, f"Root cause: {state.root_cause} ({state.confidence:.0%})")
        return {"incident": state, "target": target}

    def _route_after_rca(self, graph_state: WorkflowState) -> Literal["conclusive", "insufficient_evidence"]:
        return "conclusive" if graph_state["incident"].confidence >= 0.8 else "insufficient_evidence"

    async def _solution(self, graph_state: WorkflowState) -> dict:
        state, target = graph_state["incident"], graph_state["target"]
        runbook_evidence = next((item for item in state.evidence if item.source == "runbook"), None)
        matched_runbook = runbook_evidence.data[0] if runbook_evidence and runbook_evidence.data else None
        command = matched_runbook.get("command") if matched_runbook else None
        if command is None:
            command = f"docker compose restart {target}"
        title = matched_runbook.get("title") if matched_runbook else None
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
        last = {"container_status": "unknown", "service_healthy": False, "dependency_up": False}
        for attempt in range(1, self.verification_attempts + 1):
            try:
                last["container_status"] = await self.tools.container_status(target)
                health = await self.tools.service_health(service)
                last["service_healthy"] = bool(health.get("healthy"))
                dependency_filter = f',dependency="{target}"' if target in ("redis", "mysql") else ""
                metrics = await self.tools.query_metric(
                    f'dependency_up{{service="{service}"{dependency_filter}}}'
                )
                last["dependency_up"] = bool(metrics) and all(
                    float(item.get("value", [0, 0])[1]) == 1 for item in metrics
                )
            except Exception as exc:
                last["error"] = str(exc)
            if all((
                last["container_status"] == "running",
                last["service_healthy"],
                last["dependency_up"],
            )):
                return {
                    **last, "verified": True, "attempts": attempt,
                    "message": f"Service health and {target} dependency metric recovered after {attempt} check(s)",
                }
            if attempt < self.verification_attempts:
                await asyncio.sleep(self.verification_interval)
        return {
            **last, "verified": False, "attempts": self.verification_attempts,
            "message": f"Recovery verification timed out after {self.verification_attempts} check(s)",
        }

    def _route_after_verification(self, graph_state: WorkflowState) -> Literal["resolved", "failed", "not_executed"]:
        state = graph_state["incident"]
        if state.verified is True:
            return "resolved"
        if state.verified is False:
            return "failed"
        return "not_executed"

    async def run(self, request: AnalyzeRequest) -> IncidentState:
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
                "logs": [],
                "target": request.service,
                "policy_decision": None,
            },
            config={"configurable": {"thread_id": incident.incident_id}},
        )
        return result["incident"]

    async def inspect_state(self, incident_id: str):
        """Return LangGraph's latest state snapshot for an incident."""
        return await self.graph.aget_state({"configurable": {"thread_id": incident_id}})
