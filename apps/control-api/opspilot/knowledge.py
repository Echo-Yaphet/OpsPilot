import math
from typing import Any, Protocol

import httpx

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


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class RunbookCorpus(Protocol):
    def list_runbooks(self) -> list[RunbookMatch]: ...


class OpenAICompatibleEmbeddingProvider:
    """Small optional adapter for OpenAI-compatible embedding endpoints."""

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 10):
        self.url = f"{base_url.rstrip('/')}/embeddings"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = httpx.post(
            self.url, json={"model": self.model, "input": texts}, headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in rows]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0


class SemanticKnowledgeRetriever:
    """Optional semantic runbook ranking with fail-open deterministic retrieval."""

    def __init__(
        self,
        fallback: KnowledgeRetriever,
        corpus: RunbookCorpus,
        embeddings: EmbeddingProvider,
        minimum_similarity: float = 0.75,
    ):
        self.fallback = fallback
        self.corpus = corpus
        self.embeddings = embeddings
        self.minimum_similarity = minimum_similarity

    def retrieve_runbooks(
        self, service: str, symptom: str, root_cause: str, limit: int = 3
    ) -> list[RunbookMatch]:
        deterministic = self.fallback.retrieve_runbooks(service, symptom, root_cause, limit)
        try:
            candidates = self.corpus.list_runbooks()
            query = f"service: {service}; symptom: {symptom}; root cause: {root_cause}"
            documents = [
                f"service: {item.service}; root cause: {item.root_cause}; "
                f"title: {item.title}; description: {item.description}"
                for item in candidates
            ]
            vectors = self.embeddings.embed([query, *documents])
            if len(vectors) != len(documents) + 1:
                raise ValueError("embedding provider returned an unexpected vector count")
            dimensions = {len(vector) for vector in vectors}
            if dimensions == {0} or len(dimensions) != 1:
                raise ValueError("embedding provider returned invalid vector dimensions")
            if any(not math.isfinite(value) for vector in vectors for value in vector):
                raise ValueError("embedding provider returned non-finite vector values")
            semantic: list[RunbookMatch] = []
            deterministic_ids = {item.runbook_id for item in deterministic}
            for candidate, vector in zip(candidates, vectors[1:]):
                similarity = _cosine(vectors[0], vector)
                if candidate.runbook_id in deterministic_ids or similarity < self.minimum_similarity:
                    continue
                factors = {"semantic_similarity": round(similarity, 6)}
                semantic.append(candidate.model_copy(update={
                    "score": similarity,
                    "score_explanation": RetrievalScore(total=similarity, factors=factors),
                }))
            semantic.sort(key=lambda item: (-item.score, item.runbook_id))
            # Exact deterministic matches always retain their position and score.
            return (deterministic + semantic)[:limit]
        except Exception:
            return deterministic

    def retrieve_incidents(self, incident: IncidentState, limit: int = 3) -> list[IncidentMatch]:
        return self.fallback.retrieve_incidents(incident, limit)


class NoopKnowledgeRetriever:
    def retrieve_runbooks(
        self, service: str, symptom: str, root_cause: str, limit: int = 3
    ) -> list[RunbookMatch]:
        return []

    def retrieve_incidents(self, incident: IncidentState, limit: int = 3) -> list[IncidentMatch]:
        return []
