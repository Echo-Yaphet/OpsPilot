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

Freeze reproducible cases from failure trajectories. A coding agent proposes a
versioned Skill in an isolated branch/workspace. Preserve the trigger case, diff,
evaluation results, parent version and rollback pointer. Initially evolve diagnostic
instructions and repair recipes only; never let it edit gates, probes or evaluation labels.
Run original regressions plus new counterexamples before explicit promotion.

## Stage 5: held-out evaluation

Split cases by time and topology; do not expose held-out answers to skill generation.
Fix model, tool access, budgets and baseline. Include combined faults after single-fault
repair works. Record numerator/denominator, repetitions and latency for all rates.
Recovery success means independent probe success within budget. Cost per success
includes failed attempts. Regression rate means previously passing cases that fail
with the new version. The proposed +10 percentage points recovery, -20% cost and <=3%
regression values are targets, not results. A 100% bypass interception claim applies
only to the enumerated adversarial test set, never to all possible actions.
