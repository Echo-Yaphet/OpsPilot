from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Protocol

import httpx
from workload_identity import IdentityError, mint_identity, verify_identity


PEER_STATUS_PATH = "/api/v1/verification-policy/peer-status"
PEER_STATUS_OPERATION = "read_verification_policy_status"


class PolicyContentSource(Protocol):
    def read_bytes(self) -> bytes: ...

    def accept(self, content: bytes) -> None: ...

    def status(self) -> dict: ...


class AuthenticatedPolicySource:
    """Read a bundle from an authenticated endpoint with an accepted-only cache."""

    def __init__(
        self,
        url: str,
        token: str,
        cache_path: str,
        timeout: float = 2,
        poll_interval: float = 2,
        fetch: Callable[[], bytes] | None = None,
    ):
        self.url = url
        self._token = token
        self._cache_path = Path(cache_path)
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._fetch = fetch or self._fetch_http
        self._lock = Lock()
        self._last_poll = 0.0
        self._observed_content: bytes | None = None
        self._accepted_content: bytes | None = None
        self._online: bool | None = None
        self._last_error: str | None = None
        self._using_cache = False
        self._cache_error: str | None = None

    def _fetch_http(self) -> bytes:
        response = httpx.get(
            self.url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.content

    def read_bytes(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            if self._observed_content is not None and now - self._last_poll < self._poll_interval:
                return self._observed_content
            self._last_poll = now
        try:
            content = self._fetch()
            if not content:
                raise ValueError("policy distributor returned an empty bundle")
        except Exception as exc:
            with self._lock:
                self._online = False
                self._last_error = str(exc)
                if self._accepted_content is not None:
                    self._using_cache = True
                    self._observed_content = self._accepted_content
                    return self._accepted_content
            try:
                content = self._cache_path.read_bytes()
            except OSError as cache_exc:
                raise OSError(
                    f"policy distributor unavailable ({exc}); accepted cache unavailable ({cache_exc})"
                ) from exc
            with self._lock:
                self._accepted_content = content
                self._observed_content = content
                self._using_cache = True
            return content
        with self._lock:
            self._observed_content = content
            self._online = True
            self._last_error = None
            self._using_cache = False
        return content

    def accept(self, content: bytes) -> None:
        with self._lock:
            self._accepted_content = content
            self._observed_content = content
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_name(f".{self._cache_path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, self._cache_path)
        except OSError as exc:
            with self._lock:
                self._cache_error = str(exc)
        else:
            with self._lock:
                self._cache_error = None

    def status(self) -> dict:
        with self._lock:
            return {
                "mode": "authenticated_remote",
                "url": self.url,
                "online": self._online,
                "using_cache": self._using_cache,
                "last_error": self._last_error,
                "cache_error": self._cache_error,
            }


class VerificationPolicyRolloutReporter:
    def __init__(
        self,
        node_id: str,
        peers: Mapping[str, str] | None = None,
        timeout: float = 2,
        max_concurrency: int = 4,
        identity_key: str = "opspilot-local-policy-peer-key",
        identity_key_id: str = "verification-policy-peer-v1",
        identity_issuer: str = "opspilot-control-api",
        identity_audience: str = "opspilot-verification-policy-peer",
        identity_ttl_seconds: int = 10,
    ):
        self.node_id = node_id
        self.peers = dict(peers or {})
        self.timeout = timeout
        self.max_concurrency = max_concurrency
        self.identity_key = identity_key
        self.identity_key_id = identity_key_id
        self.identity_issuer = identity_issuer
        self.identity_audience = identity_audience
        self.identity_ttl_seconds = identity_ttl_seconds

    @staticmethod
    def _node_status(node_id: str, status: dict, online: bool = True) -> dict:
        distribution = status.get("distribution") or {}
        return {
            "node_id": node_id,
            "online": online,
            "observed_revision": status.get("observed_bundle_revision"),
            "observed_digest": status.get("observed_content_digest"),
            "accepted_revision": status.get("bundle_revision"),
            "accepted_digest": status.get("content_digest"),
            "load_result": status.get("load_result"),
            "last_error": status.get("last_error"),
            "source": status.get("source"),
            "distribution_online": distribution.get("online"),
            "using_cache": bool(distribution.get("using_cache", False)),
        }

    def _headers(self, target_node_id: str) -> dict[str, str]:
        credential = mint_identity(
            self.identity_key,
            issuer=self.identity_issuer,
            audience=self.identity_audience,
            subject=self.node_id,
            ttl_seconds=self.identity_ttl_seconds,
            method="GET",
            path=PEER_STATUS_PATH,
            operation=PEER_STATUS_OPERATION,
            target=target_node_id,
            key_id=self.identity_key_id,
        )
        return {"Authorization": f"Bearer {credential}"}

    @staticmethod
    def _offline_node(node_id: str, base_url: str, exc: Exception) -> dict:
        return {
            "node_id": node_id,
            "online": False,
            "observed_revision": None,
            "observed_digest": None,
            "accepted_revision": None,
            "accepted_digest": None,
            "load_result": "offline",
            "last_error": str(exc),
            "source": base_url,
            "distribution_online": None,
            "using_cache": False,
        }

    async def _fetch_peer(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        node_id: str,
        base_url: str,
    ) -> dict:
        try:
            async with semaphore:
                response = await client.get(
                    f"{base_url.rstrip('/')}{PEER_STATUS_PATH}",
                    headers=self._headers(node_id),
                )
                response.raise_for_status()
                return self._node_status(node_id, response.json())
        except Exception as exc:
            return self._offline_node(node_id, base_url, exc)

    async def report(self, local_status: dict) -> dict:
        nodes = [self._node_status(self.node_id, local_status)]
        semaphore = asyncio.Semaphore(self.max_concurrency)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = [
                self._fetch_peer(client, semaphore, node_id, base_url)
                for node_id, base_url in sorted(self.peers.items())
            ]
            if tasks:
                nodes.extend(await asyncio.gather(*tasks))

        accepted = {
            (node["accepted_revision"], node["accepted_digest"])
            for node in nodes if node["online"]
        }
        observed_versions = [
            (node["observed_revision"], node["observed_digest"])
            for node in nodes
            if node["online"] and isinstance(node["observed_revision"], int)
        ]
        accepted_versions = [
            (node["accepted_revision"], node["accepted_digest"])
            for node in nodes
            if node["online"] and isinstance(node["accepted_revision"], int)
        ]
        desired_versions = observed_versions or accepted_versions
        desired_revision = max((version[0] for version in desired_versions), default=None)
        desired_digests = {
            digest for revision, digest in desired_versions
            if revision == desired_revision and isinstance(digest, str)
        }
        desired_digest = next(iter(desired_digests)) if len(desired_digests) == 1 else None
        desired_conflict = len(desired_digests) > 1
        converged = (
            bool(nodes)
            and all(node["online"] for node in nodes)
            and desired_revision is not None
            and not desired_conflict
            and accepted == {(desired_revision, desired_digest)}
            and all(
                node["load_result"] == "accepted"
                and node["observed_revision"] == node["accepted_revision"]
                and node["observed_digest"] == node["accepted_digest"]
                for node in nodes
            )
        )
        degraded = (
            any(not node["online"] for node in nodes)
            or any(
                node["online"] and node["distribution_online"] is False
                for node in nodes
            )
        )
        stalled = desired_revision is not None and not converged and not degraded
        rollout_state = (
            "degraded" if degraded
            else "converged" if converged
            else "stalled" if stalled
            else "inactive"
        )
        return {
            "converged": converged,
            "healthy": converged and not degraded,
            "degraded": degraded,
            "stalled": stalled,
            "rollout_state": rollout_state,
            "desired_revision": desired_revision,
            "desired_digest": desired_digest,
            "desired_conflict": desired_conflict,
            "desired": {
                "revision": desired_revision,
                "digest": desired_digest,
                "conflict": desired_conflict,
            },
            "online_nodes": sum(1 for node in nodes if node["online"]),
            "total_nodes": len(nodes),
            "nodes": nodes,
        }


class VerificationPolicyPeerAuthenticator:
    """Verify one-time, request-bound credentials for the internal peer endpoint."""

    def __init__(
        self,
        node_id: str,
        identity_key: str,
        identity_key_id: str,
        identity_issuer: str,
        identity_audience: str,
        maximum_ttl_seconds: int,
        consume: Callable[[str, str, int], None],
    ):
        self.node_id = node_id
        self.identity_key = identity_key
        self.identity_key_id = identity_key_id
        self.identity_issuer = identity_issuer
        self.identity_audience = identity_audience
        self.maximum_ttl_seconds = maximum_ttl_seconds
        self.consume = consume

    def verify(self, authorization: str | None) -> dict:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise IdentityError("peer credential is required")
        identity = verify_identity(
            authorization[len(prefix):],
            self.identity_key,
            issuer=self.identity_issuer,
            audience=self.identity_audience,
            method="GET",
            path=PEER_STATUS_PATH,
            maximum_ttl_seconds=self.maximum_ttl_seconds,
            key_id=self.identity_key_id,
        )
        if (
            identity["operation"] != PEER_STATUS_OPERATION
            or identity["target"] != self.node_id
        ):
            raise IdentityError("peer credential claims do not match this node")
        try:
            self.consume(identity["jti"], identity["sub"], identity["exp"])
        except ValueError as exc:
            raise IdentityError(str(exc)) from exc
        return identity
