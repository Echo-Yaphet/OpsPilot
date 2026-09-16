# Stage 11：verification-policy 外部 workload identity

评测日期：2026-09-16

正式批次：`stage11-policy-identity-r2-20260916`

## 结论

verification-policy peer fan-out 与 one-shot rollout controller 已移除本地共享 HMAC 身份。Control API 和 controller 现在分别使用自己的 RSA proof private key，向独立 workload identity issuer 领取最长 10 秒、绑定 audience、subject、`GET`、peer-status 路径、读取操作、目标节点和一次性 `jti` 的 RS256 credential；peer 节点只持 issuer public key 并在返回状态前原子消费 `jti`。

正式批次使用 controller 身份完成 canary→stable→2/2 quorum，primary/canary 均接受 revision `2026091602` 和同一 digest，rollout reporter 返回 `converged`。旧 HS256 peer credential、缺失身份、错误 target 和 credential replay 均返回 401；issuer proof nonce replay 返回 401，未知 peer target 在签发前返回 403。

这证明本机 Compose 中独立 issuer、工作负载 proof key 和 peer RS256 验证链路，不代表云平台原生身份联邦、跨主机 issuer HA、生产密钥托管或轮换验收。

## 正式批次结果

| 检查 | 结果 |
|---|---:|
| controller proof 签发 | HTTP 200 |
| issuer proof nonce 重放 | HTTP 401 |
| issuer 未知 peer target | HTTP 403 |
| peer 缺失凭证 | HTTP 401 |
| 旧共享 HMAC 凭证 | HTTP 401 |
| 外部 RS256 凭证首次使用 | HTTP 200 |
| 外部凭证重放 | HTTP 401 |
| 错误 target 凭证 | HTTP 401 |
| canary / quorum | 1/1 / 2/2 |
| 最终 rollout 状态 | `converged` |
| recommendation-only 执行/验证副作用 | 0 / 0 |

候选 digest 为 `sha256:73f87a67f99d04911e76ae00a367bc93002b1f69e6f9a47f23624e617e85bbf3`，计划 digest 为 `sha256:a08a555262d2b4736f790f7ff1d39388debe338565b7132d6c78293bd7b7ae09`。审计顺序仍为 `candidate_validated → canary_published → canary_accepted → stable_published → quorum_reached`。

## 身份与权限边界

- bootstrap 为 controller 生成独立 proof key pair；controller 只挂载自己的 private key，issuer 只挂载对应 public key。
- Control API 与 controller 无 issuer signing private key，也不再配置 policy peer shared secret。
- issuer 按 subject、audience 与 operation 的精确组合只允许 `control-api` 和 `verification-policy-rollout-controller` 领取 policy peer 只读凭证，并限制目标为已知 policy 节点。
- peer verifier 只信任 issuer public key，并再次限制 subject、请求路径、operation 和 target；一次性 `jti` 继续持久化防重放。
- 策略 bundle 自身仍使用既有预签名 HMAC 完整性格式；本阶段替换的是 peer/controller 传输身份，不改变候选签名、显式 rollout 批准、canary gate 或 quorum。
- 模型仍不能决定 target、策略批准、执行或 `verified`；HTTP API、`IncidentState`、`IncidentWorkflow.run()`、`OpsTools` 和 Dashboard 格式未改变。

## 验证与限制

- workload identity、policy distribution、controller 与配置聚焦测试 53 项通过；完整后端测试 187 项通过。
- 正式 rollout 达到 2/2 exact acceptance，strict signature 为 required/valid，recommendation-only probe 无执行或 Verification 副作用。
- 最终默认栈 smoke 通过健康、mTLS runtime log、Loki、Tempo 与 recommendation-only 检查；Prometheus 9 条规则有效，原有 runtime identity 矩阵保持 401/403/200/401/404。
- Redis 安全回归保持：只建议为 `recommendation_ready`，未审批为 `awaiting_approval`，显式批准后仅重启 Redis 并达到 `resolved / verified=true`。
- 原始只读证据位于 `work/policy-rollouts/stage11-policy-identity-r2-20260916/`，不纳入 Git。r1 是权限组合进一步收紧前的成功预演，也保留为本地只读证据。
- 当前 issuer、proof key volumes、distributor、controller 和两个 Control API 仍位于同一 Docker Desktop 主机；未覆盖 issuer 故障转移、跨主机网络分区、云 workload identity federation、HSM/KMS 或轮换期间连续性。
- 下一节点是在真实独立主机或托管 HA PostgreSQL 上重复网络分区、节点丢失和数据库 failover，并验证身份 issuer/信任材料在真实故障域中的可用性。
