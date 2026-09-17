#!/usr/bin/env python3
import base64
import json
import time
import uuid
from pathlib import Path

import httpx
import psycopg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


API = "http://control-api:8080"
PRIVATE_KEY = serialization.load_pem_private_key(
    Path("/identity/access-private/private.pem").read_bytes(), password=None
)


def encode(value: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def token(*, subject: str, roles: list[str], audience: str = "opspilot-control-api",
          issued_at: int | None = None, expires_in: int = 60, credential_id: str | None = None) -> str:
    now = int(time.time()) if issued_at is None else issued_at
    header = encode({"alg": "RS256", "kid": "opspilot-api-access-v1", "typ": "JWT"})
    claims = encode({
        "iss": "opspilot-local-access-issuer", "aud": audience, "sub": subject,
        "roles": roles, "iat": now, "exp": now + expires_in,
        "jti": credential_id or str(uuid.uuid4()),
    })
    signed = f"{header}.{claims}"
    signature = PRIVATE_KEY.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signed}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def headers(value: str, request_id: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {value}"}
    if request_id:
        result["X-Request-ID"] = request_id
    return result


def require(name: str, condition: bool, detail: object) -> dict:
    if not condition:
        raise RuntimeError(f"{name} failed: {detail}")
    return {"check": name, "passed": True, "detail": detail}


def main() -> None:
    checks = []
    with httpx.Client(timeout=15) as client:
        checks.append(require("health_public", client.get(f"{API}/health").status_code == 200, 200))
        checks.append(require("missing_identity", client.get(f"{API}/api/v1/incidents").status_code == 401, 401))
        wrong_aud = token(subject="wrong-audience", roles=["viewer"], audience="other-api")
        checks.append(require("wrong_audience", client.get(
            f"{API}/api/v1/incidents", headers=headers(wrong_aud)
        ).status_code == 401, 401))
        expired = token(subject="expired", roles=["viewer"], issued_at=int(time.time()) - 120, expires_in=10)
        checks.append(require("expired", client.get(
            f"{API}/api/v1/incidents", headers=headers(expired)
        ).status_code == 401, 401))
        valid = token(subject="tampered", roles=["viewer"])
        tampered = valid[:-1] + ("A" if valid[-1] != "A" else "B")
        checks.append(require("tampered", client.get(
            f"{API}/api/v1/incidents", headers=headers(tampered)
        ).status_code == 401, 401))

        viewer = token(subject="viewer-1", roles=["viewer"])
        checks.append(require("viewer_read", client.get(
            f"{API}/api/v1/incidents?limit=1", headers=headers(viewer)
        ).status_code == 200, 200))
        body = {"service": "payment-service", "symptom": "local access validation"}
        checks.append(require("viewer_cannot_analyze", client.post(
            f"{API}/api/v1/incidents/analyze", headers=headers(viewer), json=body
        ).status_code == 403, 403))

        recommendation_id = f"access-recommendation-{uuid.uuid4()}"
        analyst = token(subject="analyst-1", roles=["analyst"])
        recommendation = client.post(
            f"{API}/api/v1/incidents/analyze", headers=headers(analyst),
            json={**body, "incident_id": recommendation_id, "execute": False, "approved": False},
        )
        checks.append(require("analyst_recommendation", recommendation.status_code == 200,
                              recommendation.status_code))
        checks.append(require("analyst_cannot_approve", client.post(
            f"{API}/api/v1/incidents/analyze", headers=headers(analyst),
            json={**body, "execute": True, "approved": True},
        ).status_code == 403, 403))

        machine = token(subject="alertmanager-validation", roles=["alertmanager"])
        checks.append(require("alertmanager_webhook", client.post(
            f"{API}/api/v1/alertmanager/webhook", headers=headers(machine),
            json={"status": "firing", "alerts": []},
        ).status_code == 200, 200))
        checks.append(require("alertmanager_cannot_read", client.get(
            f"{API}/api/v1/incidents", headers=headers(machine)
        ).status_code == 403, 403))

        approval_id = f"access-approval-{uuid.uuid4()}"
        request_id = f"request-{uuid.uuid4()}"
        credential_id = str(uuid.uuid4())
        approver = token(subject="operator-alice", roles=["approver"], credential_id=credential_id)
        approval_body = {**body, "incident_id": approval_id, "execute": True, "approved": True}
        approved = client.post(
            f"{API}/api/v1/incidents/analyze", headers=headers(approver, request_id),
            json=approval_body,
        )
        checks.append(require("verified_approval", approved.status_code == 200, approved.status_code))
        replay = client.post(
            f"{API}/api/v1/incidents/analyze", headers=headers(approver, request_id),
            json=approval_body,
        )
        checks.append(require("approval_replay", replay.status_code == 401, replay.status_code))

    dsn = "postgresql://opspilot_memory:opspilot_memory_local@memory-db:5432/opspilot_memory"
    with psycopg.connect(dsn) as db:
        audit = db.execute(
            "SELECT approval_subject,approval_roles,request_id,credential_id FROM approvals "
            "WHERE incident_id=%s ORDER BY id DESC LIMIT 1", (approval_id,),
        ).fetchone()
        side_effects = db.execute(
            "SELECT (SELECT count(*) FROM executions WHERE incident_id=%s), "
            "(SELECT count(*) FROM verifications WHERE incident_id=%s)",
            (recommendation_id, recommendation_id),
        ).fetchone()
    checks.append(require("approval_audit", audit == (
        "operator-alice", '["approver"]', request_id, credential_id,
    ), audit))
    checks.append(require("recommendation_only", side_effects == (0, 0), side_effects))
    print(json.dumps({"passed": True, "checks": checks}, indent=2, default=str))


if __name__ == "__main__":
    main()
