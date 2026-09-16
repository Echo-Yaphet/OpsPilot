from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Awaitable, Callable, Mapping

import httpx
from issuer_client import request_identity

from opspilot.config import (
    SignedVerificationPolicyBundle,
    VerificationPolicy,
    VerificationPolicyDocument,
    verification_policy_content_digest,
    verification_policy_signature,
)
from opspilot.policy_distribution import PEER_STATUS_OPERATION, PEER_STATUS_PATH


StatusReader = Callable[[str, str], Awaitable[dict]]
PhaseHook = Callable[[str], None]


@dataclass(frozen=True)
class RolloutResult:
    state: str
    revision: int
    digest: str
    accepted_nodes: tuple[str, ...]
    pending_nodes: tuple[str, ...]
    quorum: int

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "revision": self.revision,
            "digest": self.digest,
            "accepted_nodes": list(self.accepted_nodes),
            "pending_nodes": list(self.pending_nodes),
            "quorum": self.quorum,
        }


class RolloutError(RuntimeError):
    pass


def validate_bundle(content: bytes, signing_keys: Mapping[str, str]) -> SignedVerificationPolicyBundle:
    try:
        bundle = SignedVerificationPolicyBundle.model_validate_json(content)
        actual_digest = verification_policy_content_digest(bundle.policy)
        if not hmac.compare_digest(actual_digest, bundle.content_digest):
            raise ValueError("content digest does not match policy")
        key = signing_keys.get(bundle.key_id)
        if key is None:
            raise ValueError(f"unknown signing key ID: {bundle.key_id}")
        expected = verification_policy_signature(
            bundle.key_id, bundle.revision, bundle.content_digest, key
        )
        if not hmac.compare_digest(expected, bundle.signature):
            raise ValueError("invalid bundle signature")
        document = VerificationPolicyDocument.model_validate(bundle.policy)
        default = VerificationPolicy.model_validate({
            **VerificationPolicy().model_dump(),
            **document.defaults.model_dump(exclude_none=True),
        })
        for override in document.services.values():
            VerificationPolicy.model_validate({
                **default.model_dump(),
                **override.model_dump(exclude_none=True),
            })
        return bundle
    except Exception as exc:
        raise RolloutError(f"candidate bundle rejected: {exc}") from exc


