# OpsPilot

OpsPilot 是一个面向智能运维闭环的多 Agent MVP。第一阶段使用确定性编排跑通：

`故障注入 → Prometheus/Loki 取证 → RCA → 方案生成 → 安全审查 → 人工审批 → 执行 → 验证`

当前默认只生成建议，不会自动执行修复。Alertmanager 会自动创建或更新事件，但容器重启被归类为中风险，仍必须同时传入 `execute=true` 和 `approved=true`。即使人工批准，执行策略也只允许精确的 `docker compose restart <已知服务>` 操作；其他命令或未知目标会被拒绝并记录明确原因。默认栈已完全移除 Docker socket：Control API 以短期、一次性、请求绑定的 workload credential 调用 Gateway，Gateway 再调用独立身份 broker；broker 只经每目标私有 Unix socket 请求无网络、只具 `CAP_KILL` 的 actuator。每个目标加入对应 actuator 拥有的 PID namespace，因此内核把执行能力限制在单一目标进程。三个业务服务继续由 Docker logging driver 通过 mTLS RFC5424 syslog 在运行时转发到 Promtail，并保持原 Loki 标签与按服务新鲜度。

## 快速启动

要求：Docker Desktop、Docker Compose、curl，建议至少 6 GB 可用内存。

```bash
cp .env.example .env
make up
docker compose ps
make smoke
```

`make up` 会先准备被 Git 忽略的 `work/runtime-log-secrets` 投影。未配置外部来源时，本地开发 CA 仍在 `work/runtime-log-pki` 生成并复用，再通过与生产相同的严格导入器投影；运行时目录不包含 CA 私钥。直接使用 `docker compose up` 前应先运行 `make runtime-log-pki`。Docker daemon 读取客户端证书时需要绝对宿主路径；项目不在默认目录时，在 `.env` 中把 `RUNTIME_LOG_SECRET_DIR` 设置为对应绝对路径。证书和私钥不会进入镜像或 Git。

### 外部 PKI / Secret 交付与无中断轮换

外部 PKI 或 Secret agent 应把一个完整版本投递到独立暂存目录，再设置 `RUNTIME_LOG_SECRET_SOURCE_DIR`。目录必须含 `bundle.json`（只允许 `version`、`issuer`）、`ca.pem` 信任 bundle、`server-cert.pem`、`server-key.pem`，以及 `user-service`、`order-service`、`payment-service` 各自的 `*-cert.pem`/`*-key.pem`；可选 `crl.pem` 启用叶证书 CRL 检查。导入器会验证版本不可变、有效期、服务端 `host.docker.internal` SAN、server/client EKU、证书与私钥匹配、三项客户端 CN 和 CRL，然后只投影网关所需的 server key/trust/CRL 与每项客户端自己的材料。源目录中的 CA 私钥即使存在也永不投影给运行时。

```bash
RUNTIME_LOG_SECRET_SOURCE_DIR=/run/secrets/opspilot-runtime-log \
RUNTIME_LOG_SECRET_DIR=/absolute/host/path/runtime-log-secrets \
make runtime-log-pki
```

安全轮换使用两次外部版本发布：先发布同时信任旧/新签发链、但尚未吊销旧客户端的重叠 bundle；再发布保留当前身份并加入 CRL（或移除旧根）的退役 bundle。每次运行：

```bash
RUNTIME_LOG_SECRET_SOURCE_DIR=/run/secrets/opspilot-runtime-log \
make runtime-log-rotate
```

轮换器会先证明现有三个客户端仍被新信任 bundle 接受、当前服务器仍被新 trust bundle 接受，拒绝没有安全重叠的切换。跨 CA 切换严格分三阶段：先只扩展网关 trust/CRL 并第一次 HUP；再按 user/order/payment 顺序滚动客户端到新身份与新 trust；最后才投影新服务器身份并第二次 HUP。每次 HUP 都先以新 TLS context 在 1514 上建立替代监听，再关闭旧监听并让现有连接重新认证，不重启 Promtail。每项客户端重建都等待健康，最后重新验证无客户端证书拒绝、错误主机名拒绝、合法 mTLS 与 Loki 投递，并确认 Promtail 容器 ID 未改变。Docker Desktop 的 daemon-side syslog 驱动通过一次只读目录同步规避原子 inode 更新的可见性窗口；Linux daemon 上该步骤无副作用。

#### Vault Agent 交付控制器

仓库提供了可直接部署的 HashiCorp Vault Agent 接入，且不改变上述 provider-neutral 目录合约。`infra/vault-agent/runtime-log-policy.hcl` 只授予 `secret/data/opspilot/runtime-log` 读取权限；`runtime-log-agent.hcl.example` 使用 AppRole、一次性 Secret ID、`0600` token sink 和模板变更 command hook。Vault Agent 应运行在 Docker host 上，由 host Docker CLI 执行原有受限轮换脚本；它本身和任何新容器都不挂载 Docker socket。

先以具备写权限的受信发布身份，把一个已验证的完整源目录写成单一 KV v2 revision：

