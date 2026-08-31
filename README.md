# OpsPilot

OpsPilot 是一个面向智能运维闭环的多 Agent MVP。第一阶段使用确定性编排跑通：

`故障注入 → Prometheus/Loki 取证 → RCA → 方案生成 → 安全审查 → 人工审批 → 执行 → 验证`

当前默认只生成建议，不会自动执行修复。Alertmanager 会自动创建或更新事件，但容器重启被归类为中风险，仍必须同时传入 `execute=true` 和 `approved=true`。即使人工批准，执行策略也只允许精确的 `docker compose restart <已知服务>` 操作；其他命令或未知目标会被拒绝并记录明确原因。Docker socket 仅挂载到内部受限代理；Control API 通过短期、一次性、请求绑定的 workload credential 调用 Gateway，Gateway 再通过隔离网络调用固定的容器状态、restart 或 stop 接口。

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

> Docker socket 仅挂载到 `docker-proxy`。该代理位于不映射宿主端口的内部网络，只暴露白名单容器的状态、restart 和 stop 路由；原始 Docker API 不可访问。Gateway 本身无 socket 和 Docker SDK，并继续只接受 `restart_container` 与故障演练所需的 `stop_container` 类型化操作。Control API 为每次调用签发最长 10 秒的 HMAC workload credential，绑定 issuer、audience、subject、方法、路径、操作和目标；Gateway 使用持久化 `jti` 防重放。默认共享签名密钥和代理 token 仅适用于本地 MVP，生产环境仍应接入外部 workload identity、密钥轮换以及操作系统级的运行时权限隔离。

## 其他故障场景

```bash
make fault-cpu     # payment-service 执行最长 30 秒的有界 CPU 工作
make fault-mysql   # 停止 MySQL，并触发三个服务的健康检查
make recover       # 启动 Redis/MySQL 并重启 payment-service
```

CPU 场景目前完成指标/告警基础，MySQL 场景已进入同一条确定性 RCA 链路。后续阶段会补容器 CPU exporter、告警 webhook 和更丰富的修复策略。

## 目录结构

```text
apps/
  dashboard/          实时运维控制台、故障演练与 Agent 时间线
  control-api/        FastAPI 控制面、状态模型、Agent 工作流、Tools
  executor-gateway/   独立执行边界、身份校验、操作白名单与审计
  docker-proxy/       仅持有 socket 的受限容器运行时代理
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

Safety Agent 会在执行前生成 `local-compose-restart-v1` 策略决策。决策同时进入 incident evidence 和 SQLite `policy_decisions` 审计表；允许的动作以类型化 `restart_container` 和即时签发的一次性 workload credential 发送到独立 executor gateway，任意 shell 命令不会穿过该接口。Gateway 会验证凭证时效、请求绑定和 `jti` 唯一性，并把允许、拒绝和执行失败连同 workload subject/credential ID 写入自己的持久化 SQLite 审计库。通过验证后，Gateway 只能经专用内部网络调用 `docker-proxy` 的固定路由；Control API 不在该网络，Gateway 不再挂载 socket 或安装 Docker SDK。显式人工审批门与策略白名单是两个独立且都必须通过的安全条件。

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
