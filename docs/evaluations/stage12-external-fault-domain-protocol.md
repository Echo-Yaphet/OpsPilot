# Stage 12：真实外部故障域验收协议

状态：验收契约已实现，真实外部批次待运行。

## 目标与结论边界

Stage 12 用于真实独立主机/可用区和托管或独立运维的 HA PostgreSQL，不接受 Docker Desktop 同主机模拟作为正式结果。通过只表示下列枚举故障在该批拓扑与时间窗口内满足验收条件；它不自动证明零数据丢失、任意故障恢复、生产 SLA、RPO 或 RTO。

基础设施故障注入必须由外部平台完成。OpsPilot evaluator 只读取计划和观测、验证不变量、计算 plan/evidence digest，并生成不可覆盖的只读结果；它没有云平台、集群、数据库、target、策略批准、执行或 `verified` 权限。

## 前置拓扑

- 至少两个 Control API endpoint，分别位于不同真实 failure domain。
- PostgreSQL 为 `managed-ha` 或 `independently-operated-ha`，至少跨两个 failure domain，并由稳定 endpoint 提供连接。
- 至少两个 workload identity issuer 实例，分别位于不同 failure domain；Control API/controller 仍只持各自 proof private key，peer 仍只持 issuer public trust。
- 所有演练 incident 使用 `execute=false`、`approved=false`；模型不能选择 target、批准、执行或写入 `verified`。

计划和观测分别放在：

```text
work/external-fault-domain-input/<evaluation-id>/plan.json
work/external-fault-domain-input/<evaluation-id>/observation.json
```

两份输入严格拒绝未知字段。具体字段以 `ExternalFaultDomainPlan` 和 `ExternalFaultDomainObservation` 为准；可先运行聚焦测试验证 schema：

```bash
docker compose run --rm --no-deps \
  -v ./apps/control-api/opspilot:/app/opspilot:ro \
  -v ./tests:/app/tests:ro \
  control-api python -m pytest -q tests/test_external_fault_domain_evaluation.py
```

## 故障与采集顺序

1. 记录全部 Control API 健康与跨节点可见基线。
2. 隔离一个 Control API 到 PostgreSQL 的网络：隔离节点写入必须失败关闭，另一节点保持可写；恢复后必须确认失败 incident 仍为 404，并确认隔离节点重新读到 survivor 写入。
3. 丢失一个 Control API 主机/Pod：survivor 保持可写；replacement 必须在不同 failure domain 启动、恢复健康并读取 survivor 写入。
4. 触发真实 PostgreSQL failover：保留 provider event ID、开始/完成时间、旧/新 primary ID；稳定 endpoint 不变，两个 Control API 恢复 recommendation-only 写入并互相读取。
5. 丢失一个 issuer 实例：另一个 failure domain 的 issuer 必须继续签发；peer credential 首次使用为 200、重放为 401、错误 target 为 401、未知 target 签发为 403；replacement/恢复实例随后健康，trust bundle digest 保持计划值。
6. 查询数据库范围内 incident、execution 和 verification 行；incident 数必须精确等于计划值，execution/verification 必须均为 0。

## 原始证据要求

`observation.json` 必须引用下列四类原始证据，每个引用包含来源标识、采集时间和内容 SHA-256：

- `control-node`
- `database-provider`
- `identity-issuer`
- `database-audit`

原始 provider/API/SQL 输出保留在 `work/external-fault-domain-input/<evaluation-id>/raw/`，不提交 Git。`source_id` 必须是该目录下的安全相对路径；evaluator 会读取每个普通文件并重新计算 SHA-256，路径越界、符号链接、文件缺失或摘要不匹配都会 fail-closed。自报布尔值不能单独构成充分证据。

## 运行与产物

```bash
make external-fault-domain-evaluate \
  STAGE12_EVALUATION_ID=<evaluation-id>
```

系统 `make` 被 Xcode 许可阻挡时，执行 Makefile 中等价的 `docker compose run --rm --no-deps ...` 命令，不替用户接受系统许可。

输出位于 `work/external-fault-domain-evaluations/<evaluation-id>/`：

- `plan.json`
- `observation.json`
- `summary.json`
- `report.md`

所有文件设为只读，同名批次不可覆盖。只有全部检查通过时命令才返回 0；任何 topology、provider、节点恢复、数据库一致性、issuer/trust、身份防重放或零副作用条件失败都返回非零。

## 当前阻塞

本机只有 Docker Desktop，同一主机上的 Control API、PostgreSQL、issuer 和 trust volume 不能满足本协议。正式批次需要外部环境的 endpoint、failure-domain 标识、provider failover event 和原始审计证据；在这些输入到位前，Stage 12 不得标记为完成。
