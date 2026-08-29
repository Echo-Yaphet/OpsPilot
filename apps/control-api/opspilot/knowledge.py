from typing import Any, Protocol

from pydantic import BaseModel, Field

from .models import IncidentState


class RetrievalScore(BaseModel):
    """Stable, inspectable score shared by deterministic and semantic retrievers."""

    total: float
    factors: dict[str, float] = Field(default_factory=dict)


class RunbookMatch(BaseModel):
    runbook_id: str
    title: str
    service: str
    root_cause: str
    description: str
    command: str | None = None
    verification: str
    score: float
    score_explanation: RetrievalScore


class IncidentMatch(BaseModel):
    incident_id: str
    service: str
    symptom: str
    status: str
    root_cause: str | None = None
    confidence: float
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    execution_result: str | None = None
    verified: bool | None = None
    updated_at: str
    score: float
    score_explanation: RetrievalScore


class KnowledgeRetriever(Protocol):
    """Deterministic knowledge seam; semantic retrieval can replace it later."""

    def retrieve_runbooks(
        self, service: str, symptom: str, root_cause: str, limit: int = 3
    ) -> list[RunbookMatch]: ...

    def retrieve_incidents(
        self, incident: IncidentState, limit: int = 3
    ) -> list[IncidentMatch]: ...


class NoopKnowledgeRetriever:
    def retrieve_runbooks(
        self, service: str, symptom: str, root_cause: str, limit: int = 3
    ) -> list[RunbookMatch]:
        return []

    def retrieve_incidents(self, incident: IncidentState, limit: int = 3) -> list[IncidentMatch]:
        return []
