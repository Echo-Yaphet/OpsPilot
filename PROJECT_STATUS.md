# OpsPilot project handoff

Last updated: 2026-08-30 (optional semantic retrieval milestone)

## Continue from here

1. Read this document and `README.md`.
2. Run `docker compose ps` and `make smoke` to refresh runtime status.
3. Preserve the existing HTTP interfaces and `IncidentState` model while implementing the next phase.
4. Optional semantic retrieval with deterministic fallback is complete. Continue with evidence correlation or the next production-safety milestone while keeping the Redis incident flow green.

The completed LangGraph milestone preserved the Redis-down scenario end to end, represents every Agent as a real graph node, retains inspectable per-incident graph state, and requires no breaking Dashboard interface changes.

## Product goal

OpsPilot is a multi-Agent AIOps system for this closed loop:

`monitoring signal -> evidence collection -> root-cause analysis -> repair proposal -> safety review -> approval -> execution -> verification`

The first working scenario is Redis becoming unavailable to `payment-service`.

## Project location

The authoritative project root is:

`/Users/yaphet/code/OpsPilot`

The earlier generated Documents/Codex directory was moved and no longer exists.

## Implemented

### Runtime and observability

- Docker Compose monorepo with 13 services, including Alertmanager and a separate executor gateway.
- Three FastAPI sample applications: `user-service`, `order-service`, and `payment-service`.
- MySQL 8.4 and Redis 7.4 dependencies.
- Prometheus scraping all three applications every five seconds.
- Prometheus rules for Redis down, MySQL down, and a bounded high-work proxy signal.
- Loki and Promtail collect Docker logs.
- Grafana has provisioned Prometheus and Loki data sources.
- Prometheus forwards grouped alerts to Alertmanager, which delivers firing and resolved webhooks to the Control API.

### Control backend

- FastAPI control plane on port 8080.
- Shared `IncidentState`, evidence, recommendation, risk, and Agent event models.
- Agent roles represented: Coordinator, Monitor, Log, RCA, Solution, Safety, Executor, and Verification.
- Stable workflow seam: `IncidentWorkflow.run()`, backed by a real LangGraph `StateGraph`.
- Eight graph nodes cover Coordinator, Monitor, Log, RCA, Solution, Safety, Executor, and Verification.
- Conditional graph routing covers insufficient evidence, recommendation-only mode, approval blocking, approved execution, and verification failure.
- In-memory per-incident checkpoints make completed graph state inspectable without changing the HTTP response.
- Stable tool seam: `OpsTools`, with live Prometheus/Loki adapters and authenticated executor-gateway operations.
- Read-only system health aggregation endpoint for the Dashboard.
- Whitelisted local fault injection endpoint for Redis down, MySQL down, and bounded CPU work.
- CORS restricted to the local Dashboard origin.
- Docker restart is classified as medium risk and requires explicit approval.
- SQLite persists incident snapshots plus normalized evidence, Agent events, recommendations, approvals, executions, and verification records in a Docker volume.
- Alertmanager fingerprint deduplication updates an existing incident instead of creating duplicates.
- Incident list and detail APIs restore complete `IncidentState` data after Control API restart.
- Verification Agent performs bounded recovery polling after approved execution and requires the target container, affected service health endpoint, and Prometheus dependency metrics to recover.
- Verification results are retained as incident evidence, including attempts and the last observed container, service, and metric state.
- Execution policy accepts only exact `docker compose restart <known-service>` recommendations and rejects command variants or unknown targets with explicit reasons.
- A typed `ExecutionAction` and `RestrictedExecutor` form a narrow gateway seam that exposes restart operations rather than arbitrary shell execution.
- Policy allow and deny decisions are included in incident evidence and normalized into the SQLite `policy_decisions` audit table.
- Policy approval does not replace human approval: medium-risk restart requires both `execute=true` and `approved=true` before the restricted executor is called.
- The Docker socket is no longer mounted into the Control API. A separately deployed executor gateway owns Docker access and requires an internal Bearer identity.
- The gateway accepts typed `restart_container` and `stop_container` requests only, applies operation-specific target allowlists, and never accepts shell commands.
- Gateway allow, deny, and failure outcomes are persisted independently in its `execution_audit` SQLite table and volume.
- Gateway failures and timeouts produce `execution_failed` incidents and do not enter recovery verification.
- A replaceable `KnowledgeRetriever` seam supplies deterministic SQLite-backed runbook and historical-incident retrieval without changing `IncidentState` or public HTTP responses.
- SQLite is migrated in place with a `runbooks` table and three idempotently seeded runbooks for Redis, MySQL, and inconclusive service degradation.
- RCA retrieves exact-root-cause runbooks plus compact same-service/same-root-cause incident summaries and records both as `runbook` and `incident_history` evidence.
- Solution uses an exact runbook match when present and retains the existing deterministic fallback command when no concrete runbook command exists.
- Knowledge retrieval returns stable typed matches internally while preserving dictionary-shaped evidence and the numeric runbook `score` field at the HTTP boundary.
- Runbook and historical matches include score explanations; verified, resolved historical outcomes rank first, with recency breaking equal-quality ties.
- Offline Redis and MySQL retrieval fixtures protect deterministic dependency hits before a future semantic retriever is introduced.
- An optional semantic retriever composes behind the existing `KnowledgeRetriever` seam and uses an injectable embedding provider plus an OpenAI-compatible HTTP adapter.
- Semantic matches supplement rather than displace exact deterministic results; deterministic scores and ordering remain intact.
- Missing configuration, endpoint failures, timeouts, malformed vector counts, and invalid dimensions fail open to SQLite deterministic retrieval.
- The local MVP starts and tests without an embedding model, service, or API key.

