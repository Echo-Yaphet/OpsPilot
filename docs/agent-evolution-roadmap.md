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
