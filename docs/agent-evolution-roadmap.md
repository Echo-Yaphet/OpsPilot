# Agent evolution implementation plan

This document separates shipped behavior from the resume-driven target design.

## Stage 1: bounded autonomous investigation

Implemented as an opt-in Coordinator seam using OpenAI Agents SDK 0.22.0.
The existing LangGraph workflow and `IncidentWorkflow.run()` remain compatible.
The SDK runner chooses between four service-bound read-only observations, sees
each tool result, and can choose another observation before returning a summary.
Mandatory workflow evidence collection remains independent of model choices.

Configure `LLM_BASE_URL`, `LLM_MODEL`, and `INVESTIGATION_MODE=agents_sdk`.
The current adapter uses Ollama's OpenAI-compatible Chat Completions endpoint,
does not send data to OpenAI, disables SDK trace export, and disables client retries.
`INVESTIGATION_MAX_TURNS` (5), `INVESTIGATION_MAX_TOOL_CALLS` (6),
`INVESTIGATION_MAX_TOTAL_TOKENS` (4096), and `INVESTIGATION_TIMEOUT` (120 seconds)
bound each lifecycle. A busy investigator rejects
new model work immediately; degraded SDK runs skip downstream model RCA and
verification explanations while deterministic investigation/recovery continues.
Each model response is limited to 512 output tokens. Persisted aggregate usage gates
resume admission and reduces remaining output allowance. The provider reports input
usage only after a request, so one admitted request can cross the aggregate threshold;
a provider may also continue generation after cancellation.

`llm_investigation` evidence includes model, run ID, observations, elapsed time,
termination status and current plus aggregate token usage. Partial observations are
checkpointed before and after each probe. A process crash leaves a `running` record;
the next matching incident resumes the same run ID, accumulated tool/token budgets,
completed observations and deterministic compacted context.
SDK investigation summaries do not control commands, targets or verified results.
Only completed, untruncated tool observations feed the existing RCA adapter.

## Stage 2: sandbox configuration repair

Implemented as the opt-in `repair-lab` Compose profile. An intentionally
misconfigured payment replica, dedicated Redis fixture, independent validator and
non-root sandbox are isolated from the existing runtime executors. The sandbox has
a read-only root, no capabilities, no Docker socket, internal-only networks and
bounded PID/CPU/memory resources.

The SDK Repair Agent can read only the fixed workspace, generate a diagnostic script
manifest from two allowlisted Shell steps, execute that server-owned script ID and
submit a typed Redis configuration candidate. It cannot choose a production target,
approve, apply or declare verification. The validator binds service version and Redis
endpoint scope and performs a real PING before the sandbox creates an immutable package.
The Control API persists that package and requires explicit approval; its HMAC credential
binds package ID/digest, target, base digest, expiry and one-use `jti`. The sandbox repeats
validation, probes the disposable replica independently and rolls back on failure.

`make repair-lab-validate` covers missing identity, unlisted Shell, generated-script
path ownership, invalid candidates, invalid signatures, stale/tampered packages,
approval replay, initial 503 and repaired 200 health, plus effective container isolation.
`make repair-agent-live` exercises the full local Qwen3.5 proposal and approval path.

## Stage 3: harness and memory

Implemented. Resumable SDK lifecycle is persisted separately from completed incident
snapshots and carries one run ID across process interruption. Aggregate tool/token
budgets are enforced across resumes. Deterministic compaction retains content-addressed
evidence references, counterevidence, action outcomes and open questions.

The default Compose control plane uses shared PostgreSQL for incidents, normalized
audit rows, investigation checkpoints and the future `skill_versions` registry. Startup
performs an advisory-lock-protected, one-time SQLite import in one transaction; tests
cover real migration and failed-DDL rollback. SQLite remains a supported local fallback.
Event memory uses pgvector only after SQL filters for service, service version, conditions
and expiry. Missing embeddings retain filtered recency ordering and memory failure never
blocks deterministic investigation.

Control API plus all three business services emit OTLP traces to an OpenTelemetry
Collector, which exports to Tempo. Grafana provisions Tempo as a read-only data source.
No Trace tool is exposed to the model yet; Trace remains observability, not authority.

## Stage 4: candidate skill promotion

Implemented. Reproducible failure trajectories are frozen in a server-owned case set
with a content digest. An authenticated coding-agent client can submit only typed
diagnostic instructions and non-executable repair guidance against an active parent.
Each candidate receives an isolated, read-only workspace plus a logical branch name and
preserves the trigger case/digest, content diff, deterministic evaluator version, complete
regression/counterexample results, parent version and rollback pointer.

The candidate schema cannot represent probes, gates, evaluation inputs/labels, production
targets, commands, approval or verification truth. Original Redis/MySQL regressions and
healthy/unrelated counterexamples are loaded only from the image-owned fixture. Candidate
creation never changes the active Skill; a separate authenticated request with explicit
`approved=true` is required, and stale-parent or failed candidates cannot be promoted.
Promoted guidance is advisory context for SDK investigation while mandatory probes and all
execution/verification controls remain independent.

## Stage 5: held-out evaluation

Status: completed on 2026-09-10. See
[`docs/evaluations/stage5-v2-v3-report.md`](evaluations/stage5-v2-v3-report.md) for the
formal 64-trial result, integrity controls, cost/latency comparison and limitations.

Split cases by time and topology; do not expose held-out answers to skill generation.
Fix model, tool access, budgets and baseline. Include combined faults after single-fault
repair works. Record numerator/denominator, repetitions and latency for all rates.
Recovery success means independent probe success within budget. Cost per success
includes failed attempts. Regression rate means previously passing cases that fail
with the new version. The proposed +10 percentage points recovery, -20% cost and <=3%
regression values are targets, not results. A 100% bypass interception claim applies
only to the enumerated adversarial test set, never to all possible actions.

## Stage 6: combined-dependency recovery

Status: completed on 2026-09-15.

The deterministic RCA now represents simultaneous Redis and MySQL failures as one
combined root cause and owns the ordered target list. Solution emits one compatible
`Recommendation` per target. Safety evaluates the complete batch before execution and
denies the whole plan if any action is absent or rejected; approval remains one explicit
human decision for the displayed plan. Executor runs the already-approved typed actions
in deterministic order, stops after the first failure and records completed/failed target
evidence. Verification resolves one immutable service policy and requires every target
container, every target dependency metric and service health to stabilize together.

This stage does not give the model authority over targets or ordering, add arbitrary
commands, weaken Gateway/runtime allowlists, or change the public `IncidentState`, HTTP
API, `IncidentWorkflow.run()` or `OpsTools` interfaces. The Stage 5 0/2 combined result
remains an immutable historical baseline. A separate Stage 6 reliability runner now
repeats only the current deterministic combined-fault path without rewriting those frozen
labels. Batch `stage6-combined-r5-20260915` completed 5/5 valid local Compose recoveries,
including complete policy review, Redis-then-MySQL execution and fresh joint probes. Its
Wilson 95% interval is 56.6%-100%, so it is evidence for the enumerated topology rather
than a production SLA or proof for arbitrary combined faults.

## Stage 7: shared-store active-active load validation

Status: completed on 2026-09-15. See
[`docs/evaluations/stage7-active-active-r2-report.md`](evaluations/stage7-active-active-r2-report.md).

The Compose canary now runs as a second Control API process against the same PostgreSQL
incident and event-memory store. Alertmanager fingerprints deterministically bind a new
incident before workflow execution, and PostgreSQL transaction advisory locks serialize
same-alert and same-incident snapshot replacement without changing the public API.

Formal batch `stage7-active-active-r2-20260915` sent 200 unique recommendation-only writes,
64 concurrent deliveries of one new fingerprint, and 100 overlapping reads across both
nodes at concurrency 24. All 364 requests succeeded; the duplicate fingerprint converged
to one incident, all 202 opposite-node reads passed, and PostgreSQL had no state-ID
mismatch, orphan child row, or approval/execution/verification side effect. Observed
throughput was 48.811 requests/s and write p95 was 7.188 seconds. These are bounded local
Compose observations, not a production HA, capacity, or SLA claim.

## Stage 8: external verification-policy rollout controller

Status: completed on 2026-09-15. See
[`docs/evaluations/stage8-policy-rollout-r2-report.md`](evaluations/stage8-policy-rollout-r2-report.md).

