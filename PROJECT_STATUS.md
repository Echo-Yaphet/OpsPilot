# OpsPilot project handoff

Last updated: 2026-09-01 (three-service runtime log forwarding milestone)

## Continue from here

1. Read this document and `README.md`.
2. Run `docker compose ps` and `make smoke` to refresh runtime status.
3. Preserve the existing HTTP interfaces and `IncidentState` model while implementing the next phase.
4. All three business services now forward Docker logs at runtime to Promtail over RFC5424 syslog. Promtail no longer mounts the host Docker log directory, Proxy discovery volume, target volume, or positions volume. Continue with protected runtime transport or mTLS before treating TCP 1514 as production-ready.

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

- Docker Compose monorepo with 15 services, including Alertmanager, a separate executor gateway, an internal restricted Docker proxy, and a socketless container metrics exporter.
- Three FastAPI sample applications: `user-service`, `order-service`, and `payment-service`.
- MySQL 8.4 and Redis 7.4 dependencies.
- Prometheus scraping all three applications every five seconds.
- Prometheus rules for Redis down, MySQL down, sustained per-service container CPU usage, exporter loss, target collection failures, and stale CPU samples.
- A socketless exporter reads only trimmed CPU counters through an authenticated, allowlisted proxy stats route and exposes per-service CPU usage, collection health, last-success time, and strictly validated per-service alert thresholds.
- Loki and Promtail collect `user-service`, `order-service`, and `payment-service` through Docker runtime RFC5424 syslog forwarding. The runtime pipeline retains the existing `compose_service` and `container` Loki labels without giving Promtail Docker socket or host-log access.
- Prometheus derives all three per-service freshness signals from the label-preserving Promtail runtime counter. `ServiceLogCollectionStale` retains affected-service isolation, while the stack-level Loki-ingestion alert remains the downstream fallback; file-target publication and positions are no longer part of the deployed path.
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
- Verification Agent performs bounded recovery polling after approved execution and uses validated default plus per-service SLO policies for maximum attempts, interval, service health condition, dependency metric threshold, and consecutive stable checks.
- A centrally mounted, strict JSON policy document can hot-reload default and per-service SLO overrides without recreating Control API. Reload is content-addressed, retains the last-known-good snapshot after read/parse/validation failures, and exposes read-only status.
- The same file seam optionally accepts HMAC-SHA256 signed policy bundles with an explicit signing key ID, canonical content digest, positive monotonic revision, and signature. Strict signed mode is opt-in so the default single-node unsigned file remains compatible.
- Signed loading rejects unknown keys, digest/content tampering, invalid signatures, strict-schema failures, revision rollback, and same-revision digest conflicts without replacing the in-process last-known-good policy.
- Accepted signed revisions persist in SQLite, so rollback protection survives Control API recreation. Revision history stores only revision, digest, signature status, load result, and observation time; key IDs and key material are not persisted there.
- An optional read-only policy distributor serves bundles only to authenticated Bearer clients; it exposes no policy mutation route and has no host port in the rollout profile.
- Each Control API node independently fetches, verifies, validates, and applies a distributed signed bundle. Only accepted bytes replace that node's durable cache, so invalid updates and distributor partitions preserve its last-known-good bundle across process restarts.
- The local file path remains the default and does not depend on the distributor. Remote distribution is opt-in, requires strict signature mode plus an authentication token, and does not block default single-node startup.
- Policy status distinguishes observed from accepted revision/digest and reports accepted, rejected, or source-error load results plus distributor/cache health.
- The existing local `GET /api/v1/verification-policy/status` remains unauthenticated and read-only for compatibility. Peer fan-out uses a separate authenticated read-only endpoint with fresh HMAC credentials bound to source, target node, method, path, operation, expiry, and one-time `jti`.
- Peer credential IDs are atomically consumed in each node's SQLite store, so replay is rejected across Control API restarts. Missing, malformed, expired, wrong-target, wrong-path, and replayed credentials fail before policy status is returned.
- `GET /api/v1/verification-policy/rollout` uses bounded parallel peer queries with per-request timeout, retains successful node results during partial failure, exposes desired revision/digest, and distinguishes `converged`, `degraded`, `stalled`, and default-local `inactive` states without mutating policy.
- Each recovery resolves exactly one immutable SLO snapshot before polling, so a concurrent policy update cannot change an incident's verification semantics midway through its attempt budget.
- The unconfigured policy preserves the original six attempts, two-second interval, healthy service, dependency metric value `1`, and one successful check behavior; partial per-service overrides fall back to those defaults.
- Verification results retain the original evidence fields and add the effective policy, current stable count, and required stable count without changing `IncidentState` or HTTP schemas.
- Execution policy accepts only exact `docker compose restart <known-service>` recommendations and rejects command variants or unknown targets with explicit reasons.
- A typed `ExecutionAction` and `RestrictedExecutor` form a narrow gateway seam that exposes restart operations rather than arbitrary shell execution.
- Policy allow and deny decisions are included in incident evidence and normalized into the SQLite `policy_decisions` audit table.
- Policy approval does not replace human approval: medium-risk restart requires both `execute=true` and `approved=true` before the restricted executor is called.
- Across the default stack, the Docker socket is mounted only into the internal restricted proxy. Control API, executor gateway, the metrics exporter, and Promtail have no socket access. All three business services use their Docker logging drivers to forward directly to Promtail; the default Proxy deployment has no log-discovery configuration or shared target mount, and Promtail has no host-log, target, or positions mount.
- The gateway accepts typed `restart_container` and `stop_container` requests only, applies operation-specific target allowlists, and never accepts shell commands.
- The gateway image no longer contains the Docker SDK; it calls fixed status, restart, and stop proxy routes over a dedicated internal network.
- The proxy has no host port, is not attached to the Control API network, independently enforces operation-specific target allowlists, and exposes no raw Docker API routes. Its read-only stats route is limited to the three business services and returns only CPU counters needed by the exporter.
- Gateway allow, deny, and failure outcomes are persisted independently in its `execution_audit` SQLite table and volume.
- Gateway failures and timeouts produce `execution_failed` incidents and do not enter recovery verification.
- Control API no longer transmits a reusable static Gateway token. It mints a new HMAC-signed workload credential for every Gateway request.
- Workload credentials have a bounded lifetime and carry issuer, audience, subject, issued/expiry times, unique `jti`, HTTP method/path, operation, and target claims.
- Gateway rejects expired, wrong-audience, request/action-mismatched, and replayed credentials before Docker access.
- Consumed credential IDs are persisted atomically in the independent Gateway SQLite store; action audits include workload subject and credential ID.
- Every newly minted workload credential carries an explicit configurable key ID; the Gateway selects exactly that verification key and rejects unknown IDs before Docker access.
- The Gateway supports one current and one previous verification key only, with an absolute finite overlap deadline and a bounded overlap limit (one hour by default, at most one day). Incomplete, duplicate, malformed, expired, or overlong rotation configuration is rejected at startup.
- A Gateway-first then Control-API rotation preserves in-flight availability: the old signer remains valid only inside the configured overlap, while the new signer switches without weakening credential expiry, request/action/target binding, or persistent `jti` replay prevention.
- Gateway execution audits now retain the workload key ID alongside subject and credential ID. Existing `EXECUTOR_IDENTITY_KEY` configuration remains compatible through the default `control-api-v1` key ID.
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
- Alertmanager firing analysis carries its original `startsAt` into an internal evidence context without changing `AnalyzeRequest`'s public schema; manual analysis uses its request start time.
- Prometheus dependency queries are evaluated at the incident time, while Loki queries are bounded to two minutes before through five minutes after the incident (and never beyond the current time).
- A new `incident_context` evidence record exposes the origin, incident timestamp, source query windows/modes, and result counts while preserving existing Prometheus/Loki list-shaped evidence data.
- Alternative `OpsTools` implementations remain compatible: the workflow uses the original current/recent query methods when incident-time extensions are unavailable.
- Offline retrieval evaluation now covers ten labeled positive, fuzzy, contradictory, wrong-service/root-cause, service-degradation, and unrelated cases with top-1 accuracy, false-positive rate, deterministic-regression count, and embedding fallback success-rate checks.
- Semantic evaluation also covers low-similarity rejection, embedding failure, vector-count errors, dimension mismatches, and non-finite vector values.

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
- `CPU spike`: bounded 15-second Dashboard action and 30-second script action with real container CPU metrics, Prometheus firing/resolution, deterministic RCA, and Alertmanager recommendation-only handling.