class VerificationPolicyRolloutController:
    def __init__(
        self,
        *,
        signing_keys: Mapping[str, str],
        nodes: Mapping[str, str],
        canary_nodes: tuple[str, ...],
        quorum: int,
        status_reader: StatusReader,
        timeout_seconds: float = 30,
        poll_interval_seconds: float = 1,
        audit_file: str | None = None,
        phase_hook: PhaseHook | None = None,
    ):
        self.signing_keys = dict(signing_keys)
        self.nodes = dict(nodes)
        self.canary_nodes = canary_nodes
        self.quorum = quorum
        self.status_reader = status_reader
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.audit_file = Path(audit_file) if audit_file else None
        self.phase_hook = phase_hook
        self._plan_digest: str | None = None
        if not self.nodes:
            raise RolloutError("at least one rollout node is required")
        if not self.canary_nodes or any(node not in self.nodes for node in self.canary_nodes):
            raise RolloutError("canary nodes must be a non-empty subset of rollout nodes")
        if not 1 <= self.quorum <= len(self.nodes):
            raise RolloutError("quorum must be between one and the node count")
        if timeout_seconds <= 0 or poll_interval_seconds < 0:
            raise RolloutError("rollout timeout must be positive and poll interval non-negative")

    def _audit(self, event: str, bundle: SignedVerificationPolicyBundle, **details) -> None:
        if self.audit_file is None:
            return
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": time.time(),
            "event": event,
            "revision": bundle.revision,
            "digest": bundle.content_digest,
            **({"plan_digest": self._plan_digest} if self._plan_digest else {}),
            **details,
        }
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _record(self, event: str, bundle: SignedVerificationPolicyBundle, **details) -> None:
        self._audit(event, bundle, **details)
        if self.phase_hook is not None:
            self.phase_hook(event)

    def _rollout_plan_digest(self, bundle: SignedVerificationPolicyBundle) -> str:
        plan = {
            "revision": bundle.revision,
            "digest": bundle.content_digest,
            "nodes": sorted(self.nodes.items()),
            "canary_nodes": sorted(self.canary_nodes),
            "quorum": self.quorum,
        }
        encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{sha256(encoded).hexdigest()}"

    def _resume_events(self, bundle: SignedVerificationPolicyBundle) -> tuple[str, ...]:
        if self.audit_file is None or not self.audit_file.exists():
            return ()
        matching: list[str] = []
        for line_number, line in enumerate(
            self.audit_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RolloutError(
                    f"audit file is corrupt at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(entry, dict):
                raise RolloutError(f"audit file entry {line_number} must be an object")
            if (
                entry.get("revision") != bundle.revision
                or entry.get("digest") != bundle.content_digest
            ):
                continue
            recorded_plan = entry.get("plan_digest")
            if recorded_plan is None:
                continue
            if recorded_plan != self._plan_digest:
                raise RolloutError(
                    "rollout plan conflicts with the durable audit for this candidate"
                )
            event = entry.get("event")
            if isinstance(event, str):
                matching.append(event)
        return tuple(matching)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _check_revision(self, stable_path: Path, candidate: SignedVerificationPolicyBundle) -> bool:
        try:
            active_content = stable_path.read_bytes()
        except FileNotFoundError:
            return False
        active = validate_bundle(active_content, self.signing_keys)
        if candidate.revision < active.revision:
            raise RolloutError(
                f"revision rollback rejected: {candidate.revision} < {active.revision}"
            )
        if candidate.revision == active.revision:
            if candidate.content_digest != active.content_digest:
                raise RolloutError(
                    f"revision {candidate.revision} conflicts with the stable digest"
                )
            return True
        return False

    @staticmethod
    def _accepted(status: dict, bundle: SignedVerificationPolicyBundle) -> bool:
        return (
            status.get("load_result") == "accepted"
            and status.get("bundle_revision") == bundle.revision
            and status.get("content_digest") == bundle.content_digest
        )

    async def _wait_for(
        self,
        node_ids: tuple[str, ...],
        bundle: SignedVerificationPolicyBundle,
        required: int,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        deadline = time.monotonic() + self.timeout_seconds
        latest: dict[str, dict] = {}
        while True:
            results = await asyncio.gather(
                *(self.status_reader(node, self.nodes[node]) for node in node_ids),
                return_exceptions=True,
            )
            for node, result in zip(node_ids, results):
                if isinstance(result, Exception):
                    latest[node] = {"error": str(result)}
                    continue
                latest[node] = result
                accepted_revision = result.get("bundle_revision")
                accepted_digest = result.get("content_digest")
                if (
                    isinstance(accepted_revision, int)
                    and accepted_revision >= bundle.revision
                    and (
                        accepted_revision > bundle.revision
                        or accepted_digest != bundle.content_digest
                    )
                ):
                    raise RolloutError(
                        f"node {node} accepted conflicting revision/digest "
                        f"{accepted_revision}/{accepted_digest}"
                    )
            accepted = tuple(
                node for node in node_ids if self._accepted(latest.get(node, {}), bundle)
            )
            if len(accepted) >= required:
                return accepted, tuple(node for node in node_ids if node not in accepted)
            if time.monotonic() >= deadline:
                summary = {
                    node: {
                        "revision": status.get("bundle_revision"),
                        "digest": status.get("content_digest"),
                        "load_result": status.get("load_result"),
                        "error": status.get("error") or status.get("last_error"),
                    }
                    for node, status in latest.items()
                }
                raise RolloutError(
                    f"timed out waiting for {required}/{len(node_ids)} acceptances: {summary}"
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def rollout(
        self,
        *,
        candidate_path: str,
        canary_path: str,
        stable_path: str,
        approved: bool,
    ) -> RolloutResult:
        if not approved:
            raise RolloutError("explicit rollout approval is required")
        content = Path(candidate_path).read_bytes()
        bundle = validate_bundle(content, self.signing_keys)
        self._plan_digest = self._rollout_plan_digest(bundle)
        resume_events = self._resume_events(bundle)
        if not resume_events:
            self._record("candidate_validated", bundle, approved=True)
        elif "quorum_reached" in resume_events:
            stable = Path(stable_path)
            if not self._check_revision(stable, bundle):
                raise RolloutError("durable quorum result conflicts with the stable bundle")
            entries = [
                json.loads(line)
                for line in self.audit_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            completed = next(
                entry
                for entry in reversed(entries)
                if entry.get("event") == "quorum_reached"
                and entry.get("revision") == bundle.revision
                and entry.get("digest") == bundle.content_digest
                and entry.get("plan_digest") == self._plan_digest
            )
            return RolloutResult(
                state="quorum_committed",
                revision=bundle.revision,
                digest=bundle.content_digest,
                accepted_nodes=tuple(completed.get("accepted_nodes", ())),
                pending_nodes=tuple(completed.get("pending_nodes", ())),
                quorum=self.quorum,
            )
        else:
            self._record(
                "rollout_resumed",
                bundle,
                last_durable_event=resume_events[-1],
            )
        try:
            stable = Path(stable_path)
            already_stable = self._check_revision(stable, bundle)
            if not already_stable:
                canary = Path(canary_path)
                try:
                    canary_bundle = validate_bundle(canary.read_bytes(), self.signing_keys)
                    already_canary = (
                        canary_bundle.revision == bundle.revision
                        and canary_bundle.content_digest == bundle.content_digest
                    )
                except (FileNotFoundError, RolloutError):
                    already_canary = False
                if not already_canary:
                    self._atomic_write(canary, content)
                    self._record("canary_published", bundle, nodes=list(self.canary_nodes))
                accepted, _ = await self._wait_for(
                    self.canary_nodes, bundle, len(self.canary_nodes)
                )
                canary_event = (
                    "canary_revalidated" if "canary_accepted" in resume_events else "canary_accepted"
                )
                self._record(canary_event, bundle, nodes=list(accepted))
                self._atomic_write(stable, content)
                self._record("stable_published", bundle)

            node_ids = tuple(sorted(self.nodes))
            accepted, pending = await self._wait_for(node_ids, bundle, self.quorum)
            result = RolloutResult(
                state="quorum_committed",
                revision=bundle.revision,
                digest=bundle.content_digest,
                accepted_nodes=accepted,
                pending_nodes=pending,
                quorum=self.quorum,
            )
            self._record("quorum_reached", bundle, **result.as_dict())
            return result
        except Exception as exc:
            self._audit("rollout_failed", bundle, error=str(exc))
            raise


class AuthenticatedNodeStatusReader:
    def __init__(
        self,
        *,
        identity_issuer_url: str,
        identity_private_key_file: str,
        identity_subject: str,
        identity_audience: str,
        identity_ttl_seconds: int,
        request_timeout_seconds: float,
    ):
        self.identity_issuer_url = identity_issuer_url
        self.identity_private_key_file = identity_private_key_file
        self.identity_subject = identity_subject
        self.identity_audience = identity_audience
        self.identity_ttl_seconds = identity_ttl_seconds
        self.request_timeout_seconds = request_timeout_seconds

    async def __call__(self, node_id: str, base_url: str) -> dict:
        credential = await request_identity(
            self.identity_issuer_url,
            self.identity_private_key_file,
            self.identity_subject,
            audience=self.identity_audience,
            ttl_seconds=self.identity_ttl_seconds,
            method="GET",
            path=PEER_STATUS_PATH,
            operation=PEER_STATUS_OPERATION,
            target=node_id,
            timeout=self.request_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}{PEER_STATUS_PATH}",
                headers={"Authorization": f"Bearer {credential}"},
            )
            response.raise_for_status()
            return response.json()


def _json_object(raw: str, name: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RolloutError(f"{name} must be valid JSON") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) or not key or not item
        for key, item in value.items()
    ):
        raise RolloutError(f"{name} must be a non-empty string-to-string JSON object")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage a signed verification policy through canary and quorum")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--canary-bundle", required=True)
    parser.add_argument("--stable-bundle", required=True)
    parser.add_argument("--nodes-json", required=True)
    parser.add_argument("--canary-nodes", required=True, help="comma-separated node IDs")
    parser.add_argument("--quorum", required=True, type=int)
    parser.add_argument("--audit-file", required=True)
    parser.add_argument("--result-file")
    parser.add_argument("--approved", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--request-timeout", type=float, default=2)
    parser.add_argument(
        "--interrupt-after",
        choices=("candidate_validated", "canary_published", "canary_accepted", "stable_published"),
        help="test-only crash injection after the named durable phase",
    )
    return parser.parse_args()


async def _main() -> None:
    args = parse_args()
    signing_keys = _json_object(
        os.getenv("VERIFICATION_POLICY_SIGNING_KEYS", "{}"),
        "VERIFICATION_POLICY_SIGNING_KEYS",
    )
    nodes = _json_object(args.nodes_json, "nodes")
    status_reader = AuthenticatedNodeStatusReader(
        identity_issuer_url=os.getenv(
            "WORKLOAD_IDENTITY_ISSUER_URL", "http://workload-identity-issuer:8085"
        ),
        identity_private_key_file=os.getenv(
            "WORKLOAD_IDENTITY_PRIVATE_KEY_FILE", "/identity/policy-controller-private/private.pem"
        ),
        identity_subject="verification-policy-rollout-controller",
        identity_audience=os.getenv(
            "VERIFICATION_POLICY_PEER_IDENTITY_AUDIENCE",
            "opspilot-verification-policy-peer",
        ),
        identity_ttl_seconds=int(
            os.getenv("VERIFICATION_POLICY_PEER_IDENTITY_TTL_SECONDS", "10")
        ),
        request_timeout_seconds=args.request_timeout,
    )
    def interrupt_after(event: str) -> None:
        if event == args.interrupt_after:
            os._exit(75)

    controller = VerificationPolicyRolloutController(
        signing_keys=signing_keys,
        nodes=nodes,
        canary_nodes=tuple(node for node in args.canary_nodes.split(",") if node),
        quorum=args.quorum,
        status_reader=status_reader,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
        audit_file=args.audit_file,
        phase_hook=interrupt_after if args.interrupt_after else None,
    )
    result = await controller.rollout(
        candidate_path=args.candidate,
        canary_path=args.canary_bundle,
        stable_path=args.stable_bundle,
        approved=args.approved,
    )
    if args.result_file:
        controller._atomic_write(
            Path(args.result_file),
            (json.dumps(result.as_dict(), sort_keys=True, indent=2) + "\n").encode(),
        )
    print(json.dumps(result.as_dict(), sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (OSError, RolloutError, ValueError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc
