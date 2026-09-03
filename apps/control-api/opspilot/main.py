import asyncio
import hashlib
import json
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from workload_identity import IdentityError

from .config import VerificationPolicyProvider, settings
from .execution import GatewayExecutor
from .knowledge import OpenAICompatibleEmbeddingProvider, SemanticKnowledgeRetriever
from .llm import OllamaIncidentAnalyzer
from .models import AgentEvent, AgentName, AnalyzeRequest, FaultRequest, IncidentState
from .policy_distribution import (
    VerificationPolicyPeerAuthenticator,
    VerificationPolicyRolloutReporter,
)
from .storage import IncidentStore
from .tools import LiveOpsTools
from .workflow import IncidentWorkflow

app = FastAPI(title="OpsPilot Control API", version="0.1.0")
tools = LiveOpsTools(settings)
store = IncidentStore(settings.database_path)
knowledge_retriever = store
if settings.embedding_base_url and settings.embedding_model:
    knowledge_retriever = SemanticKnowledgeRetriever(
        fallback=store,
        corpus=store,
        embeddings=OpenAICompatibleEmbeddingProvider(
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_api_key,
            settings.embedding_timeout,
        ),
        minimum_similarity=settings.semantic_minimum_similarity,
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
    identity_key=settings.verification_policy_peer_identity_key,
    identity_key_id=settings.verification_policy_peer_identity_key_id,
    identity_issuer=settings.verification_policy_peer_identity_issuer,
    identity_audience=settings.verification_policy_peer_identity_audience,
    identity_ttl_seconds=settings.verification_policy_peer_identity_ttl_seconds,
)
verification_policy_peer_authenticator = VerificationPolicyPeerAuthenticator(
    node_id=settings.verification_policy_node_id,
    identity_key=settings.verification_policy_peer_identity_key,
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
workflow = IncidentWorkflow(tools, executor=GatewayExecutor(
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


@app.get("/api/v1/verification-policy/status")
async def verification_policy_status():
    """Expose reload health without allowing unauthenticated policy mutation."""
    return verification_policy_provider.status()


@app.get("/api/v1/verification-policy/peer-status")
async def verification_policy_peer_status(authorization: str | None = Header(default=None)):
    """Expose the same read-only status to authenticated peer fan-out only."""
    try:
        verification_policy_peer_authenticator.verify(authorization)
    except IdentityError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return verification_policy_provider.status()


@app.get("/api/v1/verification-policy/rollout")
async def verification_policy_rollout():
    """Report read-only multi-node policy health and convergence."""
    return await verification_policy_rollout_reporter.report(verification_policy_provider.status())


@app.post("/api/v1/incidents/analyze", response_model=IncidentState)
async def analyze(request: AnalyzeRequest):
    try:
        state = await workflow.run(request)
        store.save(state, approved=request.approved if request.execute else None)
        return state
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/incidents", response_model=list[IncidentState])
async def list_incidents(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return store.list(limit=limit, offset=offset)


@app.get("/api/v1/incidents/{incident_id}", response_model=IncidentState)
async def get_incident(incident_id: str):
    incident = store.get(incident_id)
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
async def alertmanager_webhook(payload: dict):
    processed: list[IncidentState] = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        service = labels.get("service", "payment-service")
        symptom = annotations.get("summary") or labels.get("alertname", "Alertmanager incident")
        key = alert_key(alert)
        existing = store.get_by_alert_key(key)
        if alert.get("status", payload.get("status", "firing")) == "resolved":
            if existing:
                existing.status = "alert_resolved"
                existing.events.append(AgentEvent(
                    agent=AgentName.COORDINATOR,
                    message="Alertmanager reported that the alert signal recovered",
                ))
                store.save(existing, alert_key=key)
                processed.append(existing)
            continue
        request = AnalyzeRequest(
            service=service,
            symptom=symptom,
            execute=False,
            approved=False,
            incident_id=existing.incident_id if existing else None,
        )
        request.set_evidence_context(alert_started_at(alert), "alertmanager")
        try:
            state = await workflow.run(request)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        store.save(state, alert_key=key)
        processed.append(state)
    return {"status": "accepted", "processed": len(processed), "incidents": processed}


@app.get("/api/v1/system/status")
async def system_status():
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
async def inject_fault(fault: str, request: FaultRequest):
    if not request.approved:
        raise HTTPException(status_code=403, detail="Explicit approval is required")
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