```bash
RUNTIME_LOG_SECRET_SOURCE_DIR=/secure/staging/runtime-log-v2 \
RUNTIME_LOG_VAULT_KV_MOUNT=secret \
RUNTIME_LOG_VAULT_KV_PATH=opspilot/runtime-log \
make runtime-log-vault-publish
```

KV data 的 key 与 bundle 文件名完全一致：`bundle.json`、`ca.pem`、server certificate/key、三组 service certificate/key，以及可选 `crl.pem`。发布脚本先调用同一严格校验器，之后用一次 `vault kv put` 原子创建新 revision；CA 私钥和未知字段不会发布。为 Agent 安装最小 policy/AppRole 后，复制 `infra/vault-agent/runtime-log-agent.hcl.example`，替换 Vault 地址、CA 和宿主路径，再以宿主服务管理器运行 `vault agent -config=<配置文件>`。

```bash
vault policy write opspilot-runtime-log infra/vault-agent/runtime-log-policy.hcl
vault write auth/approle/role/opspilot-runtime-log \
  token_policies=opspilot-runtime-log token_ttl=10m token_max_ttl=30m \
  secret_id_ttl=10m secret_id_num_uses=1
vault read -field=role_id auth/approle/role/opspilot-runtime-log/role-id
vault write -field=secret_id -f auth/approle/role/opspilot-runtime-log/secret-id
```

最后两个值分别写入配置所引用的 `role-id`/`secret-id` 文件并设为 `0600`；Agent 成功读取后会删除 Secret ID 文件。若更改默认 KV mount/path，必须同步修改 publisher 变量、只读 policy 和模板中的 `secret` 路径。

Agent 将单个 KV revision 原子渲染到被 Git 忽略的 `work/runtime-log-vault-agent/rendered-bundle.json`。command hook 调用与 `make runtime-log-vault-apply` 相同的受控入口：控制器要求正整数 `.Data.metadata.version`，拒绝旧 revision、同 revision 内容冲突、缺失/额外 key 和非完整渲染；随后建立不可变本地快照并再次执行证书、身份、CRL 和 key-pair 校验。首次启动只建立最小运行投影；已有投影时复用 `gateway trust → rolling clients → gateway identity` 热轮换。只有整个操作成功才原子推进 `accepted.json`，失败时保留上一已接受 revision 和全部不可变快照，便于在安全重叠材料下重试；删除该状态以允许 Vault 版本回退属于显式 break-glass 操作。

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

> 默认栈没有任何 Docker socket 挂载。`runtime-executor` broker 位于不映射宿主端口的内部网络，只暴露白名单 status、stats、restart 和 stop 固定路由；原始 Docker API 不存在。Control API、Gateway 和指标 exporter 分别持有自己的非对称 workload proof key，向独立签发器领取最长 10 秒的 RS256 credential。Gateway/exporter→broker 凭证绑定 issuer、audience、subject、方法、路径、operation、target、placement 和唯一 `jti`；Gateway 与 broker 分别持久化审计和消费记录。broker 无 Docker 权限，只能写入每目标私有 Unix socket 卷；actuator 无网络、只具 `CAP_KILL`，并拥有目标加入的 PID namespace。策略白名单与人工审批仍独立。

## 外部 workload identity

首次启动时，一次性 bootstrap job 在四个独立命名卷中生成签发器、Control API、Gateway 和 metrics exporter 的 RSA key pair。每个调用方只挂载自己的 proof private key；Gateway 与 runtime executor 只挂载签发器 public key；只有签发器挂载 JWT signing private key。签发器验证调用方对完整领取请求的签名、时间戳和一次性 nonce，并按 subject 独立限制 audience 与 operation，随后签发请求绑定的短期 RS256 credential。

签发器无宿主端口且不挂载 Docker socket。Gateway 调 runtime executor 与 metrics exporter 读取 stats 均不发送静态 shared token。签发器、Gateway 和 runtime executor 的 nonce/`jti` 消费状态各自持久化；未知 workload/audience/operation、过期 proof、重复 nonce、错误签名、错误 issuer/audience/path/action/target 和重复 `jti` 都会在 actuator 访问前拒绝。生产轮换应先让验证方信任新的签发 public key，再切换签发器 signing key，最后在所有旧凭证最长 TTL 结束后移除旧信任；调用方 proof key 可按 workload 独立轮换。

## Kubernetes 多主机 runtime plane

`infra/kubernetes/runtime-plane` 提供可由 Kustomize 渲染的五目标部署包。每个 workload 与自己的 actuator、runtime executor 同 Pod 并启用共享进程命名空间；actuator 保持只读根目录、禁止提权且只有 `CAP_KILL`，broker 不挂载 ServiceAccount token，也不访问 Kubernetes API。Gateway 与 metrics exporter 按严格 target→URL→placement registry 路由，签发器把 placement 写入 RS256 credential，目标 broker 必须精确匹配后才消费 `jti` 和访问 Unix socket。