### Dashboard

- Production Dashboard container on port 3001.
- Live health view for three applications and four infrastructure modules.
- Agent analysis trigger.
- RCA summary, confidence, Prometheus/Loki evidence, and Agent timeline.
- Local chaos-lab controls for the three fault scenarios, with a confirmation step.
- Approved repair execution and verification flow.
- Responsive desktop/mobile layout.
- Persistent incident history selector restores details and Agent timelines after refresh.
- Project-specific Open Graph image at `apps/dashboard/public/og.png`.

### Fault scenarios

- `Redis down`: complete detection, alert, evidence, RCA, recommendation, approval, restart, and recovery path.
- `MySQL down`: injectable and handled by deterministic RCA rules.
- `CPU spike`: bounded 15-second Dashboard action and 30-second script action; observability is intentionally basic.

## Verified

Latest verification for the optional semantic retrieval milestone:

- All 27 backend tests passed, including fuzzy semantic ranking, deterministic-baseline preservation, embedding-service failure fallback, ambiguous-symptom fixtures, and wrong-root-cause negative fixtures.
- The rebuilt Control API production image was deployed without embedding configuration; SQLite fallback remained active and final `make smoke` passed.
- All 13 Docker Compose services were running after acceptance; Redis and MySQL were healthy.
- A real Redis outage with the fuzzy symptom `checkout cache cannot be reached` produced Redis metric `0`, Loki error evidence, RCA confidence `0.92`, and the Redis runbook without executing in recommendation-only mode.
- The same outage with `execute=true`, `approved=false` remained `awaiting_approval`; explicit approval restarted Redis through the gateway and deep Verification resolved on check three with `verified=true`.

Latest verification for the retrieval quality foundation milestone:

- All 13 Docker Compose services were running; Redis and MySQL were healthy at baseline, and baseline `make smoke` passed.
- Twenty-five backend tests passed, including typed result compatibility, score explanations, verified/resolved historical ranking, and offline Redis/MySQL retrieval fixtures.
- The rebuilt Control API production image was deployed; final `make smoke` passed and all 13 services were running with Redis and MySQL healthy.
- Live Redis recommendation-only returned confidence `0.92`, the expected runbook, numeric `score=111`, factor explanations, and verified/resolved history first without execution.
- Live missing approval remained `awaiting_approval`; an approved unknown target remained `execution_denied` without Verification.
- Live approved Redis recovery executed through the gateway and resolved on the third deep verification check with `verified=true`.
- Live gateway identity and allowlist denials returned HTTP 401 and 403.
- Post-deployment persistence contained 38 incidents, three runbooks, 31 Control API policy decisions, and six independent gateway audit records (three allowed, three denied).

Latest verification for the runbook and historical-incident retrieval milestone:

