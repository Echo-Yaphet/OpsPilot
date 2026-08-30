# OpsPilot

OpsPilot 是一个面向智能运维闭环的多 Agent MVP。第一阶段使用确定性编排跑通：

`故障注入 → Prometheus/Loki 取证 → RCA → 方案生成 → 安全审查 → 人工审批 → 执行 → 验证`

当前默认只生成建议，不会自动执行修复。Alertmanager 会自动创建或更新事件，但容器重启被归类为中风险，仍必须同时传入 `execute=true` 和 `approved=true`。即使人工批准，执行策略也只允许精确的 `docker compose restart <已知服务>` 操作；其他命令或未知目标会被拒绝并记录明确原因。Docker socket 仅挂载到独立的 executor gateway，Control API 通过带内部身份的类型化请求调用它。

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

> Docker socket 仅挂载到独立 executor gateway。网关只接受经过 Bearer 身份校验的 `restart_container` 和故障演练所需的 `stop_container` 类型化操作，并对每种操作使用独立目标白名单。当前共享 token 仅适用于本地 MVP；生产环境仍应使用短期 workload identity 和更窄的 Docker/API 权限。

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

批准执行后，Verification Agent 会进行有界轮询，同时要求修复目标容器运行、受影响服务 `/health` 恢复、Prometheus 依赖指标恢复为 `1`。任一条件在时限内未恢复，incident 会进入 `verification_failed`，并在 evidence 中保留最后一次检查结果。

Safety Agent 会在执行前生成 `local-compose-restart-v1` 策略决策。决策同时进入 incident evidence 和 SQLite `policy_decisions` 审计表；允许的动作以类型化 `restart_container` 发送到独立 executor gateway，任意 shell 命令不会穿过该接口。网关把允许、拒绝和执行失败写入自己的持久化 SQLite 审计库。显式人工审批门与策略白名单是两个独立且都必须通过的安全条件。

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
