from dataclasses import dataclass
import shlex
from typing import Protocol

import httpx
from workload_identity import mint_identity

from .tools import OpsTools


ALLOWED_RESTART_TARGETS = frozenset({
    "redis",
    "mysql",
    "user-service",
    "order-service",
    "payment-service",
})


@dataclass(frozen=True)
class ExecutionAction:
    """A typed operation passed across the restricted executor boundary."""

    operation: str
    target: str
    command: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    policy: str
    action: ExecutionAction | None = None

    def audit_data(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy": self.policy,
            "operation": self.action.operation if self.action else None,
            "target": self.action.target if self.action else None,
            "command": self.action.command if self.action else None,
        }


class ExecutionPolicy:
    """Allow only exact, local Compose restart operations for known services."""

    name = "local-compose-restart-v1"

    def __init__(self, allowed_restart_targets: frozenset[str] = ALLOWED_RESTART_TARGETS):
        self.allowed_restart_targets = allowed_restart_targets

    def evaluate(self, command: str | None) -> PolicyDecision:
        if not command:
            return PolicyDecision(False, "denied: recommendation has no executable command", self.name)
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            return PolicyDecision(False, f"denied: command could not be parsed ({exc})", self.name)
        if len(tokens) != 4 or tokens[:3] != ["docker", "compose", "restart"]:
            return PolicyDecision(
                False,
                "denied: operation is not on the docker compose restart allowlist",
                self.name,
            )
        target = tokens[3]
        if target not in self.allowed_restart_targets:
            return PolicyDecision(False, f"denied: restart target is not allowlisted: {target}", self.name)
        action = ExecutionAction(operation="restart_container", target=target, command=command)
        return PolicyDecision(True, f"allowed: restart target is allowlisted: {target}", self.name, action)


class RestrictedExecutor:
    """Narrow gateway seam; it cannot execute arbitrary shell commands."""

    def __init__(self, tools: OpsTools, allowed_restart_targets: frozenset[str] = ALLOWED_RESTART_TARGETS):
        self.tools = tools
        self.allowed_restart_targets = allowed_restart_targets

    async def execute(self, action: ExecutionAction) -> str:
        if action.operation != "restart_container":
            raise PermissionError(f"executor operation is not supported: {action.operation}")
        if action.target not in self.allowed_restart_targets:
            raise PermissionError(f"executor target is not allowlisted: {action.target}")
        return await self.tools.restart_container(action.target)


class Executor(Protocol):
    async def execute(self, action: ExecutionAction) -> str: ...


class GatewayExecutor:
    """Typed client for the separately deployed executor gateway."""

    def __init__(
        self, base_url: str, identity_key: str, timeout: float = 15,
        issuer: str = "opspilot-control-api", audience: str = "opspilot-executor-gateway",
        subject: str = "control-api", ttl_seconds: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.identity_key = identity_key
        self.timeout = timeout
        self.issuer = issuer
        self.audience = audience
        self.subject = subject
        self.ttl_seconds = ttl_seconds

    async def execute(self, action: ExecutionAction) -> str:
        payload = {"operation": action.operation, "target": action.target}
        path = "/v1/actions"
        credential = mint_identity(
            self.identity_key,
            issuer=self.issuer,
            audience=self.audience,
            subject=self.subject,
            ttl_seconds=self.ttl_seconds,
            method="POST",
            path=path,
            operation=action.operation,
            target=action.target,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {credential}"},
                )
        except httpx.TimeoutException as exc:
            raise RuntimeError("executor gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"executor gateway unavailable: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(f"executor gateway rejected action: {detail}")
        return response.json()["result"]
