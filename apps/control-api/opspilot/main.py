import asyncio
import hmac
import hashlib
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from workload_identity import IdentityError

from .config import VerificationPolicyProvider, settings
from .access_control import (
    AccessIdentityError,
    AccessPrincipal,
    ApiAccessAuthenticator,
    approval_audit,
)
from .execution import GatewayExecutor
from .knowledge import OpenAICompatibleEmbeddingProvider, SemanticKnowledgeRetriever
from .llm import OllamaIncidentAnalyzer
from .investigation import InvestigationBudget, InvestigationJournal, SDKInvestigator
from .investigation import PostgresInvestigationJournal
from .event_memory import PostgresEventMemory
from .observability import instrument_fastapi
from .models import AgentEvent, AgentName, AnalyzeRequest, FaultRequest, IncidentState
from .policy_distribution import (
    VerificationPolicyPeerAuthenticator,
    VerificationPolicyRolloutReporter,
)
from .repair import (
    RepairApprovalRequest,
    RepairBudget,
    RepairError,
    RepairProposalStore,
    RepairRequest,
    RepairSandboxClient,
    SDKRepairAgent,
)
from .storage import IncidentStore, PostgresIncidentStore, run_database_call
from .skill_promotion import (
    SkillCandidateRequest,
    SkillPromotionError,
    SkillPromotionRequest,
    SkillPromotionService,
)
from .tools import LiveOpsTools
from .workflow import IncidentWorkflow

app = FastAPI(title="OpsPilot Control API", version="0.1.0")
instrument_fastapi(app, "opspilot-control-api")
tools = LiveOpsTools(settings)
store = (PostgresIncidentStore(
             settings.database_url,
             settings.database_path,
             connect_timeout_seconds=settings.database_connect_timeout_seconds,
             acquire_timeout_seconds=settings.database_acquire_timeout_seconds,
             statement_timeout_milliseconds=settings.database_statement_timeout_milliseconds,
             lock_timeout_milliseconds=settings.database_lock_timeout_milliseconds,
             idle_transaction_timeout_milliseconds=(
                 settings.database_idle_transaction_timeout_milliseconds
             ),
             max_concurrency=settings.database_max_concurrency,
         )
         if settings.database_url else IncidentStore(settings.database_path))
skill_promotion = SkillPromotionService(
    store, settings.skill_cases_file, settings.skill_workspace_root,
)
api_access_authenticator = ApiAccessAuthenticator(
    enabled=settings.api_access_auth_enabled,
    public_key_file=settings.api_access_public_key_file,
    key_id=settings.api_access_key_id,
    issuer=settings.api_access_issuer,
    audience=settings.api_access_audience,
    maximum_ttl_seconds=settings.api_access_maximum_ttl_seconds,
)


def _authorize_access(authorization: str | None, permission: str) -> AccessPrincipal:
    try:
        principal = api_access_authenticator.authenticate(authorization)
    except AccessIdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if permission not in principal.permissions:
        raise HTTPException(
            status_code=403, detail=f"access identity lacks {permission} permission"
        )
    return principal