- All 13 Docker Compose services were running; Redis and MySQL were healthy before and after acceptance.
- `make smoke` passed before implementation and after the rebuilt Control API image was deployed.
- Twenty-three backend tests passed, including deterministic runbook ranking, compact historical retrieval, RCA/Solution knowledge integration, and all prior safety and gateway regressions.
- The Control API production image rebuilt and deployed successfully; the existing SQLite volume migrated in place and retained earlier incident history across container recreation.
- Live healthy-path analysis returned the service-degradation runbook plus three matching historical incidents while preserving the `IncidentState` response shape.
- Live Redis recommendation-only analysis returned RCA confidence `0.92`, the `redis-dependency-recovery-v1` runbook, historical matches, and performed no execution.
- Live missing-approval analysis remained `awaiting_approval`; an approved unknown target remained `execution_denied` with no Verification.
- Live gateway identity and target allowlist checks returned HTTP 401 and 403 respectively.
- Live approved Redis recovery executed through the gateway and reached `resolved`, `verified=true` on the third deep verification check.
- The Control API database contained persisted incidents, three runbooks, and policy decisions; the independent gateway database retained both allow and deny audit outcomes.

These results were observed against the running local system, not inferred from static code:

- All 12 Docker Compose services started successfully.
- The control API and all three sample applications returned healthy responses.
- Prometheus reported all three scrape targets as up.
- Six dependency series reported healthy during the baseline: Redis and MySQL for each sample application.
- Stopping Redis produced three firing `RedisDependencyDown` alerts.
- The payment incident contained a Redis metric value of `0` and a Loki connection-error log.
- RCA returned `Redis dependency is unavailable` with confidence `0.92`.
- Recommendation returned `docker compose restart redis`, medium risk, approval required.
- Recommendation-only mode performed no execution.
- Approved execution restarted Redis, returned `resolved`, and reported `verified=true`.
- `payment-service` returned healthy after recovery.
- Seven workflow and graph-level tests passed inside the control API container.
- The LangGraph Redis-down path passed live in recommendation-only and approved execution modes.
- The Dashboard production build passed.
- Dashboard HTML, CORS, and `/api/v1/system/status` were validated; the endpoint now covers three applications and five infrastructure modules.
- All 12 Compose services ran successfully; Redis and MySQL were healthy after acceptance recovery.
- Prometheus and Alertmanager configuration validation passed with `promtool` and `amtool`.
- A real Redis outage generated three Prometheus alerts, Alertmanager delivered them to the webhook, and payment/user/order incidents were persisted with Redis metric `0`, Loki errors, RCA confidence `0.92`, and `execution_requested=false`.
- Recovery notifications updated persisted incidents to `alert_resolved`; data remained available after the Control API container was recreated.
- Ten backend tests passed, covering LangGraph behavior, webhook safety, fingerprint deduplication, persistence, list/detail restoration, and approval audit recording.
- Control API and Dashboard production images built successfully; Dashboard ESLint passed in the declared Node container environment.
- Final `make smoke` passed after the Redis acceptance fault was recovered.
- Deep Redis recovery acceptance passed: recommendation-only mode left Redis stopped; explicit approval restarted it; service health and the Redis dependency metric recovered on the third bounded check; the incident finished `resolved` with `verified=true`.
- Seventeen backend tests passed, including exact allowlisting, non-allowlisted command and target rejection, missing approval, webhook non-execution, policy audit persistence, and Redis recommendation/execution regressions.
- The Control API production image rebuilt successfully and the existing SQLite database migrated in place with the `policy_decisions` audit table.
- Live policy acceptance passed: Redis recommendation-only mode recorded an allow decision but left Redis exited; explicit approval restarted Redis through the restricted executor and deep verification passed on the third check.
- Live deny acceptance passed: an approved request for `unknown-service` returned `execution_denied`, did not execute or verify, and persisted the exact deny reason alongside the live Redis allow record.
- Twenty backend tests passed after the gateway split, including gateway identity rejection, typed-action allowlisting, independent gateway audit persistence, and executor failure routing.
- Both the Control API and executor gateway production images built and deployed successfully.
- The running Control API container has only its `/data` volume and no Docker socket mount; only the executor gateway owns the socket.
- Live Redis gateway acceptance passed: recommendation-only and unapproved requests did not execute, the unknown target was policy-denied, and approved execution restarted Redis through the gateway and completed deep verification with `resolved` and `verified=true`.
- Final `make smoke` passed after gateway-based Redis recovery.

