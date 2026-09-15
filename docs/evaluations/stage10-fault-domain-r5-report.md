# Stage 10：Control API 网络分区与 PostgreSQL failover 故障域演练

评测日期：2026-09-16

正式批次：`stage10-fault-domain-r5-20260916`

## 结论

两个 Control API 在独立的本机 Docker 数据库网络上连接一个稳定 PostgreSQL 端点。隔离 primary Control API 的数据库网络后，该节点的 recommendation-only 写入在 15 秒客户端边界内失败关闭，同期 canary 写入成功；网络恢复后 primary 无需重建即可读取 canary 写入的数据。

PostgreSQL physical-streaming standby 在切换前确认已回放全部范围内 incident。停止原 primary、提升 standby 并把稳定端点别名移到新主库后，两个 Control API 均无需重建即恢复写入，且新写入可从对端读取。最终 4/4 个预期 incident 保留，`executions=0`、`verifications=0`。

这是一台 Docker Desktop 主机上的网络命名空间与流复制演练，不是真实跨主机、跨可用区或托管 PostgreSQL HA 证明。切换由验收工具显式编排，不包含自动 leader election、fencing、旧主库重新加入或 RPO/RTO SLA。

## 正式批次结果

| 检查 | 结果 |
|---|---:|
| 切换前跨节点可见 | 通过 |
| 隔离节点写入 | 失败关闭（客户端状态 599，15 秒边界） |
| 分区失败请求在恢复后延迟落库 | 未发生（HTTP 404） |
| 未隔离 canary 同期写入 | HTTP 200 |
| 网络恢复后 primary 重新加入 | HTTP 200 |
| standby 切换前追平 | 通过 |
| standby 提升后可写 | 通过 |
| 切换后 primary / canary 写入 | HTTP 200 / HTTP 200 |
| 切换后跨节点可见 | 通过 |
| 范围内 incident 保留 | 4/4 |
| execution / verification 副作用 | 0 / 0 |

Plan digest：`sha256:a119c4fd382e42de489eff0f4607d57cb74a3bd3ef942ef2bc20b34a32ffda9e`

## 方法与故障边界

- 验收使用独立临时 primary/standby 数据卷，不修改默认 `memory-db` 数据卷。
- primary 允许专用 replication role；standby 通过 `pg_basebackup` 建立并持续流式回放 WAL。
- 两个 Control API 只在本批次中改用 `stage10-memory-db:5432`，现有公开 API、状态模型和本地数据卷保持不变。
- 分区只断开 primary Control API 与专用数据库网络；它仍可由宿主探测，canary 与数据库链路不受影响。
- 切换前先从 standby 只读核对范围内 incident，随后停止旧主库、提升 standby，并把稳定数据库别名显式移到新主库。
- 切换后不重建 Control API，以验证当前每次操作重新连接的存储实现能够解析新端点并继续工作。
- 所有 incident 请求均携带 `execute=false`、`approved=false`；模型调查与 repair 模式在演练拓扑中禁用。
- 成功或失败后都停止临时数据库、删除仅属于本批次的临时卷/网络，并强制恢复默认 Control API 到原 `memory-db`。

## 无效尝试

- r1 在 Control API 重建时遇到瞬时 TCP reset；等待逻辑未把该错误作为可重试启动状态，未进入业务断言。
- r2 暴露 `.env` 中启用 repair agent 与演练清空 LLM 配置的冲突；演练随后显式设置 `REPAIR_MODE=disabled`。
- r3 已通过分区、恢复与 standby 追平，但提升命令由容器默认 root 用户执行，被 PostgreSQL 正确拒绝；r4 改为 `postgres` 用户执行提升。
- r4 首次完整通过，但只证明客户端未收到成功，未单独排除网络恢复后的延迟落库；r5 增加失败 incident 恢复后仍为 404 的断言后作为正式批次。

r1-r3 均在产物生成前终止并由 finally 清理恢复默认栈，不计入正式结果。r4 作为被更严格证据取代的成功预演保留在本地原始证据中。

## 回归验证

- 新增聚焦报告测试 3 项；与 Stage 7 报告测试合跑共 7 项通过。
- 完整后端测试 185 项通过。
- 两个 Control API 镜像已重建，并在默认 `memory-db` 配置下强制重建容器。
- 最终 smoke 通过服务/依赖健康、runtime-log mTLS、Loki、Tempo 与 recommendation-only 检查。

## 安全边界与限制

- 模型仍不能决定 target、策略批准、执行或 `verified`。
- recommendation-only 请求没有 approval、execution 或 Verification 记录。
- PostgreSQL failover 没有改变 `IncidentState`、HTTP API、`IncidentWorkflow.run()`、`OpsTools` 或 Dashboard 数据格式。
- 本批没有并发写穿越主库故障瞬间，因此不提供零数据丢失结论。
- 599 表示客户端在 15 秒边界内未取得 HTTP 响应；它证明隔离节点没有误报成功，但当前同步数据库访问仍会占用请求直到连接超时。
- 仍需在真实独立主机/可用区、托管 HA PostgreSQL、自动故障检测/fencing、连接池耗尽和旧主库重新加入条件下验收。

本地原始只读证据位于 `work/fault-domain-evaluations/stage10-fault-domain-r5-20260916/`，不纳入 Git。
