# PostgreSQL 故障快速失败与请求隔离评测

评测日期：2026-09-17

正式批次：`postgres-fast-fail-r4-20260917`

## 结论

Control API 的 PostgreSQL incident store、调查 journal 和 pgvector event memory 现在都具有显式可配置的建连、并发槽获取、SQL、锁、空闲事务和应用侧数据库调用超时。同步数据库操作移出异步 API 事件循环；每个进程最多允许 8 个并发数据库连接尝试，额外请求在 0.25 秒获取边界内失败关闭。

在本机 Docker 数据库网络隔离期间，经未隔离 canary 所在的共享 API 网络向 primary 同时发送 8 个 recommendation-only 写请求，8/8 均返回 HTTP 502；最慢请求为 0.094 秒，低于 3 秒目标。同期 primary `/health` 返回 HTTP 200、耗时 0.006 秒，canary 写入返回 HTTP 200。恢复网络后等待 16 秒再逐一查询，8 个超时 incident 仍全部为 HTTP 404。

随后 physical-streaming standby 追平并被显式提升，稳定数据库别名切换到新主库。两个既有 Control API 无需重建即可恢复写入与跨节点读取；4/4 个预期 incident 保留，`executions=0`、`verifications=0`。

Plan digest：`sha256:ff1209825ff8f1938ae404c7310404ea0e454b36741321fb14dd521e9409f4b7`

## 正式批次结果

| 检查 | 结果 |
|---|---:|
| 并发隔离写 | 8/8 HTTP 502 |
| 最慢隔离写失败延迟 | 0.094 秒（目标不超过 3 秒） |
| 隔离节点只读健康入口 | HTTP 200，0.006 秒 |
| 未隔离 canary 同期写入 | HTTP 200 |
| 恢复 16 秒后延迟写入 | 8/8 HTTP 404 |
| 网络恢复后 primary 重新加入 | HTTP 200 |
| standby 切换前追平 | 通过 |
| standby 提升后可写 | 通过 |
| 切换后双节点写入与跨节点读取 | 通过 |
| 范围内 incident 保留 | 4/4 |
| execution / verification 副作用 | 0 / 0 |

## 实现边界

- `DATABASE_CONNECT_TIMEOUT_SECONDS=1`
- `DATABASE_ACQUIRE_TIMEOUT_SECONDS=0.25`
- `DATABASE_REQUEST_TIMEOUT_SECONDS=2.5`
- `DATABASE_STATEMENT_TIMEOUT_MILLISECONDS=1500`
- `DATABASE_LOCK_TIMEOUT_MILLISECONDS=500`
- `DATABASE_IDLE_TRANSACTION_TIMEOUT_MILLISECONDS=2000`
- `DATABASE_MAX_CONCURRENCY=8`

应用侧硬截止时间会传播到数据库工作线程。即使底层 DNS 或 libpq 建连在调用返回后才结束，连接在执行任何 SQL 前还会再次检查截止时间并退出；已经开始的 SQL 仍受 PostgreSQL `statement_timeout` 和事务回滚约束。

前两个诊断批次从 Docker Desktop 宿主端口探测被隔离节点，均观察到 15 秒超时和宿主入口不可达。该现象来自容器数据库网络 detach 时 Docker Desktop 的端口转发变化，混入了入口 NAT 故障。正式 r4 批次改由 canary 经未受影响的共享 API 网络访问 primary，只隔离数据库链路；失败批次原始产物继续保留在被 Git 忽略的 `work/` 下，不作为通过证据。

## 声明限制

这是单台 Docker Desktop 主机、8 个并发故障请求和手工 standby 提升的有界观测，不是生产容量、跨主机 HA、自动选主、fencing、SLA、RPO、RTO 或零数据丢失证明。Stage 12 的真实独立主机或托管 HA PostgreSQL 批次仍为 pending。