## Interfaces to preserve

### Control API

- `GET /health`
- `GET /api/v1/system/status`
- `POST /api/v1/incidents/analyze`
- `GET /api/v1/incidents`
- `GET /api/v1/incidents/{incident_id}`
- `POST /api/v1/alertmanager/webhook`
- `POST /api/v1/faults/{fault}`

`POST /api/v1/incidents/analyze` accepts:

```json
{
  "service": "payment-service",
  "symptom": "Redis unavailable",
  "execute": false,
  "approved": false
}
```

Keep response compatibility with `IncidentState`; the Dashboard consumes its evidence, events, root cause, confidence, recommendations, execution result, and verification fields.

### Module seams

- `IncidentWorkflow.run(request) -> IncidentState`
- `OpsTools.query_metric(...)`
- `OpsTools.query_logs(...)`
- `OpsTools.container_status(...)`
- `OpsTools.service_health(...)`
- `OpsTools.restart_container(...)`
- `OpsTools.stop_container(...)`

These seams were chosen so LangGraph and alternative tool adapters can replace implementations without changing callers.

## Commands

From `/Users/yaphet/code/OpsPilot`:

```bash
make up            # build and start the entire system
make smoke         # baseline application and control API check
make test          # run workflow tests in the control container
make fault-redis   # stop Redis and generate health traffic
make fault-mysql   # stop MySQL and generate health traffic
make fault-cpu     # bounded payment-service CPU work
make recover       # start Redis/MySQL and restart payment-service
make down          # stop the stack
```

Local entry points:

- Dashboard: `http://localhost:3001`
- Control API docs: `http://localhost:8080/docs`
- Grafana: `http://localhost:3000`
- Prometheus: `http://localhost:9090`
- Loki readiness: `http://localhost:3100/ready`

## Important implementation files

- `docker-compose.yml`: full local stack.
- `apps/control-api/opspilot/models.py`: shared state and request models.
- `apps/control-api/opspilot/workflow.py`: LangGraph Agent workflow, conditional routes, and inspectable per-incident checkpoints.
- `apps/control-api/opspilot/tools.py`: Prometheus, Loki, and Docker tool seam.
- `apps/control-api/opspilot/execution.py`: execution allowlist policy, typed action, and restricted executor boundary.
- `apps/executor-gateway/app.py`: authenticated typed Docker operations, operation-specific allowlists, and independent execution audit storage.
- `apps/control-api/opspilot/main.py`: HTTP routes, system status, CORS, and fault injection.
- `apps/control-api/opspilot/storage.py`: SQLite schema, normalized audit records, incident snapshots, and query operations.
- `apps/dashboard/app/page.tsx`: Dashboard behavior and UI.
- `apps/dashboard/app/globals.css`: Dashboard visual system.
- `apps/shared-service/app.py`: shared sample-application implementation.
- `infra/prometheus/alerts.yml`: alert rules.
- `infra/alertmanager/alertmanager.yml`: grouped webhook delivery to the Control API.
- `tests/test_workflow.py`: approval, policy allow/deny, Redis-path, graph inspection, inconclusive RCA, verification failure, and stale-log precedence tests.

## Current limitations

- LangGraph now provides the orchestration and checkpointed state; RCA and remediation policies remain deterministic and no LLM is connected yet.
- SQLite is appropriate for the single-node local MVP but is not intended for multi-replica Control API deployments.
- Typed deterministic retrieval and optional embedding-based semantic ranking are implemented; corpus embedding caches/vector indexes, evidence correlation, and learned long-term memory are not yet implemented.
- Verification now checks container state, application health, and dependency metrics, but uses fixed local thresholds rather than configurable SLO policies.
- Old error logs can appear in the ten-minute Loki window. Metrics currently take precedence for Redis/MySQL RCA, but evidence scoring needs time and incident correlation.
- CPU observation is a synthetic proxy, not container CPU from cAdvisor or an equivalent exporter.
- The executor gateway still has broad Docker socket access. It has a separate deployment, operation/target allowlists, a shared internal identity, and independent audit records, but production needs short-lived workload identity and a narrower Docker API proxy or equivalent runtime permissions.
- Alert resolution records signal recovery as `alert_resolved`; it does not claim that an approved remediation or deep service-level verification occurred.
- Authentication and multi-user authorization are not implemented.
- The Dashboard is intentionally local and has not been publicly deployed because it controls the local Docker environment.
- `work/dashboard-init-backup` contains recoverable initializer remnants and is excluded from Docker build context; it is not part of the product.

