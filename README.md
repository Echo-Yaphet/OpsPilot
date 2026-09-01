# OpsPilot

OpsPilot 是一个面向智能运维闭环的多 Agent MVP。第一阶段使用确定性编排跑通：

`故障注入 → Prometheus/Loki 取证 → RCA → 方案生成 → 安全审查 → 人工审批 → 执行 → 验证`

当前默认只生成建议，不会自动执行修复。Alertmanager 会自动创建或更新事件，但容器重启被归类为中风险，仍必须同时传入 `execute=true` 和 `approved=true`。即使人工批准，执行策略也只允许精确的 `docker compose restart <已知服务>` 操作；其他命令或未知目标会被拒绝并记录明确原因。整个默认栈只有内部受限代理挂载 Docker socket；Control API 通过短期、一次性、请求绑定的 workload credential 调用 Gateway，Gateway 再通过隔离网络调用固定的容器状态、restart 或 stop 接口。三个业务服务现在都由 Docker logging driver 通过 RFC5424 syslog 在运行时转发到 Promtail，并保持原 Loki 标签与按服务新鲜度；Promtail 不再读取宿主 Docker JSON 日志，也不再需要 Proxy 文件发现或 positions 卷。

## 快速启动

要求：Docker Desktop、Docker Compose、curl，建议至少 6 GB 可用内存。

```bash
cp .env.example .env
make up
docker compose ps
make smoke
```

入口：

- Control API / OpenAPI：<http://localhost:8080/docs>
- OpsPilot Dashboard：<http://localhost:3001>
- Grafana：<http://localhost:3000>（admin / admin，也可匿名查看）
- Prometheus：<http://localhost:9090>
- Alertmanager：<http://localhost:9093>
- Loki：<http://localhost:3100/ready>
- 三个示例服务：<http://localhost:8001/health>、<http://localhost:8002/health>、<http://localhost:8003/health>

首次构建需拉取镜像，MySQL 健康检查通过后示例服务才会启动。

如果拉取镜像提示连接 `127.0.0.1:7890` 被拒绝，说明 Docker Desktop 配置了本机代理但代理未监听；启动对应代理或在 Docker Desktop 中关闭该代理后重试 `make up`。

## Redis 宕机最小链路验收

先启动系统并确认 `make smoke` 通过，然后：

```bash
make fault-redis
sleep 15
curl -sS -X POST http://localhost:8080/api/v1/incidents/analyze \
  -H 'content-type: application/json' \
  -d '{"service":"payment-service","symptom":"Redis unavailable"}'
```

验收响应应包含：

- `root_cause` 为 `Redis dependency is unavailable`
- `confidence` 为 `0.92`
- Prometheus 指标和 Loki 错误日志证据
- `docker compose restart redis` 建议
- `risk=medium`、`requires_approval=true`
- 未请求执行时 `status=recommendation_ready`

批准执行并验证容器恢复：

```bash
curl -sS -X POST http://localhost:8080/api/v1/incidents/analyze \
  -H 'content-type: application/json' \
  -d '{"service":"payment-service","symptom":"Redis unavailable","execute":true,"approved":true}'
```

> 整个默认栈只有 `docker-proxy` 挂载 Docker socket。该代理位于不映射宿主端口的内部网络，只暴露白名单容器的 status、stats、restart 和 stop 固定路由；原始 Docker API 和日志发现 API 均不可访问。新的容器指标 exporter 无 socket，只能使用本地代理身份读取三个业务容器裁剪后的 CPU 计数器。Gateway 本身也无 socket 和 Docker SDK，并继续只接受 `restart_container` 与故障演练所需的 `stop_container` 类型化操作。Control API 为每次调用签发最长 10 秒的 HMAC workload credential，绑定显式 key ID、issuer、audience、subject、方法、路径、操作和目标；Gateway 使用持久化 `jti` 防重放。user/order/payment 都通过 Docker 的 RFC5424 syslog driver 转发到 Promtail；Promtail 只挂载自身配置，不再挂载宿主容器日志目录、文件 target 卷或 positions 卷。runtime pipeline counter 保留三个服务的 `compose_service`、`container` 标签和按服务 freshness。本地 TCP 1514 接收器未启用传输认证，仅适用于 Docker Desktop MVP，生产应采用受保护的 runtime transport 或 mTLS。