## Verified

Latest verification for the three-service runtime log forwarding milestone:

- The complete current-source suite passed all 94 backend tests with the existing single LangGraph deprecation warning. Compose validation, real Promtail 3.5.3 syntax validation, and `promtool` passed with nine rules; the rebuilt 15-service default stack and enhanced smoke passed.
- Docker inspection confirmed user/order/payment all use the `syslog` driver at `tcp://host.docker.internal:1514`. Promtail has only its read-only configuration mount, the Proxy has no discovery volume, and Compose contains no host Docker-log, target, or positions path. Promtail reported zero syslog parsing errors.
- Loki retained `compose_service` and the original `/opspilot-<service>-1` `container` label for all three services. All three runtime counters increased and all three `opspilot_service_log_read_fresh` series were `1`.
- Stopping only `user-service` made only its runtime freshness become `0`, kept order/payment at `1`, fired `ServiceLogCollectionStale`, and created recommendation-only incident `90cc70f6-700e-44f0-8be2-dd83bd498a93` with no execution or Verification. Starting it restored freshness and updated the same incident to `alert_resolved`.
- A bounded payment CPU fault reached about `1.002` cores, fired `ContainerHighCPU`, and persisted only the confidence-`0.9` recommendation incident `3b67ae24-651c-4f22-8e97-120c2a5ac330`. A real Redis outage produced `dependency_up=0`; recommendation-only did not execute, missing approval left Redis exited with `awaiting_approval`, and explicit approval restarted it through Gateway/Proxy to `resolved`, `verified=true` on verification attempt three under the default unsigned policy.
- Gateway returned 401 without identity, 200 once then 401 on replay, and 403 for an authenticated unknown target. Proxy returned 401 without identity, 404 for the raw Docker route, and 403 for an unknown target; Control API could not resolve the isolated Proxy. Only Proxy mounted the Docker socket. Final freshness was `1` for all services with no firing alerts.

Latest verification for the payment-service runtime log forwarding milestone:

- The rebuilt current-source test image passed all 94 backend tests; the only warning remains the existing LangGraph dependency deprecation notice. New coverage verifies that Proxy file discovery can be narrowed to the non-migrated services without weakening its strict supported-service allowlist.
- Compose, real Promtail 3.5.3 syntax, and `promtool` validation passed with 11 healthy rules. The 15-service stack deployed with exactly two file targets plus the RFC5424 receiver; enhanced smoke proved Proxy target count `2`, payment runtime counter growth, Loki availability, and freshness `1` for all three services.
- Docker inspection confirmed `payment-service` uses the `syslog` logging driver at `tcp://host.docker.internal:1514`. Loki retained `compose_service="payment-service"` and `container="/opspilot-payment-service-1"`; Promtail reported no parsing errors after the final short RFC5424 tag was deployed.
- Stopping only `payment-service` made its runtime-derived freshness become `0` while user/order remained `1`, fired `ServiceLogCollectionStale`, and created recommendation-only incident `5417b733-64c4-4086-b764-a6dd7d21ecbc` with no execution or Verification. Starting it restored freshness `1`, cleared the alert, and updated the same incident to `alert_resolved`.
- The named positions volume retained and advanced user/order offsets around 7.50 MB across forced Promtail recreation. Promtail sought to the persisted offsets rather than zero, payment runtime forwarding reconnected, and the post-recreation syslog parsing-error counter remained `0`.
- A bounded payment CPU fault reached about `1.002` cores, fired and resolved `ContainerHighCPU`, and persisted only a confidence-`0.9` recommendation incident. A real Redis outage produced `dependency_up=0`, recommendation-only non-execution, and `awaiting_approval` while Redis remained exited; explicit approval restarted Redis through Gateway/Proxy and reached `resolved`, `verified=true` on verification attempt four under the default unsigned policy.
- Gateway returned 401 without identity, 200 once then 401 on replay, and 403 for an authenticated unknown target. Proxy returned 401 without identity, 404 for the raw Docker route, and 403 for an unknown target; Control API could not resolve the isolated Proxy. Only Proxy mounted the Docker socket. Final Compose/runtime checks and smoke passed with no firing alerts.

Latest verification for the per-service log freshness milestone:

- The rebuilt current-source test image passed all 93 backend tests; the only warning remains the existing LangGraph dependency deprecation notice. New coverage verifies successful path/service publication metrics and last-known-good mapping retention after discovery failure.
- Compose, `promtool`, and shell validation passed with 11 healthy Prometheus rules: ten alerts plus the `opspilot_service_log_read_fresh` recording rule. The rebuilt Proxy and reloaded Prometheus deployed in the 15-service stack; enhanced smoke proved all three business services fresh alongside the existing target, Loki, CPU, and recommendation-only checks.
- Stopping only `payment-service` made its freshness signal become `0` and fired `ServiceLogCollectionStale` with `service=payment-service`; `user-service` and `order-service` remained `1`. Alertmanager created a recommendation-only incident with no execution or Verification. Restarting payment-service restored its signal to `1`, cleared the alert, and updated the incident to `alert_resolved`.
- A bounded payment-service CPU fault reached about `1.003` cores, fired `ContainerHighCPU`, produced RCA confidence `0.9` without execution or Verification, and then reached `alert_resolved`.
- A real Redis outage produced `dependency_up=0`, recommendation-only non-execution, and `awaiting_approval` without approval. Explicit approval restarted Redis through Gateway/Proxy and reached `resolved`, `verified=true` on verification attempt two under the accepted default unsigned policy.
- Live Gateway checks returned 401 without identity, 200 once then 401 on replay, and 403 for an authenticated unknown target. Proxy returned 401 without identity and 404 for the raw Docker route; Control API could not resolve the Proxy network name. Docker Desktop exposed the sole Proxy socket bind from `/run/host-services/docker.proxy.sock` to container path `/var/run/docker.sock`.

Latest verification for the persistent Promtail positions and log freshness milestone:

- The rebuilt current-source test image passed all 92 backend tests; the only warning remains the existing LangGraph dependency deprecation notice. New coverage verifies successful and failed publication metrics, last-known-good target retention, and restoration of the persisted target timestamp after a Proxy restart.
- Compose, Prometheus `promtool`, and the real Promtail 3.5.3 configuration validated with nine alert rules. The rebuilt Proxy plus recreated Promtail deployed in the 15-service default stack; enhanced smoke proved three active targets, successful fresh publication, recent Loki logs, fresh CPU metrics, and the existing recommendation-only flow.
- The `promtail-positions` named volume retained all three Docker log paths and increasing offsets across forced Promtail recreation and stop/start recovery, preventing repeated replay from offset zero after subsequent recreations.
- A live invalid-project Proxy deployment retained the last-known-good target file while `LogTargetPublicationFailed` and `LogTargetsStale` both reached firing. Restoring the default Proxy cleared them and restored CPU collection. Stopping Promtail made `LokiLogIngestionStale` fire; restarting it with new health traffic cleared the alert without losing positions.
- A bounded payment-service CPU fault reached about `1.001` cores, fired `ContainerHighCPU`, created only a recommendation incident with root cause `Container CPU usage is high`, confidence `0.9`, no execution/verification claim, and then reached `alert_resolved`.
- A real Redis outage produced `dependency_up=0`, confidence `0.92`, recommendation-only non-execution, and `awaiting_approval` without approval. Explicit approval restarted Redis through Gateway/Proxy and reached `resolved`, `verified=true` on verification attempt two.
- Live Gateway checks returned 401 without identity, 200 once then 401 on replay, and 403 for an authenticated unknown target. Proxy returned 401 without identity and 404 for the raw Docker route; Control API could not resolve the Proxy network name, and only the Proxy mounted the Docker socket.

Latest verification for the socketless Promtail log discovery milestone:

- The rebuilt current-source test image passed all 89 backend tests; the only warning remains the existing LangGraph dependency deprecation notice. New coverage verifies Compose-project-bound, three-service log target discovery plus deterministic atomic publication to the Promtail target file.
- Compose and the real Promtail 3.5.3 configuration validated. The rebuilt Proxy and recreated Promtail deployed in the 15-service default stack; Proxy published exactly `user-service`, `order-service`, and `payment-service`, and Promtail activated all three targets with the existing `compose_service` and `container` labels.
- Promtail no longer mounts `/var/run/docker.sock`; it has only the config, read-only target volume, and read-only host container-log directory. Runtime inspection confirmed that the Proxy is the sole socket owner, its target volume is the only shared discovery seam, and Control API still cannot resolve the isolated Proxy network name.
- Loki returned live payment-service logs with the compatible labels. During a real Redis outage it returned the error line inside the incident window; `incident_context` reported one Loki line without changing the list-shaped Loki evidence or Dashboard-facing state.
- A bounded payment-service CPU fault reached about `0.998` cores, fired `ContainerHighCPU`, created only a recommendation incident with root cause `Container CPU usage is high`, confidence `0.9`, no execution/verification claim, and then reached `alert_resolved`.
- A real Redis outage produced `dependency_up=0`, RCA confidence `0.92`, recommendation-only non-execution, and `awaiting_approval` when approval was missing. Explicit approval restarted Redis through Gateway/Proxy and reached `resolved`, `verified=true` on verification attempt four.
- Live Gateway checks returned 401 without identity, 200 once then 401 on replay, and 403 for an authenticated unknown target. Proxy checks returned 401 without identity, 200 for allowlisted stats, 403 for denied stats, and 404 for the raw Docker route and removed log-discovery API.

Latest verification for the container metrics health and per-service CPU policy milestone:

- The rebuilt current-source test image passed all 87 backend tests; the only warning remains the existing LangGraph dependency deprecation notice. New coverage verifies distinct per-service thresholds, exact target coverage, invalid-value rejection, retained last-success timestamps across target failures, and the prior per-target fail-open behavior.
- Compose validation and Prometheus `promtool` configuration/rule checks passed with six rules. The rebuilt exporter and reloaded Prometheus exposed active `0.8` thresholds for all three services; smoke additionally proved payment-service collection healthy, threshold active, and last-success age below 30 seconds.
- A real restricted-Proxy outage produced `container_cpu_metrics_up=0` plus firing `ContainerMetricsCollectionFailed` and `ContainerMetricsDataStale` alerts for all three services. Restarting the Proxy restored metrics to `1` and cleared both alert types.
- A bounded payment-service CPU fault reached about `1.0009` cores and fired `ContainerHighCPU` through the label-matched `0.8` threshold. Alertmanager created only a recommendation incident with root cause `Container CPU usage is high`, confidence `0.9`, no execution or verification claim, and then updated it to `alert_resolved`.
- A real Redis outage still produced `dependency_up=0`, confidence `0.92`, recommendation-only non-execution, and `awaiting_approval` without approval. Explicit approval restarted Redis through Gateway/Proxy and reached `resolved`, `verified=true` on verification attempt three.
- Live Gateway checks returned 401 without identity, 403 for an authenticated unknown target, 200 once then 401 on replay. Proxy stats returned 401 without identity, 200 for an allowlisted business service, 403 for a denied stats target, and 404 for a raw route. Only the Proxy mounted the Docker socket, and Control API still could not resolve its isolated network name.

Latest verification for the real container CPU observability and incident-loop milestone:

- All 86 backend tests passed in the rebuilt Control API image. New coverage verifies proxy stats identity/target allowlisting, trimmed CPU counters, Docker delta-to-core conversion, per-target exporter failure isolation, CPU evidence/RCA, and a safe service recommendation. The only warning remains the existing LangGraph dependency deprecation notice.
- Compose validation passed and the affected Control API, Docker Proxy, container metrics exporter, and Prometheus configuration deployed. The default stack now has 15 running services; Redis/MySQL are healthy, the default unsigned local Verification policy is accepted with no load error, and final smoke includes `container_cpu_metrics_up=1` for payment-service.
- Prometheus reported the new exporter target up. Live payment-service CPU usage rose from about `0.0013` cores at idle to about `1.00` core during the bounded 30-second fault, remained above `0.8` for the required 10 seconds, and fired `ContainerHighCPU`.
- Alertmanager created a persisted payment-service incident with root cause `Container CPU usage is high`, confidence `0.9`, `execution_requested=false`, no execution result, no verification claim, and a medium-risk approval-required restart recommendation. After the bounded work ended, it updated the same incident to `alert_resolved` without executing remediation.
- A real Redis outage still produced `dependency_up=0`, confidence `0.92`, recommendation-only non-execution, and `awaiting_approval` when approval was missing. Explicit approval restarted Redis through Gateway/Proxy and reached `resolved`, `verified=true`.
- Live Gateway checks returned 401 without identity, 403 for an authenticated unknown target, and 401 on replay. Proxy stats returned 401 without identity, 200 for an allowlisted business service, 403 for a non-stats target, and 404 for a raw Docker route. Control API could not resolve the Proxy network; Control API, Gateway, and the exporter had no Docker socket mount, while the Proxy remained the sole socket owner among them.

Latest verification for the authenticated peer rollout control-plane hardening milestone:

- The rebuilt Control API and canary images passed all 83 backend tests. New coverage verifies short-lived request/path/operation/target binding, missing identity, overlong lifetime rejection, durable one-time `jti` consumption across node restart, bounded fan-out concurrency, deterministic node ordering, partial-result retention, and `converged`/`degraded`/`stalled` semantics. The only warning remains the existing LangGraph dependency deprecation notice.
- Compose validation passed. The default 14-service stack was healthy before and after acceptance, Redis/MySQL were healthy, and final `make smoke` passed. Default unsigned local-file mode was restored with no load error; the local public status endpoint remained 200 while the internal peer endpoint returned 401 without identity.
- A profile-scoped primary and canary independently accepted signed revision 104 with digest `sha256:c9795ef4e22fda109477ff79aa8ff256afca90a47abdc81a7d6af0aec00f9999`. Authenticated fan-out reported `rollout_state=converged`, `healthy=true`, and two of two online nodes. The persistent primary database's highest accepted signed revision is now 104; future strict signed acceptance must use a revision greater than 104.
- Peer status returned 401 without identity, 200 for a valid request-bound credential, 401 on replay, and 401 for a wrong target. The existing local status route remained 200 without authentication on both primary and canary.
- An online canary using an unknown policy signing key observed revision 104 but rejected it; the primary reported `stalled` rather than convergence. Stopping the canary retained the primary result and reported `degraded`, one of two online. Stopping the distributor kept both nodes converged on accepted-only revision 104 caches while reporting `degraded`; distributor and canary recovery returned the rollout to healthy convergence.
- A real Redis outage produced dependency metric `0`, RCA confidence `0.92`, recommendation-only non-execution, and `awaiting_approval` with Redis still stopped. Approved recovery restarted Redis through the Gateway and Proxy and reached `resolved`, `verified=true` after two stable checks on verification attempt five under the immutable revision 104 policy.
- Live Gateway checks returned 401 without identity, 200 once, 401 on replay, and 403 for an authenticated unknown target. Proxy checks returned 401/200/403/404/404 for missing identity, fixed status, unknown target, raw Docker, and delete-style routes. Only the Proxy owned the Docker socket, and Control API could not resolve the Proxy network.

Latest verification for the authenticated multi-node Verification policy rollout milestone:

- All 75 backend tests passed in the rebuilt Control API image. New coverage verifies authenticated distribution, complete signed-mode configuration, accepted-only caching, tamper rejection without cache replacement, restart recovery during a partition, convergence, and offline-node reporting. The only warning remains the LangGraph dependency deprecation notice.
- Compose validation passed. A profile-scoped distributor and an independent canary Control API with its own SQLite volume deployed alongside the primary; both independently accepted signed revision 103, digest `sha256:c9795ef4e22fda109477ff79aa8ff256afca90a47abdc81a7d6af0aec00f9999`, and reported `converged=true` for two online nodes.
- The persistent primary database's highest accepted signed revision is now 103 with that digest. Any future strict signed acceptance against the current volume must use a revision greater than 103; do not clear revision history to bypass rollback protection.
- Distributor requests without identity and with the wrong Bearer token returned 401; the valid identity returned 200. Content tampering, an unknown key at revision 104, signed revision 100 rollback, and a correctly signed revision 104 with an invalid schema were rejected by both nodes while accepted revision 103 remained their last-known-good.
- Stopping the canary produced one of two online nodes and `converged=false`. Stopping the distributor left both nodes on revision 103; the canary then restarted while the distributor remained offline and recovered revision 103 from its accepted-only durable cache.
- The default unsigned local-file configuration was restored after acceptance. The profile-scoped canary and distributor were removed, the rebuilt primary was deployed, `/health` passed, and policy status returned unsigned local-file mode with no load error.
- A real Redis outage retained `dependency_up=0`, RCA confidence `0.92`, recommendation-only non-execution, and missing-approval blocking. Approved recovery restarted Redis through the Gateway and Proxy and reached `resolved`, `verified=true` on check four under the distributed eight-attempt/two-stable-check policy.
- Live Gateway checks returned 401 without identity, 200 once and 401 on replay, and 403 for an authenticated unknown target. Proxy checks returned 401/200/403/404/404 for missing identity, fixed status, unknown target, raw Docker, and delete-style routes. Only the Proxy mounted the Docker socket, and Control API could not resolve it.
- With the Proxy unavailable, an approved Redis request entered `execution_failed`, retained `verified=null`, and produced no Verification evidence. Proxy, Redis, payment-service, and the default unsigned policy were healthy afterward.

Latest verification for the signed Verification SLO policy bundle milestone:

- All 69 backend tests passed in the rebuilt Control API image; new coverage verifies valid signed loading, strict-mode compatibility, digest tampering, unknown key IDs, invalid signatures, signed schema failure, persistent revision rollback protection, environment fallback, last-known-good retention, and minimal revision-history fields. The only warning remains the LangGraph dependency deprecation notice.
- Compose validation passed and the rebuilt Control API image deployed. The default mounted unsigned JSON remained healthy in compatibility mode; strict mode was then enabled with a one-key trusted keyring without adding a policy mutation API.
- A tampered bundle at strict-mode startup was rejected and fell back to the environment defaults. Valid revision 101 then hot-loaded without recreation. Subsequent content tampering, an unknown key, signed revision 100 rollback, and a correctly signed revision 102 with an invalid schema were all rejected while revision 101 remained the last-known-good snapshot.
- Live SQLite inspection showed accepted and rejected loads with only revision, content digest, signature status, load result, and time. The trusted key and key ID were not written to revision history.
- A real Redis outage retained `dependency_up=0`, RCA confidence `0.92`, recommendation-only non-execution, and missing-approval blocking while Redis remained stopped. Approved recovery restarted Redis through the Gateway and Proxy and reached `resolved`, `verified=true` on check three under the signed eight-attempt policy only after two consecutive stable checks.
- Live Gateway identity returned 200 once and 401 on replay; missing identity returned 401 and an authenticated unknown target returned 403. Proxy missing identity returned 401, its fixed status route returned 200, unknown target returned 403, and raw/delete-style routes returned 404. Control API could not resolve the Proxy and only the Proxy mounted the Docker socket.
- With the Proxy deliberately unavailable, an approved request entered `execution_failed`, retained `verified=null`, and did not enter recovery polling. The Proxy and default unsigned compatibility policy were restored afterward; Redis, MySQL, payment-service, and the Control API were healthy.

Latest verification for the Gateway workload identity key rotation milestone:

- All 61 backend tests passed in rebuilt Control API and Gateway images. New coverage verifies current/previous key acceptance, explicit key IDs, unknown-key rejection, overlap expiry and maximum duration, invalid rotation configuration, and key-ID auditing; the only warning remains the LangGraph dependency deprecation notice.
- Compose validation passed and rebuilt Control API and Gateway production images deployed. The unchanged default `EXECUTOR_IDENTITY_KEY` configuration remained operational with the default `control-api-v1` ID.
- A live two-stage rotation first deployed the Gateway with new `control-api-v2` current key plus the old `control-api-v1` key under a finite deadline. The still-old Control API successfully queried Redis through the Gateway; after the Control API switched, the new key also succeeded.
- During the live overlap, old and new key requests returned 200. Unknown key ID, expired credential, wrong audience, claim mismatch, replay, and missing identity returned 401; an authenticated unknown target returned 403. A deliberately incomplete previous-key configuration was rejected when a temporary Gateway container imported its application.
- Live Proxy checks preserved the boundary: missing identity returned 401, unknown fixed-route target returned 403, and raw Docker plus delete-style routes returned 404.
- A real Redis outage retained `dependency_up=0`, RCA confidence `0.92`, recommendation-only non-execution, and missing-approval blocking while Redis remained stopped. Approved recovery restarted Redis through the Gateway and Proxy and reached `resolved`, `verified=true` on verification check four. Redis and `payment-service` were healthy afterward.

Latest verification for the safe hot-reload SLO policy milestone:

- All 51 backend tests passed in the rebuilt Control API image; new coverage verifies valid content reload, strict unknown-field rejection, invalid-update last-known-good fallback, environment fallback, and exactly one policy resolution per recovery. The only warning remains the LangGraph dependency deprecation notice.
- Compose validation passed and the rebuilt Control API production image deployed. Updating the mounted policy changed its content revision while the Control API container ID remained unchanged.
- A live strict policy of two immediate attempts requiring two stable checks correctly ended `verification_failed` after approved Gateway/Proxy Redis restart because Prometheus had not stabilized inside the budget.
- A subsequent valid update supplied eight attempts at two-second intervals and two stable checks. An invalid replacement requesting one attempt but two stable checks retained that last valid revision, reported the validation error, and the next approved Redis recovery reached `resolved`, `verified=true` on total check four with `stable_checks=2` under the retained eight-attempt policy.
- Live Redis acceptance retained `dependency_up=0`, RCA confidence `0.92`, recommendation-only non-execution, missing-approval blocking while Redis remained exited, typed approved restart, and deep recovery verification. Default policy content was restored afterward and reload status returned error-free.
- Runtime security boundaries remained intact: only the Proxy mounts the Docker socket; Gateway missing identity returned 401; one request-bound credential returned 200 then 401 on replay; Proxy missing identity returned 401; its raw Docker route returned 404; Control API still could not resolve the Proxy name.

Latest verification for the configurable per-service SLO milestone:

- All 48 backend tests passed in the rebuilt Control API image; coverage includes configuration parsing and invalid-value rejection, per-service overrides, default fallback, health-condition and metric-threshold strategies, consecutive stability, timeout failure, and all prior workflow, persistence, retrieval, identity, Gateway, and Proxy regressions. The only warning remains the LangGraph dependency deprecation notice.
- Compose validation passed and the rebuilt Control API production image deployed successfully. The default runtime configuration was restored after acceptance.
- A real Redis outage preserved recommendation-only behavior and RCA confidence `0.92`; `execute=true, approved=false` remained `awaiting_approval` while Redis stayed stopped.
- With a live strict policy of two immediate attempts requiring two stable checks, approved execution restarted Redis through the Gateway and restricted Proxy but correctly ended `verification_failed` because the dependency metric had not stabilized within the budget.
- With a live policy of eight attempts at two-second intervals requiring two stable checks, approved execution restarted Redis and reached `resolved`, `verified=true` on total check four only after `stable_checks=2`; the payment service and dependency metric both recovered.
- Runtime security denials remained intact: Gateway missing identity returned 401, a credential returned 200 then 401 on replay, an authenticated unknown target returned 403, Proxy missing identity returned 401, a raw Docker route returned 404, and Control API still could not resolve the Proxy network name.

Latest verification for the restricted Docker runtime proxy milestone:

