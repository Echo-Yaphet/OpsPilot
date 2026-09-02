# Orchestrator-native runtime plane

This Kustomize package places one workload, actuator, and runtime-executor in each Kubernetes Pod. `shareProcessNamespace: true` lets the actuator see only processes in that workload Pod; it has a read-only root, `allowPrivilegeEscalation: false`, RuntimeDefault seccomp, and only `CAP_KILL`. The broker has no Kubernetes API credential and reaches the actuator only through the placement PVC's Unix socket.

The five target Pods carry distinct placement IDs (`kubernetes/opspilot/<target>`). The Gateway routes by a strict target registry, the external issuer signs that placement into the request-bound RS256 credential, and each broker rejects credentials for any other placement before consuming the `jti` or touching its actuator. Kubernetes schedules each self-contained placement independently, so targets can move across hosts without changing identity or routing contracts; the preferred anti-affinity is deliberately soft so a single-node development cluster remains schedulable.

All broker replicas use `runtime-audit-database/url`. PostgreSQL's primary key makes credential consumption atomic across nodes, and audit rows include placement plus the executing Pod UID. The checked-in StatefulSet is an acceptance baseline; production should supply a managed HA PostgreSQL endpoint through the same Secret contract.

Before applying, publish the five local images to the cluster registry and create these Secrets in namespace `opspilot`:

- `runtime-audit-database`: keys `password` and `url` (a PostgreSQL DSN)
- `workload-identity-issuer-signing`: key `private.pem`
- `workload-identity-issuer-public`: key `public.pem`
- `workload-identity-client-public-keys`: keys `control-api.pem`, `executor-gateway.pem`, `container-metrics-exporter.pem`
- `executor-gateway-proof-key`: key `private.pem`
- `mysql-runtime-environment`: MySQL image bootstrap variables
- `service-runtime-environment`: `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, and `REDIS_URL`

Label Control API and the metrics exporter Pods with `opspilot.io/runtime-client: "true"`; mount their existing proof keys and give the metrics exporter the same `runtime-executor-placements` ConfigMap value. Control API continues to reach only the Gateway Service and is not granted direct runtime-broker access.

Render without a cluster:

```bash
kubectl kustomize infra/kubernetes/runtime-plane
```

Apply only after replacing image references and provisioning the external Secrets:

```bash
kubectl apply -k infra/kubernetes/runtime-plane
```

The Kubernetes API is never used as an execution API: there is no ServiceAccount token, RBAC grant, Docker socket, arbitrary command route, or action beyond the existing typed status/stats/restart/stop contracts.