多 broker 通过 `RUNTIME_EXECUTOR_DATABASE_URL` 使用 PostgreSQL：全局主键原子拒绝跨节点重放，action audit 同时记录 placement 与 executor instance。未配置 DSN 时默认 Compose 继续使用兼容 SQLite。部署前需要准备镜像和外部 Secret；完整清单、Secret contract 与 NetworkPolicy 见 `infra/kubernetes/runtime-plane/README.md`。本地可重复验收：

```bash
make runtime-identity-validate
make runtime-orchestrator-validate
kubectl kustomize infra/kubernetes/runtime-plane >/tmp/opspilot-runtime-plane.yaml
```

## 其他故障场景

```bash
make fault-cpu     # payment-service 执行最长 30 秒的有界 CPU 工作
make fault-mysql   # 停止 MySQL，并触发三个服务的健康检查
make recover       # 启动 Redis/MySQL 并重启 payment-service
```

CPU 场景现在使用 actuator 裁剪后的真实目标进程 CPU 计数器。`container-metrics-exporter` 通过 runtime executor 的认证 stats 路由导出 CPU 用量、采集健康、最后成功时间和生效阈值。`CONTAINER_CPU_THRESHOLDS` 必须以 JSON 对象精确配置全部采集目标，值表示主机 CPU 核数且范围为 `(0, 1024]`；默认三个业务服务均为 `0.8`：

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
  runtime-executor/   外部身份验证、固定路由、审计与 actuator Unix socket 分发
  runtime-actuator/   无网络、每目标 PID namespace 与 CAP_KILL 强制边界
  container-metrics-exporter/  通过 actuator 的裁剪 stats 路由导出真实 CPU 指标
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

当前 `IncidentWorkflow.run()` 是稳定入口，内部使用真实 LangGraph `StateGraph` 编排八个 Agent 节点。默认仍可用确定性 RCA 与修复策略在无模型环境完成验收；配置本地 Ollama 后，RCA 节点会增加结构化模型分析，Solution 节点采用模型生成的非执行性建议标题。HTTP 接口、工具 seam 和状态模型保持不变。

RCA 完成初步判断后会通过独立的知识检索 seam 查询 SQLite 中的确定性 runbook 和同服务、同根因的历史 incident。检索 seam 使用稳定的类型化结果；写入 `runbook` 和 `incident_history` evidence 时仍序列化为原有字典结构，不增加或改变 `IncidentState` 字段。每个结果包含总分和 `score_explanation` 因子，历史记录优先采用已验证且已解决的结果。Solution 优先使用精确命中的 runbook 标题和类型化策略可校验的命令。当前内置 Redis、MySQL 和服务降级三份基础 runbook，并用离线 Redis/MySQL fixtures 锁定基础命中，为后续语义 RAG 保留可评估的替换边界。

可选语义排序层现已位于同一 seam 后方。默认不配置模型时仍只使用 SQLite 确定性检索；配置 OpenAI-compatible embedding endpoint 后，语义结果只补充确定性结果，精确命中的顺序和原有数字分数不会被降低。embedding 服务超时、报错或返回异常数据时会自动回退，不影响 Control API 启动和分析。可选环境变量为 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_API_KEY`、`EMBEDDING_TIMEOUT` 和 `SEMANTIC_MINIMUM_SIMILARITY`。语义命中仍只影响 RCA/Solution evidence 与建议，执行必须继续通过策略和人工审批门。

本地 LLM 通过可选 Ollama adapter 接入。模型只接收有界的指标、CPU、错误日志、Runbook 和历史 Incident 上下文，并返回严格校验的 `root_cause`、`confidence`、`rationale` 与 `recommendation_title`；日志和历史文本在 prompt 中被明确视为不可信数据。已知故障的确定性根因、执行 target 和命令不会被模型覆盖；证据不足时，模型候选置信度最高限制为 `0.79`。超时、连接失败、空响应或 schema 错误会记录 `llm_analysis` 降级证据并继续确定性流程。

Docker Compose 中可使用宿主机 Ollama：

```bash
LLM_BASE_URL=http://host.docker.internal:11434
LLM_MODEL=gemma3:latest
LLM_TIMEOUT=90
```

模型输出会作为 `llm_analysis` evidence 显示在 Dashboard，模型只能改善 RCA 解释和建议标题；固定操作、策略白名单、风险分级、人工审批、runtime identity、防重放和 Verification 均保持在模型边界之外。

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

Safety Agent 会在执行前生成 `local-compose-restart-v1` 策略决策。决策同时进入 incident evidence 和 SQLite `policy_decisions` 审计表；允许的动作以类型化 `restart_container` 和即时签发的一次性 workload credential 发送到独立 executor gateway，任意 shell 命令不会穿过该接口。Gateway 会验证凭证时效、请求绑定和 `jti` 唯一性，并把允许、拒绝和执行失败连同 workload subject、credential ID 和 key ID 写入自己的持久化 SQLite 审计库。通过验证后，Gateway 只能经专用内部网络调用 `runtime-executor` 固定路由；broker 再通过目标专属 Unix socket 请求 actuator，无法访问 Docker API 或其他 actuator。Control API 不在执行网络。显式人工审批门与策略白名单是两个独立且都必须通过的安全条件。

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
