# Stage 9：verification-policy controller 中断恢复

评测日期：2026-09-16

正式批次：`stage9-controller-resume-r1-20260916`

## 结论

外置 one-shot controller 在 canary 已精确接受、stable 尚未发布的安全边界被强制终止后，replacement 进程从同一份 fsync 审计和 rollout 计划恢复。中断检查点保持 canary revision `2026091601`、stable revision `2026091600`；恢复进程重新确认 canary，随后只发布一次 stable 并达到 2/2 quorum，最终 rollout reporter 返回 `converged`。

该结果只证明同主机 Docker Compose、持久 volume 未丢失和一个明确 crash point 下的进程恢复。它不代表 controller 高可用、跨主机 failover、分布式共识或生产 SLA。

## 恢复协议

- 每个候选绑定一个 rollout plan digest，覆盖候选 revision/digest、排序后的节点及 endpoint、canary 集合和 quorum。
- 每条阶段审计在追加后 flush + `fsync`，并携带同一个 plan digest。
- 重启时严格解析完整审计；损坏 JSON 或同一候选对应不同计划会 fail-closed。
- stable 尚未推进时，即使审计已有 `canary_accepted`，恢复进程仍重新读取节点状态并要求 canary 精确接受候选。
- stable 已是候选时跳过 canary 发布，直接等待 quorum；已有 `quorum_reached` 时核对 stable 后幂等返回，不再次访问节点。
- 测试专用 `--interrupt-after` 只在指定阶段已经持久化后终止进程，不跳过签名、批准、canary 或 quorum 条件。

## 正式批次结果

| 检查 | 结果 |
|---|---:|
| 预签名候选与显式批准 | 通过 |
| 注入中断位置 | `canary_accepted` 持久化后 |
| 中断退出码 | `75` |
| 中断时 canary / stable revision | `2026091601` / `2026091600` |
| 恢复时 canary 重复发布 | 0 |
| 恢复时 canary 精确重验 | 通过 |
| 最终 quorum | 2/2 |
| 最终 rollout 状态 | `converged` |
| recommendation-only 执行/验证副作用 | 0 / 0 |

完整阶段序列为：

`candidate_validated → canary_published → canary_accepted → rollout_resumed → canary_revalidated → stable_published → quorum_reached`

候选 digest 为 `sha256:73f87a67f99d04911e76ae00a367bc93002b1f69e6f9a47f23624e617e85bbf3`。`candidate_validated` 与 `canary_published` 均只出现一次。

## 回归与安全边界

- controller 聚焦测试 8 项通过，覆盖 canary 前后中断、stable 发布后恢复、完成后幂等重试、计划漂移和损坏审计拒绝。
- 完整后端测试 182 项通过。
- 默认模式恢复后的真实 Redis 回归保持三段边界：只建议不执行；请求执行但未批准停在 `awaiting_approval`；显式批准后只重启 Redis 并达到 `resolved / verified=true`。
- 最终 Compose、运行状态和 smoke 通过；runtime-log mTLS/Loki/Tempo 检查正常，Prometheus 9 条规则全部有效。
- 现有 HTTP API、`IncidentState`、`IncidentWorkflow.run()`、`OpsTools` 与 Dashboard 格式未改变。
- controller 仍不生成或签名策略，不决定 incident target，不批准、不执行，也不设置 `verified`。
- 策略签名、显式 rollout 批准、canary 精确接受和 quorum 仍是相互独立的确定性条件。

## 限制与下一节点

- 审计、rollout channel 和 controller 仍依赖同一主机上的持久 volume；未验证 volume 丢失或跨主机接管。
- peer/controller 身份仍使用本地共享 HMAC，不是外部 workload identity。
- 未覆盖节点双向网络分区、跨可用区延迟、PostgreSQL 主库切换或连接池耗尽。
- 下一节点优先验证跨主机/网络分区与 PostgreSQL failover，再接入外部 workload identity。

本地原始只读证据位于 `work/policy-rollouts/stage9-controller-resume-r1-20260916/`，不纳入 Git。