The rollout controller is a separate one-shot deployment component with no HTTP server and
no production execution authority. It accepts only an already signed policy bundle plus an
explicit rollout approval, independently verifies the key ID, HMAC, content digest and strict
policy schema, rejects rollback or same-revision conflicts, and writes through atomic files.
The distributor retains its compatible `/bundle` route and adds isolated read-only canary and
stable channels.

Rollout is staged: every configured canary must report the exact accepted revision and digest
through a fresh request-bound peer credential before stable is published. The controller then
waits for an explicit quorum across the configured nodes and records an fsync-backed audit log.
Canary failure leaves stable untouched; a successful quorum may still report pending minority
nodes rather than claiming full convergence. The current formal batch required 2/2 nodes and
therefore also reached full convergence.

This is bounded coordination, not distributed consensus. The local acceptance uses two
same-host processes, a shared HMAC peer key and a single distributor/volume. Real host loss,
network partitions, controller failover, cross-zone timing and managed PostgreSQL failover
remain future failure-domain work.

## Stage 9: rollout controller interruption recovery

Status: completed on 2026-09-16. See
[`docs/evaluations/stage9-controller-resume-r1-report.md`](evaluations/stage9-controller-resume-r1-report.md).

The one-shot controller now binds each approved candidate to a content-addressed rollout
plan covering revision/digest, node endpoints, canary membership and quorum. Every durable
phase carries that plan digest. A restarted controller strictly parses the fsync-backed audit,
rejects corrupt records or a changed plan for the same candidate, reconciles the actual canary
and stable bundles, and resumes from the last safe boundary. It revalidates canary acceptance
before a not-yet-published stable update, skips canary replay when stable already contains the
candidate, and returns an already committed quorum result idempotently without polling again.

Formal batch `stage9-controller-resume-r1-20260916` terminated the controller immediately
after durable canary acceptance. At that checkpoint canary held revision `2026091601` while
stable remained at `2026091600`. The replacement process used the same plan/audit, revalidated
canary, published stable once and reached 2/2 quorum with no execution or Verification side
effect. This proves enumerated process-interruption recovery on the same-host Compose topology;
it does not cover host/volume loss, controller leader election, network partitions, external
workload identity or PostgreSQL failover.

## Stage 10: database fault-domain rehearsal

Status: completed on 2026-09-16. See
[`docs/evaluations/stage10-fault-domain-r5-report.md`](evaluations/stage10-fault-domain-r5-report.md).

The validation topology uses a dedicated Docker database network plus isolated temporary
PostgreSQL primary/standby volumes. Disconnecting one Control API from the database network
made that node fail closed within the bounded client timeout while the other node continued
recommendation-only writes. Reconnecting the network restored shared-state visibility without
recreating the process.

The standby uses physical streaming replication. After its scoped incident set caught up, the
runner stopped the old primary, promoted the standby, and moved the stable database endpoint
alias. Both existing Control API processes resumed writes and cross-node reads, all four scoped
incidents remained present, and no execution or Verification row was created.

This closes the first reproducible local network-partition and PostgreSQL endpoint-failover
rehearsal, not the real cross-host requirement. It has no automatic leader election, fencing,
old-primary rejoin, failure-window concurrent writes, managed-service behavior, cross-zone
latency, RPO/RTO or production SLA result. Stage 11 replaces policy peer and controller
shared-HMAC identity with the existing external workload identity boundary; the remaining
failure-domain acceptance belongs on real independent hosts or a managed HA PostgreSQL endpoint.

## Stage 11: verification-policy external workload identity

Status: completed on 2026-09-16. See
[`docs/evaluations/stage11-policy-identity-r2-report.md`](evaluations/stage11-policy-identity-r2-report.md).

Control API peer fan-out and the one-shot rollout controller now use separate asymmetric proof
keys to request short-lived RS256 credentials from the independent workload identity issuer.
The issuer allowlists workload subject, peer audience, read-only operation and target node before
signing. Receiving nodes hold only issuer public trust, repeat the request/operation/target/subject
checks and atomically consume each `jti`. The local shared peer HMAC configuration is removed;
the HMAC wrapper around the pre-signed policy bundle remains a separate content-integrity boundary.

