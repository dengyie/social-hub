# ADR-002：SQLite 即队列（BEGIN IMMEDIATE 抢占 + 租约），/metrics 手写文本暴露

- 状态：已接受（2026-09-13）
- 背景：设计文档 §6.1 要求持久队列语义但零外部依赖；可观测性要求 /metrics 但 M0 不想引入 prometheus-client。
- 决策：
  1. 队列直接落在 `automation_tasks` 表：认领用独立 `isolation_level=None` 引擎发 **BEGIN IMMEDIATE** 抢占（同库写者互斥），内联账号限频过滤（min_interval / per_day），原子置 `running` + `lease_expires_at`。
  2. worker 心跳续租（lease/3 间隔）；到期租约由调度器 `reclaim_expired` 回收 → 退避重排或终态失败，杜绝 worker 崩溃饿死队列。
  3. 幂等：`idem_key` 部分唯一索引（仅活跃状态唯一，终态可重排）+ API 层 `Idempotency-Key` 语义（同键活跃任务直接返回原任务）。
  4. /metrics 用 ~60 行手写注册表输出 Prometheus 文本格式；够用、零依赖，M4 接入采集器时再评估换库。
- 后果：单 worker 正确性已被测试覆盖（tests/test_queue.py）；多 worker 只需把 `SOCIAL_HUB_WORKERS>1`，不改表。