- All 39 backend tests passed in the rebuilt Control API image, including restricted-proxy identity, fixed-route and target denials, proxy-failure auditing, plus every prior workflow, persistence, retrieval, gateway identity, and replay regression; the only warning remains the LangGraph dependency deprecation notice.
- Compose validation passed and rebuilt Control API, executor gateway, and Docker proxy production images deployed successfully; all 14 services were running and final `make smoke` passed.
- Runtime inspection confirmed that only `docker-proxy` mounts `/var/run/docker.sock`; Control API and Gateway have only their data volumes. The proxy has no host port, and Control API cannot resolve its name because it is absent from the dedicated internal network.
- The Gateway image has no Docker SDK. Live proxy checks returned HTTP 401 without proxy identity, HTTP 404 for raw `/containers/json` and delete-style routes, and HTTP 200 for the fixed allowlisted status route.
- Live Gateway checks returned HTTP 401 for missing, expired, wrong-audience, and claim-mismatched credentials, HTTP 403 for an authenticated unknown target, and HTTP 401 when replaying a credential whose first request returned HTTP 200.
- A real Redis outage produced dependency metric `0`, RCA confidence `0.92`, no action in recommendation-only mode, and `awaiting_approval` while Redis remained exited when approval was missing. Explicit approval restarted Redis through the Gateway and restricted proxy, then deep Verification completed with container running, service healthy, dependency metric restored, `status=resolved`, and `verified=true`.
- The independent Control API and Gateway SQLite stores both retained readable policy, execution, verification, allow, deny, workload subject, and credential-ID audit records.

Latest verification for the short-lived gateway workload identity milestone:

- All 36 backend tests passed in the rebuilt Control API image, including missing identity, expiry, wrong audience, action/target binding, replay denial, target allowlisting, successful typed execution, and all prior workflow/retrieval regressions.
- Compose configuration validation passed; rebuilt Control API and executor gateway production images were deployed successfully; all 13 services were running and final `make smoke` passed.
- A legacy reusable static Bearer token returned HTTP 401. A valid short-lived credential for an unknown target returned HTTP 403, and replaying that exact credential returned HTTP 401.
- A real Redis outage with `checkout cache cannot be reached` produced Redis metric `0`, RCA confidence `0.92`, and no execution in recommendation-only mode; missing approval remained `awaiting_approval`.
- Explicit approval restarted Redis through a newly minted workload credential and deep Verification resolved on check two with `status=resolved` and `verified=true`.
- The latest Gateway allow and deny audits contained `identity_subject=control-api` and a credential ID; consumed credential IDs persisted independently. The running Control API had neither the legacy token environment variable nor a Docker socket.

Latest verification for the incident-time evidence correlation milestone:

- All 34 backend tests passed in the rebuilt Control API image, including Alertmanager `startsAt` propagation, incident-window tool calls, unchanged public schemas, ten-case retrieval quality metrics, low-similarity rejection, embedding failure fallback, and vector anomaly fallback.
- The rebuilt Control API production image was deployed; its startup completed successfully and `make smoke` passed with `incident_context` evidence while retaining the original Prometheus and Loki data shapes.
- A real Redis outage with the fuzzy symptom `checkout cache cannot be reached` produced Redis metric `0`, one Loki error inside the bounded incident window, RCA confidence `0.92`, the Redis runbook, and no execution in recommendation-only mode.
- The same outage kept `execute=true, approved=false` at `awaiting_approval`; an approved unknown target was policy-denied without Verification.
- Live executor-gateway checks returned HTTP 401 without identity and HTTP 403 for an authenticated non-allowlisted target.
- Approved Redis recovery restarted Redis through the gateway and resolved on deep Verification check four with the container running, service healthy, dependency metric restored, and `verified=true`; final `make smoke` passed.
- A live Alertmanager firing event preserved its `startsAt` in `incident_context`, remained non-executing, and its resolved event only changed the incident to `alert_resolved`.
- Control API policy/approval/execution/verification audits and the gateway's independent allowed/denied audit records were both readable from their persistent SQLite stores after acceptance.

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
- `GET /api/v1/verification-policy/status`
- `GET /api/v1/verification-policy/peer-status` (internal authenticated peer read)
- `GET /api/v1/verification-policy/rollout`
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
- `apps/docker-proxy/app.py`: internal fixed-route Docker runtime proxy and operation-specific target allowlists; its optional file-target publisher is no longer configured or mounted by the default stack.
- `apps/container-metrics-exporter/app.py`: socketless CPU exporter backed by the Proxy's trimmed read-only stats route.
- `apps/control-api/opspilot/main.py`: HTTP routes, system status, CORS, and fault injection.
- `apps/control-api/opspilot/storage.py`: SQLite schema, normalized audit records, incident snapshots, revision history, and durable peer credential consumption.
- `apps/control-api/opspilot/policy_distribution.py`: authenticated remote source, accepted-only cache, request-bound peer identity, and bounded multi-node rollout reporter.
- `apps/policy-distributor/app.py`: authenticated read-only bundle endpoint used by the optional rollout profile.
- `apps/dashboard/app/page.tsx`: Dashboard behavior and UI.
- `apps/dashboard/app/globals.css`: Dashboard visual system.
- `apps/shared-service/app.py`: shared sample-application implementation.
- `infra/prometheus/alerts.yml`: alert rules.
- `infra/alertmanager/alertmanager.yml`: grouped webhook delivery to the Control API.
- `tests/test_workflow.py`: approval, policy allow/deny, Redis-path, graph inspection, inconclusive RCA, verification failure, and stale-log precedence tests.

## Current limitations

- LangGraph now provides the orchestration and checkpointed state; RCA and remediation policies remain deterministic and no LLM is connected yet.
- SQLite is appropriate for the single-node local MVP but is not intended for multi-replica Control API deployments.
- Typed deterministic retrieval, optional embedding-based semantic ranking, incident-time evidence correlation, and an expanded offline quality set are implemented; corpus embedding caches/vector indexes and learned long-term memory are not yet implemented.
- Authenticated pull distribution, per-node validation/cache fallback, request-bound replay-safe peer status, and bounded configured-node convergence reporting are implemented. The reporter remains observational rather than a quorum/consensus system; peer identity still uses a local shared HMAC key, and SQLite incident storage prevents active-active Control API writes from being a production topology.
- Error logs inside the bounded incident window can still represent a recently recovered failure. Metrics take precedence for Redis/MySQL RCA; richer per-source confidence and scrape-delay handling are not yet implemented.
- CPU observation now uses real Docker counters with strict per-service thresholds and health/staleness alerts, but the local exporter still polls on scrape, covers only the three business services, uses the local shared proxy token, and requires recreation to change targets or thresholds. Last-success timestamps are process-local and reset when the exporter restarts.
- Promtail mounts neither the Docker socket nor the host container-log directory. All three business services use runtime RFC5424 forwarding with label-preserving Promtail metrics; the default stack no longer configures Proxy file discovery, shared target files, or persisted positions. The local syslog receiver is published on TCP 1514 without transport authentication for the Docker Desktop MVP; production should use protected runtime transport or mTLS. Per-service freshness proves Promtail received each source, while the separate sent-entry signal remains stack-wide because Promtail does not label sent counters by service.
- The local HMAC workload identity now supports explicit key IDs and bounded current/previous key rotation, but key material and the Gateway-to-proxy token still come from local environment configuration. The proxy narrows the reachable Docker API surface but still ultimately owns a privileged Docker socket; production needs externally issued workload identity and an OS/runtime-enforced least-privilege executor rather than relying only on application route controls.
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