Formal batch `stage11-policy-identity-r2-20260916` reached exact canary acceptance and 2/2 quorum
at revision `2026091602`. Missing identity, the retired HS256 credential, wrong target and credential
replay returned 401; issuer nonce replay returned 401 and an unknown peer target returned 403.
This is same-host Compose evidence for the external issuer seam, not cloud-native federation,
issuer HA, production key custody or cross-host failure-domain validation.

## Stage 12: external failure-domain acceptance

Status: acceptance contract implemented; real external batch pending.

The repository now has a fail-closed evidence module and CLI for the next real-host run. Its
small interface accepts one immutable topology plan and one external observation document,
then validates network partition, Control API node loss/replacement, provider-reported database
failover, stable-endpoint recovery, cross-node visibility, issuer-instance loss, credential
issuance during that loss, trust continuity, replay/target rejection and zero remediation side
effects. Plans with fewer than two distinct Control API, database or issuer failure domains are
rejected before reporting. Required raw control-node, database-provider, identity-issuer and
database-audit evidence is referenced by SHA-256, and finalized artifacts are read-only and
non-overwritable.

Infrastructure fault injection deliberately remains outside OpsPilot. Completing this stage
requires independently provisioned hosts or availability zones plus managed or independently
operated HA PostgreSQL and redundant issuer instances. No local fixture can produce a passing
formal result, and a future pass will still be bounded acceptance evidence rather than an SLA,
zero-data-loss, RPO or RTO claim. See
[`docs/evaluations/stage12-external-fault-domain-protocol.md`](evaluations/stage12-external-fault-domain-protocol.md).

## Post-Stage 12 local hardening: PostgreSQL fast failure and request isolation

Status: completed on 2026-09-17. See
[`docs/evaluations/postgres-fast-fail-r4-report.md`](evaluations/postgres-fast-fail-r4-report.md).

All Control API PostgreSQL persistence paths now use explicit configurable connection,
concurrency-acquisition, statement, lock, idle-transaction and application-call deadlines.
Synchronous store and retrieval work no longer blocks the async API event loop. A request
deadline is propagated into its worker; if lower-level connection setup finishes after the
caller has timed out, the connection is closed before any SQL can run.

Formal batch `postgres-fast-fail-r4-20260917` sent eight concurrent writes to a
database-isolated node over the unaffected shared API network. All eight failed closed with
HTTP 502 in at most 0.094 seconds, `/health` stayed responsive in 0.006 seconds, and the
surviving node remained writable. Sixteen seconds after network recovery all eight failed IDs
still returned 404. Both existing nodes then resumed writes and cross-node reads after the
same physical-standby promotion used by Stage 10, without execution or Verification side
effects.

This is a bounded same-host concurrency and recovery observation, not production capacity,
cross-host HA, an SLA, RPO/RTO or zero-data-loss proof. The real Stage 12 batch remains pending.

## Post-Stage 12 local hardening: Control API access control

Status: completed on 2026-09-17. See
[`docs/evaluations/control-api-access-r1-report.md`](evaluations/control-api-access-r1-report.md).

The Control API now has an additive RS256 access layer with `viewer`, `analyst`, `approver`,
`admin` and machine-only `alertmanager` roles. `/health` and the existing public read-only
verification-policy status remain compatible. Incident reads, analysis, fault injection,
Repair Lab and Skill mutation use explicit permissions without changing request/response models,
`IncidentState`, `IncidentWorkflow.run()` or `OpsTools`.

An execution approval is valid only when the request carries a verified approver identity.
The credential `jti` is consumed atomically before workflow execution; replay fails closed.
Approval rows retain subject, roles, timestamp, request ID and credential ID. The Dashboard signs
short-lived per-request credentials in its server process, so its private key is never shipped to
the browser. Alertmanager receives a distinct bootstrap-generated machine credential that cannot
read incidents or obtain approval/execution authority.

The application-level feature remains compatibility-disabled unless configured; the Compose stack
enables the local reference issuer. This is a local authorization seam and test fixture, not a
production IAM, SSO, zero-trust or compliance result. A production deployment still needs an
external IdP/issuer, user sessions, managed key rotation/revocation and independent security review.
Stage 12 remains pending and unchanged.
