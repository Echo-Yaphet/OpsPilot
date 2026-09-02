import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


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


def mint_external_identity(
    private_key_pem: bytes,
    *,
    key_id: str,
    issuer: str,
    audience: str,
    subject: str,
    ttl_seconds: int,
    method: str,
    path: str,
    operation: str,
    target: str,
    placement: str | None = None,
    now: int | None = None,
    credential_id: str | None = None,
) -> str:
    """Mint an RS256 credential. Only the external issuer should call this."""
    if not 1 <= ttl_seconds <= 60:
        raise IdentityError("identity TTL must be between 1 and 60 seconds")
    issued_at = int(time.time()) if now is None else now
    header = {"alg": "RS256", "kid": key_id, "typ": "JWT"}
    claims = {
        "iss": issuer, "aud": audience, "sub": subject, "iat": issued_at,
        "exp": issued_at + ttl_seconds, "jti": credential_id or str(uuid.uuid4()),
        "method": method.upper(), "path": path, "operation": operation, "target": target,
    }
    if placement is not None:
        if not isinstance(placement, str) or not placement:
            raise IdentityError("identity placement is invalid")
        claims["placement"] = placement
    encoded_header = _encode(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_claims = _encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{encoded_header}.{encoded_claims}".encode()
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{signed.decode()}.{_encode(signature)}"


def verify_external_identity(
    token: str,
    public_key_pem: bytes,
    *,
    key_id: str,
    issuer: str,
    audience: str,
    method: str,
    path: str,
    maximum_ttl_seconds: int,
    clock_skew_seconds: int = 2,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify an externally issued RS256 credential and its request bindings."""
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = json.loads(_decode(encoded_header))
        claims = json.loads(_decode(encoded_claims))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IdentityError("credential encoding is invalid") from exc
    if header != {"alg": "RS256", "kid": key_id, "typ": "JWT"}:
        raise IdentityError("credential header or external issuer key ID is invalid")
    try:
        key = serialization.load_pem_public_key(public_key_pem)
        key.verify(
            _decode(encoded_signature), f"{encoded_header}.{encoded_claims}".encode(),
            padding.PKCS1v15(), hashes.SHA256(),
        )
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise IdentityError("credential signature is invalid") from exc
    required = {"iss", "aud", "sub", "iat", "exp", "jti", "method", "path", "operation", "target"}
    if not isinstance(claims, dict) or not required.issubset(claims):
        raise IdentityError("credential claims are incomplete")
    if claims["iss"] != issuer or claims["aud"] != audience:
        raise IdentityError("credential issuer or audience is invalid")
    if claims["method"] != method.upper() or claims["path"] != path:
        raise IdentityError("credential is not bound to this request")
    if not all(isinstance(claims[key], str) and claims[key] for key in ("sub", "jti", "operation", "target")):
        raise IdentityError("credential string claims are invalid")
    if "placement" in claims and (not isinstance(claims["placement"], str) or not claims["placement"]):
        raise IdentityError("credential placement is invalid")
    current = int(time.time()) if now is None else now
    if not isinstance(claims["iat"], int) or not isinstance(claims["exp"], int):
        raise IdentityError("credential timestamps are invalid")
    if claims["iat"] > current + clock_skew_seconds:
        raise IdentityError("credential is not active yet")
    if claims["exp"] < current - clock_skew_seconds:
        raise IdentityError("credential has expired")
    if claims["exp"] <= claims["iat"] or claims["exp"] - claims["iat"] > maximum_ttl_seconds:
        raise IdentityError("credential lifetime is invalid")
    return {**claims, "key_id": key_id}


def sign_issuer_request(private_key_pem: bytes, subject: str, payload: Mapping[str, Any], *, now: int | None = None, nonce: str | None = None) -> dict[str, str]:
    """Create a short-lived proof that lets one workload call the external issuer."""
    timestamp = str(int(time.time()) if now is None else now)
    request_nonce = nonce or str(uuid.uuid4())
    canonical = json.dumps(
        {"nonce": request_nonce, "payload": dict(payload), "subject": subject, "timestamp": timestamp},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    return {
        "X-Workload-Subject": subject,
        "X-Workload-Timestamp": timestamp,
        "X-Workload-Nonce": request_nonce,
        "X-Workload-Signature": _encode(signature),
    }


def verify_issuer_request(public_key_pem: bytes, subject: str, timestamp: str, nonce: str, signature: str, payload: Mapping[str, Any], *, now: int | None = None, maximum_age_seconds: int = 10) -> None:
    try:
        issued_at = int(timestamp)
    except ValueError as exc:
        raise IdentityError("issuer request timestamp is invalid") from exc
    current = int(time.time()) if now is None else now
    if abs(current - issued_at) > maximum_age_seconds:
        raise IdentityError("issuer request has expired")
    canonical = json.dumps(
        {"nonce": nonce, "payload": dict(payload), "subject": subject, "timestamp": timestamp},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    try:
        key = serialization.load_pem_public_key(public_key_pem)
        key.verify(_decode(signature), canonical, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise IdentityError("issuer request signature is invalid") from exc
