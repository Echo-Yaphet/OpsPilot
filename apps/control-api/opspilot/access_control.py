from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class AccessIdentityError(ValueError):
    pass


ROLE_PERMISSIONS = {
    "viewer": frozenset({"read"}),
    "analyst": frozenset({"read", "analyze", "repair_propose"}),
    "approver": frozenset({"read", "analyze", "approve", "repair_approve"}),
    "admin": frozenset({"read", "analyze", "approve", "repair_propose", "repair_approve", "admin"}),
    "alertmanager": frozenset({"alert_webhook"}),
}


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise AccessIdentityError("access credential is not valid base64url") from exc


@dataclass(frozen=True)
class AccessPrincipal:
    subject: str
    roles: tuple[str, ...]
    credential_id: str
    issued_at: int
    expires_at: int

    @property
    def permissions(self) -> frozenset[str]:
        permissions: set[str] = set()
        for role in self.roles:
            permissions.update(ROLE_PERMISSIONS[role])
        return frozenset(permissions)


class ApiAccessAuthenticator:
    """Verify RS256 access tokens without owning credential issuance."""

    def __init__(self, *, enabled: bool, public_key_file: str, key_id: str, issuer: str,
                 audience: str, maximum_ttl_seconds: int, clock_skew_seconds: int = 2):
        self.enabled = enabled
        self.public_key_file = public_key_file
        self.key_id = key_id
        self.issuer = issuer
        self.audience = audience
        self.maximum_ttl_seconds = maximum_ttl_seconds
        self.clock_skew_seconds = clock_skew_seconds

    def authenticate(self, authorization: str | None, *, now: int | None = None) -> AccessPrincipal:
        if not self.enabled:
            return AccessPrincipal(
                "compatibility-anonymous", ("admin", "alertmanager"),
                "compatibility-mode", 0, 2**31 - 1,
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise AccessIdentityError("RS256 access credential is required")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            encoded_header, encoded_claims, encoded_signature = token.split(".")
            header = json.loads(_decode(encoded_header))
            claims = json.loads(_decode(encoded_claims))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AccessIdentityError("access credential encoding is invalid") from exc
        if header != {"alg": "RS256", "kid": self.key_id, "typ": "JWT"}:
            raise AccessIdentityError("access credential header or key ID is invalid")
        try:
            key = serialization.load_pem_public_key(Path(self.public_key_file).read_bytes())
            key.verify(_decode(encoded_signature), f"{encoded_header}.{encoded_claims}".encode(),
                       padding.PKCS1v15(), hashes.SHA256())
        except (OSError, ValueError, TypeError, InvalidSignature) as exc:
            raise AccessIdentityError("access credential signature is invalid") from exc
        required = {"iss", "aud", "sub", "roles", "iat", "exp", "jti"}
        if not isinstance(claims, dict) or not required.issubset(claims):
            raise AccessIdentityError("access credential claims are incomplete")
        if claims["iss"] != self.issuer or claims["aud"] != self.audience:
            raise AccessIdentityError("access credential issuer or audience is invalid")
        if not all(isinstance(claims[key], str) and claims[key] for key in ("sub", "jti")):
            raise AccessIdentityError("access credential subject or ID is invalid")
        roles = claims["roles"]
        if not isinstance(roles, list) or not roles or not all(
            isinstance(role, str) and role in ROLE_PERMISSIONS for role in roles
        ):
            raise AccessIdentityError("access credential roles are invalid")
        if not isinstance(claims["iat"], int) or not isinstance(claims["exp"], int):
            raise AccessIdentityError("access credential timestamps are invalid")
        current = int(time.time()) if now is None else now
        if claims["iat"] > current + self.clock_skew_seconds:
            raise AccessIdentityError("access credential is not active yet")
        if claims["exp"] < current - self.clock_skew_seconds:
            raise AccessIdentityError("access credential has expired")
        lifetime = claims["exp"] - claims["iat"]
        if lifetime <= 0 or lifetime > self.maximum_ttl_seconds:
            raise AccessIdentityError("access credential lifetime is invalid")
        return AccessPrincipal(claims["sub"], tuple(dict.fromkeys(roles)), claims["jti"],
                               claims["iat"], claims["exp"])

    def authorize(self, authorization: str | None, permission: str) -> AccessPrincipal:
        principal = self.authenticate(authorization)
        if permission not in principal.permissions:
            raise AccessIdentityError(f"access identity lacks {permission} permission")
        return principal


def approval_audit(principal: AccessPrincipal, request_id: str) -> dict:
    return {"subject": principal.subject, "roles": list(principal.roles),
            "credential_id": principal.credential_id, "expires_at": principal.expires_at,
            "request_id": request_id}
