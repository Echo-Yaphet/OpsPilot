# Stage 6：Redis+MySQL 组合故障重复评测

评测日期：2026-09-15

正式批次：`stage6-combined-r5-20260915`

## 结论

5 次真实 Docker Compose 组合故障试验全部完成且有效，5/5 达到完整恢复条件。每次试验都识别出 Redis+MySQL 组合根因，生成并通过 Redis→MySQL 两项策略审查，按相同顺序执行，并同时通过工作流 Verification 与独立服务、容器、依赖探针。

该结果只覆盖本地 Compose 中一个明确的 Redis+MySQL 拓扑。5/5 的 Wilson 95% 置信区间仍为 56.6%–100%，不能描述成生产 SLA，也不能证明任意组合故障都能恢复。

## 方法

- 每轮开始前恢复 Redis、MySQL 和 `payment-service` 的运行与健康基线。
- 依次停止 Redis 和 MySQL，并等待 Prometheus 同时观测两个 `dependency_up` 序列为 `0`；未达到故障准入条件的样本记为基础设施无效。
- 使用不带模型调查与解释的确定性 `IncidentWorkflow.run()`，并显式传入 `execute=true`、`approved=true`。
- 成功要求组合根因完全匹配、建议顺序为 Redis→MySQL、两项策略均允许、实际执行顺序为 Redis→MySQL、状态为 `resolved / verified=true`。
- 工作流结束后重新读取 `payment-service` 健康、两个目标容器状态及两个依赖指标；独立探针必须全部通过。
- 每轮结果实时追加并 `fsync` 到 checkpoint；最终 JSON、JSONL 和 Markdown 产物只读且同名批次不可覆盖。
- 工作流状态保存到共享 PostgreSQL，因此每轮的策略和执行审计均沿用现有持久化边界。

## 结果

| 指标 | 结果 |
|---|---:|
| 有效试验 / 基础设施无效 | 5 / 0 |
| 完整组合恢复 | 5/5（100%） |
| Wilson 95% CI | 56.6%–100% |
| 组合根因正确 | 5/5 |
| Redis→MySQL 建议 | 5/5 |
| 全计划策略审查通过 | 5/5 |
| Redis→MySQL 实际执行 | 5/5 |
| 工作流与独立联合验证 | 5/5 |
| 平均成功延迟 | 10.073 s |

五次成功延迟分别为 9.587、10.478、9.385、9.485 和 11.432 秒。这里的延迟从故障指标准入后开始，到工作流结束及独立探针完成为止，不包括基线清理与故障注入等待。

共享 PostgreSQL 中核对到 10 条策略决策记录：每个 incident 都按位置 `0=redis`、`1=mysql` 保存且 `allowed=true`；另有 5 条执行审计，命令与结果均保留 Redis→MySQL 顺序。

## 与 Stage 5 的关系

Stage 5 的 `stage5-v2-v3-isolated-r4` 是不可变历史基线，组合故障标签和 0/2 结果不作回写。Stage 6 使用独立 runner 和当前组合根因口径验证新增的确定性多目标闭环；它不是新的 v2/v3 模型或 Skill 对照实验，因此不能用来声称 v3 带来了恢复率提升。

## 安全边界

- 模型不选择目标或顺序，不批准、不执行，也不设置 `verified`。
- 显式人工批准、逐项执行策略、Gateway 身份、broker allowlist、actuator 隔离和 Verification 保持相互独立。
- runner 只调用现有类型化工具与工作流接口，不增加 shell 执行面，也不改变 `IncidentState`、HTTP API、`IncidentWorkflow.run()` 或 `OpsTools`。

## 可追溯证据

本地原始证据位于 `work/stage6-evaluations/stage6-combined-r5-20260915/`，包含 `plan.json`、`trials.jsonl`、`summary.json`、`report.md` 和逐轮 `checkpoint.jsonl`。该目录为本地评测证据，不纳入 Git。

- Plan digest：`sha256:46741301c4d7755ee6eb9c2f1f5eafdc72ab22ecf3417119d21da502c16ab9d9`
