from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentName(StrEnum):
    COORDINATOR = "coordinator"
    MONITOR = "monitor"
    LOG = "log"
    RCA = "rca"
    SOLUTION = "solution"
    SAFETY = "safety"
    EXECUTOR = "executor"
    VERIFICATION = "verification"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Evidence(BaseModel):
    source: str
    summary: str
    data: Any = None


class AgentEvent(BaseModel):
    agent: AgentName
    message: str
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Recommendation(BaseModel):
    title: str
    command: str | None = None
    risk: RiskLevel
    requires_approval: bool = True


class IncidentState(BaseModel):
    incident_id: str = Field(default_factory=lambda: str(uuid4()))
    service: str = "payment-service"
    symptom: str = "dependency unavailable"
    status: str = "investigating"
    evidence: list[Evidence] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    root_cause: str | None = None
    confidence: float = 0
    recommendations: list[Recommendation] = Field(default_factory=list)
    execution_requested: bool = False
    execution_result: str | None = None
    verified: bool | None = None


class AnalyzeRequest(BaseModel):
    service: str = "payment-service"
    symptom: str = "dependency unavailable"
    execute: bool = False
    approved: bool = False
    incident_id: str | None = None


class FaultRequest(BaseModel):
    approved: bool = False