## Gateway workload identity 密钥轮换

未配置新变量时仍使用原有 `EXECUTOR_IDENTITY_KEY` 和默认 key ID `control-api-v1`。轮换时，Gateway 只按凭证 header 中的明确 `kid` 选择验证 key，不会用多把 key 逐一试签。当前签名/验证 key 使用：

```bash
EXECUTOR_IDENTITY_KEY_ID=control-api-v2
EXECUTOR_IDENTITY_KEY=replace-with-new-secret
```

Gateway 可在一个明确截止的短窗口内同时接受上一把验证 key：

```bash
EXECUTOR_IDENTITY_PREVIOUS_KEY_ID=control-api-v1
EXECUTOR_IDENTITY_PREVIOUS_KEY=replace-with-old-secret
EXECUTOR_IDENTITY_PREVIOUS_KEY_VALID_UNTIL=1788175045 # Unix timestamp
EXECUTOR_IDENTITY_MAX_ROTATION_OVERLAP_SECONDS=3600
```

安全切换顺序是：先部署“新 key 为当前、旧 key 为上一把且带未来截止时间”的 Gateway；确认旧 Control API 仍可访问后，再把 Control API 切到新 key；窗口结束后从 Gateway 删除全部 `PREVIOUS` 配置。上一把 key 的 ID、secret、截止时间必须同时配置；重复 ID、重复 secret、非法/已过去的截止时间，或截止时间超过重叠上限都会让 Gateway 拒绝启动。重叠上限默认 3600 秒且只能配置为 1–86400 秒。未知 `kid` 与窗口结束后的旧 key 会在 Docker 访问前返回 401。凭证原有的短期 expiry、audience、请求/action/target 绑定和持久化 `jti` 防重放保持不变。

## 其他故障场景

```bash
make fault-cpu     # payment-service 执行最长 30 秒的有界 CPU 工作
make fault-mysql   # 停止 MySQL，并触发三个服务的健康检查
make recover       # 启动 Redis/MySQL 并重启 payment-service
```

CPU 场景现在使用真实 Docker CPU 计数器。`container-metrics-exporter` 通过受限代理的认证 stats 路由导出 CPU 用量、采集健康、最后成功时间和生效阈值。`CONTAINER_CPU_THRESHOLDS` 必须以 JSON 对象精确配置全部采集目标，值表示主机 CPU 核数且范围为 `(0, 1024]`；默认三个业务服务均为 `0.8`：

```bash
CONTAINER_CPU_THRESHOLDS={"user-service":0.7,"order-service":0.8,"payment-service":0.9}
```

Prometheus 通过 `service` 标签匹配用量与阈值，持续超过阈值 10 秒触发 `ContainerHighCPU`。exporter 不可抓取 30 秒触发 `ContainerMetricsExporterDown`，单服务采集失败 30 秒触发 `ContainerMetricsCollectionFailed`，最后成功样本超过 60 秒触发 `ContainerMetricsDataStale`。高 CPU 告警经现有 Alertmanager webhook 创建只建议 incident，确定性 RCA 返回 `Container CPU usage is high`，置信度 `0.9`，并建议在显式人工审批后重启对应服务；Alertmanager 自身永不请求执行。30 秒脚本和 Dashboard 的 15 秒动作仍保持有界。MySQL 场景继续使用同一条确定性 RCA 链路。

日志采集健康也由 Prometheus 显式监控。Prometheus 使用 Promtail 的 label-preserving runtime counter 记录 `opspilot_service_log_read_fresh{service=...}`；任一业务服务一分钟内无新日志并持续 30 秒时触发 `ServiceLogCollectionStale`。Promtail 不可抓取或一分钟内没有向 Loki 发送任何新日志时，栈级 `LokiLogIngestionStale` 继续兜底下游整体故障。旧文件 target 的发布/陈旧规则已随文件发现路径移除。所有这些基础设施告警仍只经 Alertmanager 创建或更新建议事件，不会自动执行修复。

