# Stage 8：外部 verification-policy rollout/quorum controller

评测日期：2026-09-15

正式批次：`stage8-policy-rollout-r2-20260915`

## 结论

外置 one-shot controller 完成一次严格签名的 canary→stable→quorum rollout。`control-api-canary` 先精确接受 revision `2026091501` 与候选 digest，控制器随后才原子推进 stable；最终 primary 和 canary 2/2 接受同一 revision/digest，现有 rollout reporter 返回 `converged`。

控制器没有 HTTP 服务，不生成策略、不签署候选，也不参与 incident target、人工审批、执行或 `verified`。它要求调用方提供已签名 bundle 和显式 `approved`，并独立校验 key ID、HMAC、content digest、严格 policy schema、revision 回退与同 revision 冲突。

该结果只覆盖同一台 Docker Desktop 主机上的两个 Control API 进程、一个 distributor 和一个共享 rollout volume。它是有界的发布协调与 quorum gate，不是分布式共识、生产 HA 或跨故障域证明。

## 正式批次结果

| 检查 | 结果 |
|---|---:|
| 候选签名/digest/schema/revision 校验 | 通过 |
| canary 在 stable 发布前精确接受 | 1/1 |
| quorum | 2/2 |
| 最终 rollout 状态 | `converged` |
| primary/canary accepted revision | `2026091501` / `2026091501` |
| accepted digest | `sha256:73f87a67f99d04911e76ae00a367bc93002b1f69e6f9a47f23624e617e85bbf3` |
| 签名状态 | required / valid |
| pending 节点 | 0 |
| recommendation-only 执行/验证副作用 | 0 / 0 |

审计顺序为 `candidate_validated → canary_published → canary_accepted → stable_published → quorum_reached`。canary 从发布到确认接受约 2.09 秒；这只是当前本机轮询观察，不是延迟 SLO。

## 失败批次与 fail-closed 证据

首个批次 `stage8-policy-rollout-r1-20260915` 暴露了验收编排缺陷：运行 one-shot controller 时 Compose 重新创建 distributor，导致节点回退到旧缓存并拒绝未知 key ID。控制器在 canary gate 超时后停止，审计中只有 `candidate_validated` 与 `canary_published`；命名卷核对为 canary revision `2026091501`、stable revision `2026091500`，证明 canary 未确认时 stable 不会推进。

修复仅在验收脚本为 one-shot controller 增加 `--no-deps`，避免它改变正在被验证的 distributor。没有弱化节点签名校验、accepted-only cache、peer 身份或 quorum 条件。

## 安全边界

- 缺少显式 rollout 批准时，控制器在任何文件写入前拒绝。
- 候选必须由受信外部系统预先签名；控制器不持有“生成任意策略”的接口。
- canary 必须全部精确接受候选 revision/digest，才允许推进 stable。
- stable 使用临时文件、`fsync` 和原子替换；阶段审计逐条追加并 `fsync`。
- quorum 是显式整数且不能超过节点数；达到 quorum 可保留 pending 少数节点，不冒充全量收敛。
- peer 状态继续使用短期、请求/operation/target/`jti` 绑定且防重放的 credential。
- 现有公开 HTTP API、`IncidentState`、`IncidentWorkflow.run()`、`OpsTools` 与 Dashboard 格式未改变。

## 验证

- 新增/相关聚焦测试：13 项通过。
- 完整后端测试：179 项通过。
- 正式 rollout：canary 1/1、quorum 2/2、最终 `converged`。
- 默认模式恢复后，真实 Redis 路径保持 recommendation-only 无执行、缺少审批停在 `awaiting_approval`、显式批准后 `restarted redis` 并达到 `resolved / verified=true`。
- 受影响的 Control API primary/canary、policy distributor 与 rollout controller 镜像均已重建。
- 最终默认模式下 Compose 配置、完整 smoke、runtime-log mTLS/Loki/Tempo/依赖健康及 Prometheus 9 条告警规则均通过。

## 限制与下一节点

- 当前只有两个同主机节点；quorum=2 等价于全量接受，未覆盖少数节点离线时的真实部署推进。
- distributor、rollout volume 和 controller 没有跨主机冗余；peer 身份仍使用本地共享 HMAC。
- 未覆盖发布过程中的主机丢失、双向网络分区、跨可用区延迟、controller crash-resume 或 PostgreSQL failover。
- 下一节点应优先做跨主机/网络分区与 PostgreSQL failover 故障域验证，并把 peer/controller 身份接到外部 workload identity。

本地原始只读证据位于 `work/policy-rollouts/stage8-policy-rollout-r2-20260915/`；失败批次证据位于相邻的 `stage8-policy-rollout-r1-20260915/`，二者均不纳入 Git。