### Completed: short-lived gateway workload identity

- Replaced the reusable static Bearer token with per-request HMAC workload credentials capped at a short lifetime.
- Bound every credential to issuer, audience, subject, request method/path, typed operation, and target.
- Added atomic, persistent one-time `jti` consumption so captured credentials cannot be replayed.
- Correlated Gateway action audits with workload subject and credential ID while preserving the independent audit database.
- Revalidated legacy-token, expiry, audience, claim mismatch, replay, allowlist, recommendation-only, missing-approval, and approved Redis recovery paths.

### Completed: safe Gateway workload identity key rotation

- Added explicit configurable key IDs to minted credentials and exact key selection at the Gateway.
- Added current plus one previous verification key with an absolute overlap deadline and fail-fast configuration validation.
- Preserved default configuration compatibility, short credential lifetime, request/action/target binding, persistent replay denial, typed operations, and Gateway/Proxy isolation.
- Revalidated a live Gateway-first/Control-API-second rotation, old/new/unknown/expired keys, all identity denials, Proxy fixed-route denials, and the real Redis recovery path.

### Completed: restricted Docker runtime proxy

- Moved the Docker socket and Docker SDK out of the Gateway into a single-purpose internal proxy.
- Exposed only authenticated, fixed container status, restart, and stop routes with an independent target allowlist; raw Docker API paths return 404.
- Isolated the proxy on a non-published internal network shared only with the Gateway, so Control API cannot address it.
- Preserved workload credentials, Gateway typed actions, policy and approval gates, Gateway audit persistence, and execution-failure routing.
- Revalidated identity denials, replay denial, proxy denials, recommendation-only, missing approval, real Redis recovery, deep verification, and both audit stores.

### Completed: configurable per-service SLO verification

- Added validated defaults and partial per-service overrides for attempt budget, interval, service health condition, dependency metric threshold, and consecutive stable checks.
- Preserved the original default behavior, constructor compatibility, `IncidentState`, HTTP APIs, `OpsTools`, existing verification evidence fields, and Gateway/Proxy safety boundaries.
- Added tests for policy parsing, invalid configuration, service-specific strategies, default fallback, stable recovery, and bounded failure.
- Revalidated recommendation-only, missing approval, real timeout failure, approved Redis recovery after consecutive stable checks, identity/replay/target/proxy denials, and network isolation.

### Completed: safe hot-reload SLO policy management

- Added a strict, centrally mounted JSON policy document for default and per-service overrides without Control API recreation.
- Added content-addressed reload, immutable per-recovery snapshots, environment fallback, and last-known-good retention for missing, malformed, unknown-field, or constraint-invalid updates.
- Added a read-only reload status endpoint without introducing an unauthenticated mutation API.
- Revalidated strict-budget failure, invalid-update fallback, consecutive-stability recovery, recommendation-only, approval blocking, identity/replay/proxy denials, socket ownership, and network isolation against the live stack.

### Completed: signed SLO policy bundles and durable revision history

- Added an explicit key ID, monotonic revision, canonical policy digest, and HMAC-SHA256 signature wrapper around the existing strict policy document.
- Added opt-in strict signature enforcement with a trusted keyring while preserving the default local unsigned compatibility mode, environment fallback, hot reload, and immutable per-recovery snapshots.
- Persisted minimal accepted/rejected revision history without key material, and enforced rollback plus same-revision conflict rejection across Control API restarts.
- Revalidated valid signed load, tampering, unknown key, invalid signature, schema rejection, rollback, last-known-good, environment fallback, recommendation-only, approval blocking, approved Redis recovery, Gateway replay, Proxy fixed routes, network isolation, and execution-failure routing.

### Completed: authenticated multi-node policy rollout

- Added a profile-scoped, read-only policy distributor that requires Bearer identity and never accepts policy writes.
- Added opt-in authenticated pull distribution; every Control API node still independently validates signature, digest, schema, monotonic revision, and immutable Verification snapshots.
- Added accepted-only durable node caches so invalid content never replaces restart-safe last-known-good state and distributor partitions do not block verification.
- Added observed/accepted revision and load-result status plus a read-only multi-node rollout endpoint that separates convergence, rollout health, and offline nodes.
- Revalidated two-node convergence, tamper/unknown-key/rollback/schema rejection, canary and distributor outages, cache restart recovery, default local-file fallback, the real Redis recovery loop, and all Gateway/Proxy isolation boundaries.

### Completed: rollout control-plane hardening

- Preserved the existing unauthenticated local read-only status interface while moving node fan-out to a separate authenticated read-only peer endpoint; no policy mutation API was added.
- Reused short-lived HMAC workload credentials with explicit key ID, issuer/audience, source identity, unique `jti`, method/path/operation binding, and target-node binding.
- Persisted peer credential consumption per node so credential replay remains rejected after restart.
- Replaced serial fan-out with configurable bounded concurrency and per-peer timeout while preserving all successful results during partial failures.
- Added desired revision/digest plus explicit `converged`, `degraded`, `stalled`, and default-local `inactive` states without removing the existing rollout response fields.
- Revalidated healthy convergence, signature-rejection stall, canary loss, distributor loss with accepted-only cache, recovery, default local mode, the real Redis approval/recovery path, and Gateway/Proxy isolation.

### Completed: real container CPU observability and incident-loop integration

- Added a socketless exporter that converts real Docker CPU counter deltas into per-service core usage through an authenticated, trimmed, allowlisted Proxy stats route.
- Added Prometheus scraping, collection-health metrics, and a bounded payment-service high-CPU alert without attaching cAdvisor or another component to the Docker socket.
- Added container CPU evidence and deterministic high-CPU RCA behind the existing `OpsTools`/`IncidentWorkflow.run()` seams without changing `IncidentState` or Dashboard evidence shapes.
- Preserved bounded fault duration, Alertmanager non-execution, explicit policy and approval gates, Verification snapshots, revision rollback protections, and Gateway/Proxy isolation.
- Revalidated live CPU firing/resolution, persisted recommendation-only handling, the full Redis approval/recovery path, identity/replay/target denials, socket ownership, Compose, smoke, and the complete test suite.

### Completed: container metrics health and per-service CPU policy

