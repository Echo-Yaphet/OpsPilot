import base64
import json
import time
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from opspilot import main
from opspilot.access_control import AccessIdentityError, ApiAccessAuthenticator
from opspilot.models import AnalyzeRequest, IncidentState
from opspilot.storage import IncidentStore


def encode(value: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def credential(private_key, *, roles, subject="human-1", audience="opspilot-control-api",
               now=None, credential_id=None, expires_in=60):
    issued = int(time.time()) if now is None else now
    header = encode({"alg": "RS256", "kid": "test-access-v1", "typ": "JWT"})
    claims = encode({
        "iss": "test-access-issuer", "aud": audience, "sub": subject,
        "roles": roles, "iat": issued, "exp": issued + expires_in,
        "jti": credential_id or str(uuid.uuid4()),
    })
    signed = f"{header}.{claims}"
    signature = private_key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signed}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def authenticator(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return private_key, ApiAccessAuthenticator(
        enabled=True, public_key_file=str(public_path), key_id="test-access-v1",
        issuer="test-access-issuer", audience="opspilot-control-api",
        maximum_ttl_seconds=300,
    )


def test_rs256_access_rejects_wrong_audience_expiry_and_tampering(tmp_path):
    private_key, auth = authenticator(tmp_path)
    now = int(time.time())
    valid = credential(private_key, roles=["viewer"], now=now)
    assert auth.authorize(f"Bearer {valid}", "read").subject == "human-1"

    for token in (
        credential(private_key, roles=["viewer"], audience="other", now=now),
        credential(private_key, roles=["viewer"], now=now - 100, expires_in=10),
        valid[:-1] + ("A" if valid[-1] != "A" else "B"),
    ):
        try:
            auth.authenticate(f"Bearer {token}", now=now)
        except AccessIdentityError:
            pass
        else:
            raise AssertionError("invalid credential was accepted")


class FakeWorkflow:
    async def run(self, request: AnalyzeRequest):
        return IncidentState(
            incident_id=request.incident_id or "authorized-incident",
            service=request.service,
            symptom=request.symptom,
            status="resolved" if request.execute and request.approved else "recommendation_ready",
            execution_requested=request.execute,
            execution_result="executed" if request.execute and request.approved else None,
            verified=True if request.execute and request.approved else None,
        )


def test_endpoint_role_matrix_approval_audit_and_replay(tmp_path, monkeypatch):
    private_key, auth = authenticator(tmp_path)
    store = IncidentStore(str(tmp_path / "incidents.db"))
    monkeypatch.setattr(main, "api_access_authenticator", auth)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "workflow", FakeWorkflow())
    client = TestClient(main.app)

    viewer = credential(private_key, roles=["viewer"])
    analyst = credential(private_key, roles=["analyst"])
    approver = credential(private_key, roles=["approver"], subject="operator-alice")
    alertmanager = credential(private_key, roles=["alertmanager"], subject="alertmanager-1")
    body = {"service": "payment-service", "symptom": "Redis unavailable"}

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/incidents", headers={"Authorization": f"Bearer {viewer}"}).status_code == 200
    assert client.post("/api/v1/incidents/analyze", headers={"Authorization": f"Bearer {viewer}"}, json=body).status_code == 403
    assert client.post("/api/v1/incidents/analyze", headers={"Authorization": f"Bearer {analyst}"}, json=body).status_code == 200
    assert client.post("/api/v1/incidents/analyze", headers={"Authorization": f"Bearer {analyst}"},
                       json={**body, "execute": True, "approved": True}).status_code == 403

    approval_headers = {"Authorization": f"Bearer {approver}", "X-Request-ID": "request-123"}
    approved = client.post("/api/v1/incidents/analyze", headers=approval_headers,
                           json={**body, "execute": True, "approved": True})
    assert approved.status_code == 200
    replay = client.post("/api/v1/incidents/analyze", headers=approval_headers,
                         json={**body, "execute": True, "approved": True})
    assert replay.status_code == 401
    with store.connection() as db:
        audit = db.execute(
            "SELECT approval_subject,approval_roles,request_id,credential_id FROM approvals"
        ).fetchone()
    assert audit["approval_subject"] == "operator-alice"
    assert json.loads(audit["approval_roles"]) == ["approver"]
    assert audit["request_id"] == "request-123"

    alert_headers = {"Authorization": f"Bearer {alertmanager}"}
    assert client.post("/api/v1/alertmanager/webhook", headers=alert_headers,
                       json={"status": "firing", "alerts": []}).status_code == 200
    assert client.get("/api/v1/incidents", headers=alert_headers).status_code == 403
    assert client.post("/api/v1/incidents/analyze", headers=alert_headers, json=body).status_code == 403


def test_compatibility_mode_preserves_existing_unauthenticated_api(tmp_path):
    auth = ApiAccessAuthenticator(
        enabled=False, public_key_file="unused", key_id="unused", issuer="unused",
        audience="unused", maximum_ttl_seconds=60,
    )
    principal = auth.authorize(None, "admin")
    assert principal.subject == "compatibility-anonymous"