## Recommended roadmap

### Completed: real LangGraph orchestration

- Added the LangGraph dependency and a typed internal graph state around `IncidentState`.
- Implemented one real node for each existing Agent role.
- Added conditional routing for insufficient evidence, approval required, execution, and verification failure.
- Preserved recommendation-only mode, explicit approval, public HTTP models, and Dashboard events.
- Added graph-level coverage for Redis success, no-approval blocking, inconclusive RCA, inspectable state, and verification failure.

### Completed: event-driven incidents and persistence

- Added Alertmanager, Prometheus delivery, fingerprint deduplication, and a non-executing webhook.
- Added SQLite incident snapshots and normalized audit tables.
- Added incident list/detail APIs and Dashboard refresh-safe history/timeline restoration.

### Completed: execution policy and restricted executor boundary

- Added exact command and target allowlisting with explicit deny reasons.
- Added typed execution actions and an executor interface that cannot accept arbitrary shell commands.
- Persisted allow and deny decisions as normalized audit records without changing `IncidentState` or HTTP response compatibility.
- Preserved the explicit human approval gate and verified recommendation-only, approved recovery, and policy denial against the live stack.

### Completed: separate executor gateway

- Removed the Docker SDK and Docker socket mount from the Control API image and container.
- Added a separately deployed gateway that receives only typed operations and owns the Docker socket.
- Added internal Bearer authentication plus operation-specific restart and stop allowlists.
- Added a persistent gateway audit database for allow, deny, and failure outcomes.
- Added explicit execution failure/timeout routing that skips Verification.
- Revalidated recommendation-only, missing-approval, policy-denial, approved Redis recovery, deep verification, and smoke paths.

### Then: knowledge and further production safety

- Completed deterministic SQLite-backed runbook and historical-incident retrieval with RCA/Solution evidence integration.
- Completed stable typed retrieval results, explainable scoring, verified/resolved historical ranking, and baseline offline evaluation fixtures.
- Add incident-time evidence correlation and a larger labeled retrieval evaluation set; consider a persisted vector index only when corpus size requires it.
- Replace the local shared gateway token with short-lived workload identity and narrow the gateway's Docker/API permissions.
- Generalize Verification Agent thresholds and time windows into per-service SLO policies.
- Add cAdvisor or equivalent container metrics for the CPU scenario.

## Handoff prompt

Use this in a new conversation:

> Continue OpsPilot from `/Users/yaphet/code/OpsPilot`. Before changing anything, read `AGENTS.md`, `PROJECT_STATUS.md`, and `README.md`, then run `docker compose ps` and `make smoke` to refresh the actual baseline. The current stack has 13 services. Alertmanager incidents, SQLite persistence, bounded service-level recovery verification, exact execution allowlisting, auditable policy decisions, and a separately deployed authenticated executor gateway are complete. The Control API no longer mounts the Docker socket; the gateway accepts only typed allowlisted operations and persists its own execution audit. Preserve all HTTP interfaces, `IncidentState`, `IncidentWorkflow.run(request) -> IncidentState`, `OpsTools`, Dashboard data compatibility, Alertmanager's non-execution rule, and the independent requirements that policy allows the action and the caller supplies both `execute=true` and `approved=true`. Implement the next roadmap milestone: runbook and historical-incident retrieval, preferably with a deterministic retrieval seam and SQLite-backed records before adding semantic RAG. Keep recommendation-only, missing-approval, policy-denial, gateway failure, approved Redis recovery, deep verification, and both audit stores green. Run `make test`, rebuild affected production images, run `make smoke`, perform relevant live acceptance, and update `PROJECT_STATUS.md` before declaring completion.