- Added strict startup validation for exact per-target CPU thresholds and exported the active threshold for Prometheus label matching.
- Added retained last-success timestamps plus distinct alerts for exporter unavailability, per-target collection failure, and stale samples.
- Strengthened smoke to verify collection health, the active payment-service threshold, and fresh samples.
- Revalidated failure/staleness firing and recovery, threshold-driven real CPU firing/resolution, Alertmanager non-execution, the Redis approval/recovery path, and all Gateway/Proxy isolation boundaries.

### Completed: socketless Promtail log discovery

- Removed Promtail's Docker socket mount and Docker API discovery without changing Loki query labels, incident-time correlation, list-shaped evidence, or Dashboard formats.
- Reused the sole socket-owning restricted Proxy to discover only the current Compose project's three business services and atomically publish derived JSON log paths through a dedicated volume; no log-discovery HTTP API was added.
- Kept the last-known-good target file when discovery is empty or fails, mounted it read-only into Promtail, and strengthened smoke to require recent payment-service logs in Loki.
- Revalidated live Loki error ingestion, CPU alert firing/resolution, the complete Redis approval/recovery path, identity/replay/target/raw-route denials, socket ownership, Compose, smoke, and the complete test suite.

### Completed: persistent Promtail positions and log freshness

- Moved Promtail positions from process-local `/tmp` storage into a dedicated named volume and verified offsets survive forced container recreation and stop/start recovery.
- Added internal Proxy metrics for latest publication success, last-known-good publication time, discovered target count, and cumulative failed attempts while retaining the atomic target file on empty discovery or failure.
- Added Prometheus scraping and alerts for target publication failure, stale targets, and stale Promtail-to-Loki ingestion by reusing Promtail 3.5.3 native metrics.
- Strengthened smoke to require successful fresh publication and exactly three active Promtail targets without changing Loki labels, incident evidence, Dashboard formats, or any HTTP/workflow/tool contract.
- Revalidated all three new alert firing/recovery paths, positions persistence, live CPU firing/resolution, Alertmanager recommendation-only behavior, the full Redis approval/recovery path, Gateway/Proxy denials, network isolation, and socket ownership.

### Completed: per-service log freshness

- Added a last-known-good Proxy info metric that maps each allowlisted log path to its Compose service without changing Promtail targets or Loki stream labels.
- Joined the mapping to Promtail's per-path read counters in Prometheus, recorded `opspilot_service_log_read_fresh`, and added `ServiceLogCollectionStale` with the affected `service` label while retaining the stack-level Loki send alert.
- Strengthened smoke to generate traffic and require fresh signals for exactly the three business services.
- Revalidated isolated per-service firing/resolution, Alertmanager recommendation-only handling, live CPU and Redis flows, Gateway identity/replay/target denials, Proxy raw-route denial, network isolation, and sole socket ownership.

### Completed: payment-service runtime log forwarding

- Migrated `payment-service` from the host Docker JSON-log path to the Docker syslog logging driver and Promtail's RFC5424 receiver without adding a service or changing Loki query labels.
- Narrowed Proxy file discovery to user/order, retained their atomic last-known-good target publication and persistent positions, and exported a label-preserving Promtail runtime counter for the migrated service.
- Combined file-read and runtime-receive counters behind the existing `opspilot_service_log_read_fresh` recording rule and kept `ServiceLogCollectionStale`, stack-level Loki sending, Alertmanager non-execution, and all public/workflow contracts unchanged.
- Revalidated runtime logging driver state, Loki labels, isolated payment freshness firing/resolution, file offset continuity plus runtime reconnect across Promtail recreation, CPU/Redis flows, Gateway/Proxy denials, network isolation, socket ownership, Compose, smoke, and the complete test suite.

### Completed: three-service runtime log forwarding

- Migrated `user-service` and `order-service` to the already validated Docker RFC5424 syslog path, so all three business services now use one runtime pipeline without adding a service or changing Loki query labels.
- Removed the deployed Proxy discovery configuration and shared mount, Promtail file target, host Docker-log and positions mounts, and the obsolete file-publication alert rules. The Proxy's execution/stats boundary and sole socket ownership remain unchanged.
- Simplified `opspilot_service_log_read_fresh` to the label-preserving runtime counter with explicit zero fallback for all three services, while retaining isolated `ServiceLogCollectionStale`, stack-level Loki sending, Alertmanager non-execution, and all public/workflow contracts.
- Revalidated logging drivers, Loki labels, isolated user freshness firing/resolution, zero syslog parsing errors, CPU/Redis flows, Gateway/Proxy denials, network isolation, socket ownership, Compose, smoke, and the complete test suite.

### Then: knowledge and further production safety

- Completed deterministic SQLite-backed runbook and historical-incident retrieval with RCA/Solution evidence integration.
- Completed stable typed retrieval results, explainable scoring, verified/resolved historical ranking, and baseline offline evaluation fixtures.
- Completed incident-time Prometheus/Loki/Alertmanager evidence correlation and a larger labeled retrieval evaluation set with explicit quality metrics and fallback/anomaly cases.
- Consider a persisted embedding cache or vector index only when corpus size requires it.
- Replace local HMAC key material with externally issued workload identity when moving beyond the local stack.
- If active-active Control API deployment is required, move incident/audit persistence to a shared production database and add an external rollout controller or quorum model.
- Protect the local RFC5424 receiver with an authenticated runtime transport or mTLS before production use, then expand log coverage only through similarly socketless, label-preserving paths.

## Handoff prompt

Use this in a new conversation:

> Continue OpsPilot from `/Users/yaphet/code/OpsPilot`. Before changing anything, read `AGENTS.md`, `PROJECT_STATUS.md`, and `README.md`, then run `git status --short --branch`, `docker compose ps`, `docker compose config --quiet`, and `make smoke`. The default stack has 15 services; the persistent primary Verification-policy history has accepted signed revision 104. All three business services now use Docker runtime RFC5424 forwarding to Promtail; Promtail has no host Docker-log, target, positions, or socket mount, and the default Proxy deployment has no log-discovery mount. Existing `compose_service`/`container` Loki labels, three-service `opspilot_service_log_read_fresh`, `ServiceLogCollectionStale`, and stack-level Loki sending remain green. All Alertmanager handling is recommendation-only; policy allowlisting and explicit human approval remain independent. Preserve all HTTP interfaces, `IncidentState`, `IncidentWorkflow.run(request) -> IncidentState`, `OpsTools`, Dashboard evidence formats, Gateway/Proxy isolation, and the protected IDE/system files. Next protect TCP 1514 with authenticated runtime transport or mTLS before expanding production log coverage; rerun the complete test/deployment/live acceptance sequence, update docs, stage only functional files, commit, and autonomously push the validated node to `origin/main`.
