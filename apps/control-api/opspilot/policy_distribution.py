from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Protocol

import httpx


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
    ):
        self.node_id = node_id
        self.peers = dict(peers or {})
        self.timeout = timeout

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

    async def report(self, local_status: dict) -> dict:
        nodes = [self._node_status(self.node_id, local_status)]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for node_id, base_url in sorted(self.peers.items()):
                try:
                    response = await client.get(
                        f"{base_url.rstrip('/')}/api/v1/verification-policy/status"
                    )
                    response.raise_for_status()
                    nodes.append(self._node_status(node_id, response.json()))
                except Exception as exc:
                    nodes.append({
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
                    })

        accepted = {
            (node["accepted_revision"], node["accepted_digest"])
            for node in nodes if node["online"]
        }
        converged = (
            bool(nodes)
            and all(node["online"] for node in nodes)
            and len(accepted) == 1
            and next(iter(accepted))[0] is not None
            and all(
                node["load_result"] == "accepted"
                and node["observed_revision"] == node["accepted_revision"]
                and node["observed_digest"] == node["accepted_digest"]
                for node in nodes
            )
        )
        observed_revisions = [
            node["observed_revision"] for node in nodes
            if isinstance(node["observed_revision"], int)
        ]
        return {
            "converged": converged,
            "healthy": converged and all(
                node["distribution_online"] is not False for node in nodes
            ),
            "desired_revision": max(observed_revisions) if observed_revisions else None,
            "online_nodes": sum(1 for node in nodes if node["online"]),
            "total_nodes": len(nodes),
            "nodes": nodes,
        }
