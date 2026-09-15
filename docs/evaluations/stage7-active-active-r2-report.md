# Stage 7：Control API / PostgreSQL active-active 压测

评测日期：2026-09-15

正式批次：`stage7-active-active-r2-20260915`

## 结论

两个独立 Control API 进程连接同一个 PostgreSQL，在 24 并发下完成 364/364 个交叠请求，未出现 HTTP 失败、incident 分裂、跨节点不可见、state ID 错配、孤儿子记录或执行副作用。

该批次观测吞吐为 48.811 requests/s，写路径延迟 p50/p95/max 分别为 3.661/7.188/7.451 秒。这些数字只描述本机 Docker Compose、两个进程和本批请求形态，不能外推为生产容量或 SLA。

## 方法

- `control-api` 与 `control-api-canary` 使用独立进程和本地数据卷，但共同连接 `memory-db` 中的 Control API PostgreSQL schema。
- 200 个 recommendation-only analyze 请求携带唯一 incident ID，并在两个节点间交替分发。
- 64 次 Alertmanager firing webhook 使用同一个新 fingerprint 并发投递，检验首次创建竞争和幂等收敛。
- 100 个 incident 列表读取与写入交叠执行。
- 写入完成后，从相反节点读取全部 200 个唯一 incident，并从两个节点读取重复告警的 canonical incident，共 202/202 次跨节点可见性检查。
- 最后直接核对 PostgreSQL cardinality、`state_json.incident_id`、子表引用以及 approvals/executions/verifications 副作用。
- runner 只发送 `execute=false`、`approved=false`，不会授权或执行修复。

## 结果

| 指标 | 结果 |
|---|---:|
| HTTP 成功 | 364/364 |
| 唯一写入 | 200/200 |
| 同 fingerprint 并发投递 | 64/64 |
| fingerprint 收敛 | 1 条 canonical incident |
| 并发读取 | 100/100 |
| 跨节点可见性 | 202/202 |
| PostgreSQL 唯一 incident 行 | 200 |
| PostgreSQL 重复告警行 | 1 |
| state ID 错配 / 孤儿子记录 | 0 / 0 |
| 审批、执行或验证副作用 | 0 |
| 观察吞吐 | 48.811 requests/s |
| 写延迟 p50 / p95 / max | 3.661 / 7.188 / 7.451 s |

Plan digest：`sha256:42108859cd24beb28c9f290b90d887168056e0bd267f163dbdec9e1ed1874393`

## 实现与安全边界

- active-active Compose profile 让 canary 使用与 primary 相同的 PostgreSQL 和事件内存 DSN，同时保留独立进程与本地卷。
- Alertmanager 新 incident ID 由 fingerprint 确定性派生，使两个节点在工作流开始前绑定同一标识。
- PostgreSQL 保存路径用事务级 advisory lock 串行化同一 alert/incident 的快照与规范化子表替换，避免唯一键竞争和交叠删除/插入。
- 现有 HTTP API、`IncidentState`、`IncidentWorkflow.run()`、`OpsTools` 和 Dashboard 数据格式未改变。
- 模型仍不能决定 target、审批、执行或 `verified`；Alertmanager 与压测 runner 均保持 recommendation-only。

## 限制

- 两个节点和 PostgreSQL 位于同一台 Docker Desktop 主机，不覆盖网络分区、节点丢失、连接池耗尽、数据库 failover 或跨可用区延迟。
- 本批不执行修复，因此不评估共享 Gateway/issuer 下的并发批准执行；这些边界已有独立功能和安全验收，但不是本次容量结论。
- 结果证明枚举负载下的一致性与可重复入口，不构成生产 HA、容量或 SLA 声明。

本地原始只读证据位于 `work/active-active-evaluations/stage7-active-active-r2-20260915/`，不纳入 Git。
