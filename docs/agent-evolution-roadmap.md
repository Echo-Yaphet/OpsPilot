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
`INVESTIGATION_MAX_TURNS` (5), `INVESTIGATION_MAX_TOOL_CALLS` (6), and
`INVESTIGATION_TIMEOUT` (120 seconds) bound each run. A busy investigator rejects
new model work immediately; degraded SDK runs skip downstream model RCA and
verification explanations while deterministic investigation/recovery continues.
Each model response is limited to 512 output tokens. This is not a hard aggregate
input-token or monetary budget. A provider may continue generation after cancellation.

`llm_investigation` evidence includes model, run ID, observations, elapsed time,
termination status and successful-run token usage. Partial observations are written
to SQLite `investigation_runs` before and after each probe. A process crash can leave
a `running` record: this is an investigation journal, not a resumable checkpoint.
SDK investigation summaries do not control commands, targets or verified results.
Only completed, untruncated tool observations feed the existing RCA adapter.

## Stage 2: sandbox configuration repair

Build a separate laboratory profile containing an intentionally misconfigured
payment-service replica, a Redis fixture, and an isolated workspace runner.
Shell and filesystem tools belong only in this runner, not the Control API or
existing actuators. Restrict mounts, outbound network, resource limits and secrets.
Do not expose a generic shell on the existing runtime-executor routes.

Demonstrate: reproduce wrong Redis endpoint -> collect evidence -> propose patch ->
apply to disposable fault replica -> independent connection/health tests -> immutable
change package -> approval bound to patch digest, target, base version and validation
result -> controlled application to the lab target -> independent verification/rollback.
Changes after approval invalidate the package. Model-writable paths exclude probes,
approval state, policies and test expectations. Test malformed paths, symlinks,
resource exhaustion, changed base versions, stale/replayed approval and failed probes.

## Stage 3: harness and memory

Persist resumable SDK state and lifecycle separately from completed incident snapshots.
Add aggregate token accounting/admission and deterministic context compaction that
retains evidence references, counterevidence, action outcomes and open questions.
Migrate incident/journal/skill persistence to PostgreSQL with migration and rollback
tests; use pgvector only after filtering service version, conditions and expiry.
Add service OpenTelemetry instrumentation, Collector and Tempo before exposing Trace tools.

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
