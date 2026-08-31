import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any


class IdentityError(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise IdentityError("credential is not valid base64url") from exc


def mint_identity(
    secret: str,
    *,
    issuer: str,
    audience: str,
    subject: str,
    ttl_seconds: int,
    method: str,
    path: str,
    operation: str,
    target: str,
    now: int | None = None,
    credential_id: str | None = None,
    key_id: str = "control-api-v1",
) -> str:
    if not secret:
        raise IdentityError("identity signing key is not configured")
    if not 1 <= ttl_seconds <= 60:
        raise IdentityError("identity TTL must be between 1 and 60 seconds")
    if not isinstance(key_id, str) or not key_id:
        raise IdentityError("identity signing key ID is not configured")
    issued_at = int(time.time()) if now is None else now
    header = {"alg": "HS256", "kid": key_id, "typ": "JWT"}
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": credential_id or str(uuid.uuid4()),
        "method": method.upper(),
        "path": path,
        "operation": operation,
        "target": target,
    }
    encoded_header = _encode(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_claims = _encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{encoded_header}.{encoded_claims}"
    signature = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest()
    return f"{signed}.{_encode(signature)}"


def verify_identity(
    token: str,
    secret: str | None = None,
    *,
    issuer: str,
    audience: str,
    method: str,
    path: str,
    maximum_ttl_seconds: int,
    clock_skew_seconds: int = 2,
    now: int | None = None,
    key_id: str = "control-api-v1",
    verification_keys: Mapping[str, str] | None = None,
    previous_key_id: str | None = None,
    previous_key_valid_until: int | None = None,
) -> dict[str, Any]:
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
    except ValueError as exc:
        raise IdentityError("credential must have three segments") from exc
    try:
        header = json.loads(_decode(encoded_header))
        claims = json.loads(_decode(encoded_claims))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IdentityError("credential JSON is invalid") from exc
    if not isinstance(header, dict) or set(header) != {"alg", "kid", "typ"}:
        raise IdentityError("credential header is invalid")
    if header["alg"] != "HS256" or header["typ"] != "JWT":
        raise IdentityError("credential header is invalid")
    token_key_id = header["kid"]
    if not isinstance(token_key_id, str) or not token_key_id:
        raise IdentityError("credential key ID is invalid")
    keys = dict(verification_keys) if verification_keys is not None else {key_id: secret or ""}
    verification_key = keys.get(token_key_id)
    if not verification_key:
        raise IdentityError("credential key ID is unknown")
    current = int(time.time()) if now is None else now
    if token_key_id == previous_key_id:
        if previous_key_valid_until is None or current > previous_key_valid_until:
            raise IdentityError("credential previous key overlap has expired")
    signed = f"{encoded_header}.{encoded_claims}"
    expected = hmac.new(verification_key.encode(), signed.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_decode(encoded_signature), expected):
        raise IdentityError("credential signature is invalid")
    required = {"iss", "aud", "sub", "iat", "exp", "jti", "method", "path", "operation", "target"}
    if not isinstance(claims, dict) or not required.issubset(claims):
        raise IdentityError("credential claims are incomplete")
    if claims["iss"] != issuer or claims["aud"] != audience:
        raise IdentityError("credential issuer or audience is invalid")
    if claims["method"] != method.upper() or claims["path"] != path:
        raise IdentityError("credential is not bound to this request")
    if not all(isinstance(claims[key], str) and claims[key] for key in ("sub", "jti", "operation", "target")):
        raise IdentityError("credential string claims are invalid")
    if not isinstance(claims["iat"], int) or not isinstance(claims["exp"], int):
        raise IdentityError("credential timestamps are invalid")
    if claims["iat"] > current + clock_skew_seconds:
        raise IdentityError("credential is not active yet")
    if claims["exp"] < current - clock_skew_seconds:
        raise IdentityError("credential has expired")
    if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > maximum_ttl_seconds:
        raise IdentityError("credential lifetime is invalid")
    return {**claims, "key_id": token_key_id}
