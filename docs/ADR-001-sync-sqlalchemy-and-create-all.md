# ADR-001：M0 采用同步 SQLAlchemy + create_all，alembic 延迟到首次 schema 演化

- 状态：已接受（2026-09-13）
- 背景：设计文档技术栈写明 SQLAlchemy + alembic。M0 是全新库，暂无存量 schema 需要迁移。
- 决策：
  1. ORM/编排/队列全部用**同步** SQLAlchemy + FastAPI `def` 端点（线程池并行）。SQLite + BEGIN IMMEDIATE 抢占语义在同步驱动下最可控；async 驱动（aiosqlite）与 pysqlite 的 autocommit/isolation 行为叠加会增加队列正确性风险，M0 不值得。
  2. 建表用 `Base.metadata.create_all`；**首次需要改表时引入 alembic**（db.py 已预留 reset/init 边界，接入点清晰）。
- 后果：单机 SQLite 性能足够（写者为队列 worker 一个）；矩阵化迁移 PostgreSQL 时重新评估 async。