async def _consume_approval(principal: AccessPrincipal) -> None:
    if not api_access_authenticator.enabled:
        return
    try:
        await run_database_call(
            store.consume_api_approval_credential,
            principal.credential_id,
            principal.subject,
            principal.expires_at,
            timeout_seconds=settings.database_request_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _authorize_skill_mutation(authorization: str | None) -> None:
    expected = f"Bearer {settings.skill_promotion_token}"
    if not settings.skill_promotion_token or not authorization or not hmac.compare_digest(
        authorization, expected
    ):
        raise HTTPException(status_code=401, detail="valid Skill promotion identity is required")


knowledge_retriever = store
embedding_provider = None
if settings.embedding_base_url and settings.embedding_model:
    embedding_provider = OpenAICompatibleEmbeddingProvider(
        settings.embedding_base_url,
        settings.embedding_model,
        settings.embedding_api_key,
        settings.embedding_timeout,
    )
    knowledge_retriever = SemanticKnowledgeRetriever(
        fallback=store,
        corpus=store,
        embeddings=embedding_provider,
        minimum_similarity=settings.semantic_minimum_similarity,
    )
postgres_timeout_options = {
    "connect_timeout_seconds": settings.database_connect_timeout_seconds,
    "acquire_timeout_seconds": settings.database_acquire_timeout_seconds,
    "statement_timeout_milliseconds": settings.database_statement_timeout_milliseconds,
    "lock_timeout_milliseconds": settings.database_lock_timeout_milliseconds,
    "idle_transaction_timeout_milliseconds": (
        settings.database_idle_transaction_timeout_milliseconds
    ),
    "max_concurrency": settings.database_max_concurrency,
}
event_memory = (
    PostgresEventMemory(settings.memory_database_url, **postgres_timeout_options)
    if settings.memory_database_url else None
)
verification_policy_provider = VerificationPolicyProvider(
    settings.default_verification_policy(),
    settings.verification_service_policies,
    settings.verification_policy_file,
    settings.verification_policy_signing_keys,
    settings.verification_policy_require_signature,
    store,
    settings.verification_policy_source(),
)
verification_policy_rollout_reporter = VerificationPolicyRolloutReporter(
    node_id=settings.verification_policy_node_id,
    peers=settings.verification_policy_rollout_nodes,
    timeout=settings.verification_policy_rollout_timeout,
    max_concurrency=settings.verification_policy_rollout_max_concurrency,
    identity_issuer_url=settings.workload_identity_issuer_url,
    identity_private_key_file=settings.workload_identity_private_key_file,
    identity_audience=settings.verification_policy_peer_identity_audience,
    identity_subject=settings.executor_identity_subject,
    identity_ttl_seconds=settings.verification_policy_peer_identity_ttl_seconds,
)
verification_policy_peer_authenticator = VerificationPolicyPeerAuthenticator(
    node_id=settings.verification_policy_node_id,
    identity_public_key_file=settings.verification_policy_peer_identity_public_key_file,
    identity_key_id=settings.verification_policy_peer_identity_key_id,
    identity_issuer=settings.verification_policy_peer_identity_issuer,
    identity_audience=settings.verification_policy_peer_identity_audience,
    maximum_ttl_seconds=settings.verification_policy_peer_identity_ttl_seconds,
    consume=store.consume_verification_policy_peer_credential,
)
incident_analyzer = None
if settings.llm_base_url and settings.llm_model:
    incident_analyzer = OllamaIncidentAnalyzer(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_timeout,
        settings.llm_think,
    )
investigator = None
if settings.investigation_mode == "agents_sdk":
    journal = (PostgresInvestigationJournal(
                   settings.memory_database_url, **postgres_timeout_options
               )
               if settings.memory_database_url else InvestigationJournal(settings.database_path))
    investigator = SDKInvestigator(
        tools,
        SDKInvestigator.ollama_model(settings.llm_base_url, settings.llm_model),
        journal,
        InvestigationBudget(max_turns=settings.investigation_max_turns,
                            max_tool_calls=settings.investigation_max_tool_calls,
                            timeout_seconds=settings.investigation_timeout,
                            max_total_tokens=settings.investigation_max_total_tokens),
        event_memory=event_memory, embeddings=embedding_provider,
        service_version=settings.service_version,
        skill_provider=skill_promotion.active_instructions,
        database_timeout_seconds=settings.database_request_timeout_seconds,
    )
repair_agent = None
if settings.repair_mode == "agents_sdk":
    repair_agent = SDKRepairAgent(
        RepairSandboxClient(settings.repair_sandbox_url, settings.repair_sandbox_token),
        SDKInvestigator.ollama_model(settings.llm_base_url, settings.llm_model),
        RepairProposalStore(settings.database_path),
        settings.repair_approval_key,
        RepairBudget(
            max_turns=settings.repair_max_turns,
            max_tool_calls=settings.repair_max_tool_calls,
            timeout_seconds=settings.repair_timeout,
        ),
    )
workflow = IncidentWorkflow(tools, investigator=investigator, executor=GatewayExecutor(
    settings.executor_gateway_url,
    settings.workload_identity_issuer_url,
    settings.workload_identity_private_key_file,
    settings.executor_gateway_timeout,
    settings.executor_identity_audience,
    settings.executor_identity_subject,
    settings.executor_identity_ttl_seconds,
), knowledge_retriever=knowledge_retriever, incident_analyzer=incident_analyzer,
    default_verification_policy=settings.default_verification_policy(),
    verification_policies=settings.verification_policies(),
    verification_policy_provider=verification_policy_provider,
    database_timeout_seconds=settings.database_request_timeout_seconds,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001", "http://127.0.0.1:3001"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-control-api"}


@app.get("/api/v1/system/memory/status")
async def memory_status(authorization: str | None = Header(default=None)):
    """Read-only visibility for the optional event-memory backend."""
    _authorize_access(authorization, "read")
    if event_memory is None:
        return {"backend": "disabled", "healthy": True, "active_events": 0}
    return await run_database_call(
        event_memory.health, timeout_seconds=settings.database_request_timeout_seconds
    )


@app.get("/api/v1/skills/cases")
async def list_skill_cases(authorization: str | None = Header(default=None)):
    """Return the immutable, server-owned evaluation cases and their digest."""
    _authorize_access(authorization, "read")
    return skill_promotion.list_cases()


@app.get("/api/v1/skills/{skill_id}/versions")
async def list_skill_versions(skill_id: str, authorization: str | None = Header(default=None)):
    _authorize_access(authorization, "read")
    return skill_promotion.list_versions(skill_id)


@app.get("/api/v1/skills/{skill_id}/active")
async def get_active_skill(skill_id: str, authorization: str | None = Header(default=None)):
    _authorize_access(authorization, "read")
    try:
        return skill_promotion.active(skill_id)
    except SkillPromotionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/v1/skills/candidates", status_code=201)
async def create_skill_candidate(
    request: SkillCandidateRequest, authorization: str | None = Header(default=None),
):
    """Freeze, isolate and evaluate a candidate; this never promotes it."""
    if api_access_authenticator.enabled:
        _authorize_access(authorization, "admin")
    else:
        _authorize_skill_mutation(authorization)
    try:
        return skill_promotion.create_candidate(request)
    except SkillPromotionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/v1/skills/promotions")
async def promote_skill(
    request: SkillPromotionRequest, authorization: str | None = Header(default=None),
):
    """Promote only a passing, current-parent candidate with explicit approval."""
    principal = None
    if api_access_authenticator.enabled:
        principal = _authorize_access(authorization, "admin")
        if request.approved:
            await _consume_approval(principal)
    else:
        _authorize_skill_mutation(authorization)
    try:
        return skill_promotion.promote(request)
    except SkillPromotionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/api/v1/verification-policy/status")
async def verification_policy_status():
    """Expose reload health without allowing unauthenticated policy mutation."""
    return verification_policy_provider.status()


@app.get("/api/v1/verification-policy/peer-status")
async def verification_policy_peer_status(authorization: str | None = Header(default=None)):
    """Expose the same read-only status to authenticated peer fan-out only."""
    try:
        await run_database_call(
            verification_policy_peer_authenticator.verify,
            authorization,
            timeout_seconds=settings.database_request_timeout_seconds,
        )
    except IdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return verification_policy_provider.status()


@app.get("/api/v1/verification-policy/rollout")
async def verification_policy_rollout():
    """Report read-only multi-node policy health and convergence."""
    return await verification_policy_rollout_reporter.report(verification_policy_provider.status())


@app.post("/api/v1/incidents/analyze", response_model=IncidentState)
async def analyze(
    request: AnalyzeRequest,
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None),
):
    principal = _authorize_access(authorization, "analyze")
    audit = None
    if request.execute and request.approved:
        if "approve" not in principal.permissions:
            raise HTTPException(status_code=403, detail="verified approver identity is required")
        await _consume_approval(principal)
        audit = approval_audit(principal, x_request_id or str(uuid4()))
    try:
        state = await workflow.run(request)
        state = await run_database_call(
            store.save,
            state,
            approved=request.approved if request.execute else None,
            approval_identity=audit,
            timeout_seconds=settings.database_request_timeout_seconds,
        )
        return state
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/repair-lab/proposals")
async def propose_repair(
    request: RepairRequest, authorization: str | None = Header(default=None)
):
    """Create a prevalidated lab-only package; never applies it."""
    _authorize_access(authorization, "repair_propose")
    if repair_agent is None:
        raise HTTPException(status_code=503, detail="repair lab is disabled")
    try:
        return await repair_agent.propose(request.symptom)
    except RepairError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/v1/repair-lab/approvals")
async def approve_repair(
    request: RepairApprovalRequest, authorization: str | None = Header(default=None)
):
    """Apply exactly one persisted package after explicit human approval."""
    principal = _authorize_access(authorization, "repair_approve")
    if request.approved:
        await _consume_approval(principal)
    if repair_agent is None:
        raise HTTPException(status_code=503, detail="repair lab is disabled")
    try:
        return await repair_agent.approve(request.package_id, request.approved)
    except RepairError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/api/v1/incidents", response_model=list[IncidentState])
async def list_incidents(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    authorization: str | None = Header(default=None),
):
    _authorize_access(authorization, "read")
    return await run_database_call(
        store.list,
        limit=limit,
        offset=offset,
        timeout_seconds=settings.database_request_timeout_seconds,
    )


@app.get("/api/v1/incidents/{incident_id}", response_model=IncidentState)
async def get_incident(incident_id: str, authorization: str | None = Header(default=None)):
    _authorize_access(authorization, "read")
    incident = await run_database_call(
        store.get, incident_id, timeout_seconds=settings.database_request_timeout_seconds
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


def alert_key(alert: dict) -> str:
    fingerprint = alert.get("fingerprint")
    if fingerprint:
        return str(fingerprint)
    stable = json.dumps(alert.get("labels", {}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode()).hexdigest()


def alert_started_at(alert: dict) -> datetime:
    value = alert.get("startsAt")
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


@app.post("/api/v1/alertmanager/webhook")
async def alertmanager_webhook(
    payload: dict, authorization: str | None = Header(default=None)
):
    _authorize_access(authorization, "alert_webhook")
    processed: list[IncidentState] = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        service = labels.get("service", "payment-service")
        symptom = annotations.get("summary") or labels.get("alertname", "Alertmanager incident")
        key = alert_key(alert)
        existing = await run_database_call(
            store.get_by_alert_key,
            key,
            timeout_seconds=settings.database_request_timeout_seconds,
        )
        if alert.get("status", payload.get("status", "firing")) == "resolved":
            if existing:
                existing.status = "alert_resolved"
                existing.events.append(AgentEvent(
                    agent=AgentName.COORDINATOR,
                    message="Alertmanager reported that the alert signal recovered",
                ))
                existing = await run_database_call(
                    store.save,
                    existing,
                    alert_key=key,
                    timeout_seconds=settings.database_request_timeout_seconds,
                )
                processed.append(existing)
            continue
        request = AnalyzeRequest(
            service=service,
            symptom=symptom,
            execute=False,
            approved=False,
            incident_id=(
                existing.incident_id
                if existing
                else str(uuid5(NAMESPACE_URL, f"opspilot:alert:{key}"))
            ),
        )
        request.set_evidence_context(alert_started_at(alert), "alertmanager")
        try:
            state = await workflow.run(request)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state = await run_database_call(
            store.save,
            state,
            alert_key=key,
            timeout_seconds=settings.database_request_timeout_seconds,
        )
        processed.append(state)
    return {"status": "accepted", "processed": len(processed), "incidents": processed}


@app.get("/api/v1/system/status")
async def system_status(authorization: str | None = Header(default=None)):
    _authorize_access(authorization, "read")
    services = {
        "user-service": "http://user-service:8001/health",
        "order-service": "http://order-service:8002/health",
        "payment-service": "http://payment-service:8003/health",
    }

    async def check(name: str, url: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(url)
            payload = response.json()
            detail = payload.get("detail", payload)
            return {"name": name, "healthy": response.status_code == 200, "detail": detail}
        except Exception as exc:
            return {"name": name, "healthy": False, "detail": {"error": str(exc)}}

    results = await asyncio.gather(*(check(name, url) for name, url in services.items()))
    infrastructure = []
    for name in ("redis", "mysql", "prometheus", "alertmanager", "loki"):
        try:
            infrastructure.append({"name": name, "healthy": await tools.container_status(name) == "running"})
        except Exception as exc:
            infrastructure.append({"name": name, "healthy": False, "detail": {"error": str(exc)}})
    all_healthy = all(item["healthy"] for item in [*results, *infrastructure])
    return {"healthy": all_healthy, "services": results, "infrastructure": infrastructure}


@app.post("/api/v1/faults/{fault}")
async def inject_fault(
    fault: str, request: FaultRequest, authorization: str | None = Header(default=None)
):
    principal = _authorize_access(authorization, "admin")
    if not request.approved:
        raise HTTPException(status_code=403, detail="Explicit approval is required")
    await _consume_approval(principal)
    targets = {"redis-down": "redis", "mysql-down": "mysql"}
    if fault in targets:
        target = targets[fault]
        result = await tools.stop_container(target)
        return {"fault": fault, "status": "injected", "result": result}
    if fault == "cpu-spike":
        async with httpx.AsyncClient(timeout=35) as client:
            await client.get("http://payment-service:8003/work", params={"seconds": 15})
        return {"fault": fault, "status": "injected", "result": "payment-service CPU work completed"}
    raise HTTPException(status_code=404, detail="Unknown fault scenario")
