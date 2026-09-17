# Control API access control local acceptance

Date: 2026-09-17

Batch: `control-api-access-r1-20260917`

Scope: local Docker Compose access-control seam; not production IAM/SSO/compliance evidence

## Result

The additive RS256 access layer passed the local acceptance matrix while preserving the existing
HTTP bodies and workflow boundaries. Roles are separated into `viewer`, `analyst`, `approver`,
`admin` and machine-only `alertmanager`. `/health` and the established public read-only
verification-policy status remain compatible.

The deployment acceptance passed 15/15 checks: public health; missing identity; wrong audience;
expired and tampered credentials; viewer read/analysis denial; analyst recommendation/approval
denial; Alertmanager webhook/read denial; verified approval; approval replay rejection; approval
audit fields; and recommendation-only zero execution/Verification rows.

The approval audit recorded subject `operator-alice`, role `approver`, request correlation and the
consumed credential ID. Reusing the same signed approval credential returned HTTP 401 before a
second workflow execution. The Alertmanager credential returned HTTP 200 for its empty webhook
probe and HTTP 403 for incident reads.

## Regression verification

- Focused access/persistence/Skill/Repair coverage: 32 passed.
- Complete backend suite: 204 passed.
- Both Control API images, the identity bootstrap and Dashboard production image built.
- Dashboard production build and its two rendered/security tests passed.
- Compose rendering and Alertmanager configuration passed.
- Smoke passed with service/dependency health, Tempo, runtime-log mTLS, Loki and
  recommendation-only non-execution.
- Live Redis remained `recommendation_ready`, then `awaiting_approval`, then reached
  `resolved / verified=true` only after a verified approval identity; only Redis restarted.
- Prometheus validated all nine deployed rules.

## Boundaries and limitations

The Dashboard reference proxy represents one local administrative subject and signs a fresh
short-lived credential per request. Its private key stays server-side. This demonstrates the
replaceable verifier and authorization/audit contract, not real multi-user login, session
management or organizational identity governance. Alertmanager uses a bootstrap-generated local
machine token with only webhook permission.

Production use still requires an external issuer/IdP, managed signing-key custody and rotation,
revocation, user lifecycle/session controls, TLS termination and independent security review.
No IAM, SSO, zero-trust, compliance or production-security claim is supported. Stage 12 remains
pending; this batch does not provide independent-host, managed-HA, SLA, RPO/RTO or zero-data-loss
evidence.