## 目录结构

```text
apps/
  dashboard/          实时运维控制台、故障演练与 Agent 时间线
  control-api/        FastAPI 控制面、状态模型、Agent 工作流、Tools
  executor-gateway/   独立执行边界、身份校验、操作白名单与审计
  docker-proxy/       仅持有 socket 的受限容器运行时代理
  container-metrics-exporter/  通过受限 stats 路由导出真实容器 CPU 指标
  shared-service/     三个示例服务共享的最小实现
  user-service/       user-service 容器入口
  order-service/      order-service 容器入口
  payment-service/    payment-service 容器入口
infra/
  prometheus/         抓取与告警规则
  grafana/            数据源自动配置
  loki/               Loki 与 Promtail 配置
scripts/
  faults/             三种故障注入
tests/                Agent 闭环与安全门测试
```

## Agent 与状态模型

`IncidentState` 是所有节点共享的状态，保留 evidence、events、root cause、confidence、recommendations、execution 和 verification 结果。节点接口已包括 Coordinator、Monitor、Log、RCA、Solution、Safety、Executor、Verification。

当前 `IncidentWorkflow.run()` 是稳定入口，内部使用真实 LangGraph `StateGraph` 编排八个 Agent 节点，RCA 与修复策略仍采用确定性规则，因此无需模型密钥即可验收。HTTP 接口、工具 seam 和状态模型保持不变。

RCA 完成初步判断后会通过独立的知识检索 seam 查询 SQLite 中的确定性 runbook 和同服务、同根因的历史 incident。检索 seam 使用稳定的类型化结果；写入 `runbook` 和 `incident_history` evidence 时仍序列化为原有字典结构，不增加或改变 `IncidentState` 字段。每个结果包含总分和 `score_explanation` 因子，历史记录优先采用已验证且已解决的结果。Solution 优先使用精确命中的 runbook 标题和类型化策略可校验的命令。当前内置 Redis、MySQL 和服务降级三份基础 runbook，并用离线 Redis/MySQL fixtures 锁定基础命中，为后续语义 RAG 保留可评估的替换边界。

可选语义排序层现已位于同一 seam 后方。默认不配置模型时仍只使用 SQLite 确定性检索；配置 OpenAI-compatible embedding endpoint 后，语义结果只补充确定性结果，精确命中的顺序和原有数字分数不会被降低。embedding 服务超时、报错或返回异常数据时会自动回退，不影响 Control API 启动和分析。可选环境变量为 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_API_KEY`、`EMBEDDING_TIMEOUT` 和 `SEMANTIC_MINIMUM_SIMILARITY`。语义命中仍只影响 RCA/Solution evidence 与建议，执行必须继续通过策略和人工审批门。

Prometheus、Loki 和 Alertmanager 取证现共享事故时间上下文。手动分析以请求开始时间为锚点；Alertmanager firing 事件使用原始 `startsAt`。Prometheus 查询锚定事故时刻，Loki 只查询事故前两分钟至后五分钟（不超过当前时间）的窗口，减少十分钟滚动窗口内旧错误的干扰。关联范围、来源、查询模式和结果数量写入新增的 `incident_context` evidence；原有 Prometheus/Loki evidence 数据格式、`IncidentState` 和 HTTP schema 保持不变。指标仍优先于日志完成 Redis/MySQL RCA。

批准执行后，Verification Agent 会进行有界轮询，同时要求修复目标容器运行、受影响服务健康条件满足、Prometheus 依赖指标达到阈值，并连续稳定指定次数。任一条件在时限内未稳定，incident 会进入 `verification_failed`，并在原有 verification evidence 字段之外记录生效策略、当前连续稳定次数和要求的稳定次数。

恢复 SLO 可通过环境变量配置；未配置时仍保持原有的 6 次检查、2 秒间隔、`healthy` 健康条件、指标阈值 `1` 和单次稳定即成功：

```bash
VERIFICATION_MAX_ATTEMPTS=6
VERIFICATION_CHECK_INTERVAL_SECONDS=2
VERIFICATION_SERVICE_HEALTH_CONDITION=healthy # healthy 或 status_ok
VERIFICATION_DEPENDENCY_METRIC_THRESHOLD=1
VERIFICATION_RECOVERY_STABLE_CHECKS=1
```

`VERIFICATION_SERVICE_POLICIES` 接受 JSON 格式的按服务部分覆盖，未出现的字段和服务自动回退到上述默认策略。所有次数、间隔、健康条件、阈值以及“稳定次数不得超过最大尝试次数”等约束都会在 Control API 启动时校验。例如：

```bash
VERIFICATION_SERVICE_POLICIES={"payment-service":{"max_attempts":8,"recovery_stable_checks":2}}
```

Compose 还会把 `infra/opspilot/verification-policies.json` 目录只读挂载到 Control API。运维侧可原子替换该集中策略文件，无需重建或重启容器；文件中的 `defaults` 覆盖环境默认值，`services` 提供按服务覆盖，未声明字段仍按上述回退规则合并：

```json
{
  "defaults": {"max_attempts": 8},
  "services": {
    "payment-service": {"recovery_stable_checks": 2}
  }
}
```

每次 Verification 在开始时锁定一个不可变策略快照，避免热更新改变正在进行的恢复判定。无效 JSON、未知字段或违反约束的更新不会替换当前策略，而是继续使用最后一次有效版本。`GET /api/v1/verification-policy/status` 可查看当前来源、内容版本、已配置服务和最近一次加载错误；该接口只读，不提供未认证的策略写入能力。

### 签名策略 bundle 与 revision 历史

默认单节点本地模式继续兼容上述无签名 JSON。需要安全分发时，可显式启用严格签名模式：

```bash
VERIFICATION_POLICY_SIGNING_KEYS={"opspilot-policy-v1":"replace-with-a-secret"}
VERIFICATION_POLICY_REQUIRE_SIGNATURE=true
```

严格模式下，策略文件必须是以下 bundle；`policy` 内部仍使用原有严格 schema：

```json
{
  "key_id": "opspilot-policy-v1",
  "revision": 42,
  "content_digest": "sha256:<canonical-policy-sha256>",
  "policy": {
    "defaults": {"max_attempts": 8},
    "services": {"payment-service": {"recovery_stable_checks": 2}}
  },
  "signature": "hmac-sha256:<signature>"
}
```

摘要是对 `policy` 的 UTF-8 canonical JSON（key 排序、无多余空白）计算 SHA-256；HMAC-SHA256 签名覆盖同样 canonical 化的 `content_digest`、`key_id` 和整数 `revision`。内部 helper `create_signed_verification_policy_bundle()` 可供受信部署工具生成同一格式。未知 key、摘要不符、签名错误、schema 错误、低于已接受 revision 的回退，以及同 revision 不同摘要的冲突都会被拒绝；当前进程继续使用 last-known-good。已接受的签名 revision 会持久化，因此容器重启后仍不能回退。SQLite 历史只记录 revision、摘要、签名状态、加载结果和时间，不保存 key ID 或密钥材料。状态接口新增 `bundle_revision`、`content_digest`、`key_id`、`signature_status` 和 `signature_required`，仍保持只读。

### 认证多节点分发与收敛报告

默认配置仍直接读取本地只读文件，不依赖任何协调服务。需要渐进式多节点 rollout 时，先把受信部署工具生成的签名 bundle 原子写入 `infra/opspilot/verification-policy-distribution.json`，再显式启用严格签名和认证分发：

```bash
VERIFICATION_POLICY_SIGNING_KEYS='{"opspilot-policy-v1":"replace-with-signing-secret"}' \
VERIFICATION_POLICY_REQUIRE_SIGNATURE=true \
VERIFICATION_POLICY_DISTRIBUTION_URL=http://policy-distributor:8070/bundle \
VERIFICATION_POLICY_DISTRIBUTION_TOKEN=replace-with-distribution-token \
VERIFICATION_POLICY_NODE_ID=control-api-primary \
VERIFICATION_POLICY_ROLLOUT_NODES='{"control-api-canary":"http://control-api-canary:8080"}' \
VERIFICATION_POLICY_PEER_IDENTITY_KEY=replace-with-peer-signing-secret \
docker compose --profile policy-rollout up -d --build
```

分发服务只有带 Bearer 身份的 `GET /bundle`，没有策略写入 API，也不映射宿主端口。每个 Control API 节点独立完成 key ID、HMAC、digest、严格 schema 和单调 revision 校验；只有接受成功的 bytes 才会原子更新该节点 `/data/verification-policy-cache.json`。篡改或无效更新不会覆盖缓存，分发服务离线或节点重启时继续使用本节点 last-known-good。未配置远端 URL 时，这些组件不会影响默认单节点启动。

本地兼容接口 `GET /api/v1/verification-policy/status` 保持无需身份且只读。节点 fan-out 不再访问该接口，而是为每个目标即时签发最长 10 秒的 HMAC credential，绑定 key ID、issuer/audience、来源节点、`jti`、`GET`、peer-status 路径、读取操作和目标节点 ID；目标节点在自己的 SQLite 中原子消费 `jti`，重启后仍拒绝重放。内部 peer-status 端点缺失、错误、过期、请求不匹配或已消费的 credential 均返回 401。共享 peer key 仍只是本地 MVP 默认值，生产应替换为外部 workload identity。

`GET /api/v1/verification-policy/status` 同时显示 observed 与 accepted revision/digest、加载结果、分发连通性和缓存状态。`GET /api/v1/verification-policy/rollout` 使用 `VERIFICATION_POLICY_ROLLOUT_MAX_CONCURRENCY`（默认 4，范围 1–32）限制并行 peer 查询，并受 `VERIFICATION_POLICY_ROLLOUT_TIMEOUT` 约束；一个节点超时或离线不会丢弃其他节点结果。响应明确给出 `desired` revision/digest，以及 `rollout_state=converged|degraded|stalled|inactive`：全部节点接受 desired 且来源正常为 `converged`，peer 或分发源部分失败为 `degraded`，节点均在线但无法接受 desired 为 `stalled`，默认未启用签名 rollout 的单节点为 `inactive`。原有 `converged`、`healthy`、在线节点数和节点详情字段仍保留；该接口不是策略写入面或分布式共识系统。

Safety Agent 会在执行前生成 `local-compose-restart-v1` 策略决策。决策同时进入 incident evidence 和 SQLite `policy_decisions` 审计表；允许的动作以类型化 `restart_container` 和即时签发的一次性 workload credential 发送到独立 executor gateway，任意 shell 命令不会穿过该接口。Gateway 会按显式 key ID 选择当前或尚在重叠窗口内的上一验证 key，再验证凭证时效、请求绑定和 `jti` 唯一性，并把允许、拒绝和执行失败连同 workload subject、credential ID 和 key ID 写入自己的持久化 SQLite 审计库。通过验证后，Gateway 只能经专用内部网络调用 `docker-proxy` 的固定路由；Control API 不在该网络，Gateway 不再挂载 socket 或安装 Docker SDK。显式人工审批门与策略白名单是两个独立且都必须通过的安全条件。

## 事件与持久化 API

Control API 将 incident 完整状态和审计记录保存在 Docker volume 中的 SQLite 数据库，Dashboard 刷新或 Control API 重启后仍能恢复列表、详情、证据和 Agent 时间线。

- `POST /api/v1/alertmanager/webhook`：Alertmanager webhook；按 fingerprint 去重，只触发建议模式。
- `GET /api/v1/incidents`：按最近更新时间列出 incident。
- `GET /api/v1/incidents/{incident_id}`：读取 incident 完整详情。
- `POST /api/v1/incidents/analyze`：保持原接口兼容；可选传入 `incident_id` 对已有事件审批执行。

## 测试与停止

```bash
make test
make down
```
