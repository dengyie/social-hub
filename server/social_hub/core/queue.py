"""持久队列：SQLite 即队列，零外部依赖（ADR-002）。

认领 = BEGIN IMMEDIATE 抢占（同库写者互斥）→ 内联账号限频过滤 → 原子置 running + 租约。
到期未心跳的租约由 reclaim_expired 回收（退避重排或终态失败），防 worker 崩溃饿死队列。
verifying 与 running 同等回收：核验阶段崩溃后回 queued，续跑靠 evidence 闸门只核验、绝不重发。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ..models import AutomationTask
from .state import utcnow
from .taskops import add_event, requeue_transient, transition

CANDIDATE_SQL = text(
    """
    SELECT t.id, t.account_id, t.retries, a.rate_limit
    FROM automation_tasks t JOIN accounts a ON a.id = t.account_id
    WHERE t.status = 'queued'
      AND (t.scheduled_at IS NULL OR t.scheduled_at <= :now)
      AND (t.lease_expires_at IS NULL OR t.lease_expires_at <= :now)
      AND a.status = 'active'
      AND t.retries <= :max_retries
    ORDER BY t.created_at
    LIMIT :limit
    """
)

CLAIM_SQL = text(
    """
    UPDATE automation_tasks
    SET status='running', claimed_by=:worker, lease_expires_at=:lease,
        started_at=COALESCE(started_at, :now), updated_at=:now
    WHERE id=:tid AND status='queued'
    """
)

RATE_MIN_INTERVAL_SQL = text(
    """
    SELECT MAX(finished_at) FROM automation_tasks
    WHERE account_id=:aid AND status='done'
    """
)

RATE_PER_DAY_SQL = text(
    """
    SELECT COUNT(*) FROM automation_tasks
    WHERE account_id=:aid AND status='done' AND finished_at >= :day_start
    """
)


@contextmanager
def immediate_tx(engine: Engine):
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.exec_driver_sql("COMMIT")
        except Exception:
            conn.exec_driver_sql("ROLLBACK")
            raise


def _rate_ok(conn, account_id: int, rate_limit_raw: str, now) -> bool:
    rl = json.loads(rate_limit_raw or "{}")
    per_day = int(rl.get("per_day", 3))
    min_interval = int(rl.get("min_interval_min", 120))
    last_done = conn.execute(RATE_MIN_INTERVAL_SQL, {"aid": account_id}).scalar()
    if last_done is not None:
        if isinstance(last_done, str):  # 原生 SQL 返回 TEXT，需解析
            last_done = datetime.fromisoformat(last_done)
        if min_interval > 0 and now - last_done < timedelta(minutes=min_interval):
            return False
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    done_today = conn.execute(RATE_PER_DAY_SQL, {"aid": account_id, "day_start": day_start}).scalar()
    return not (per_day > 0 and done_today >= per_day)


def claim_next(
    claim_engine: Engine,
    session_factory: sessionmaker,
    worker_id: str,
    lease_seconds: int,
    max_retries: int = 3,
    scan_limit: int = 25,
) -> int | None:
    """认领一个到期任务，返回 task id（无任务/全被限频返回 None）。

    先走只读预检（同条件 LIMIT 1）：队列空时不再每个轮询周期都抢写锁，
    轮询是常驻行为，空队列占绝大多数时间。
    """
    now = utcnow()
    with claim_engine.connect() as conn:
        if conn.execute(CANDIDATE_SQL, {"now": now, "max_retries": max_retries, "limit": 1}).first() is None:
            return None
    claimed_id: int | None = None
    with immediate_tx(claim_engine) as conn:
        rows = conn.execute(
            CANDIDATE_SQL,
            {"now": now, "max_retries": max_retries, "limit": scan_limit},
        ).fetchall()
        for tid, account_id, _retries, rate_limit_raw in rows:
            if not _rate_ok(conn, account_id, rate_limit_raw, now):
                continue
            res = conn.execute(
                CLAIM_SQL,
                {"worker": worker_id, "lease": now + timedelta(seconds=lease_seconds),
                 "now": now, "tid": tid},
            )
            if res.rowcount == 1:
                claimed_id = tid
                break
    return claimed_id


def heartbeat(claim_engine: Engine, task_id: int, worker_id: str, lease_seconds: int) -> bool:
    now = utcnow()
    with immediate_tx(claim_engine) as conn:
        res = conn.execute(
            text(
                """
                UPDATE automation_tasks SET lease_expires_at=:lease, updated_at=:now
                WHERE id=:tid AND claimed_by=:worker AND status IN ('running','verifying')
                """
            ),
            {"lease": now + timedelta(seconds=lease_seconds), "now": now, "tid": task_id, "worker": worker_id},
        )
        return res.rowcount == 1


def reclaim_expired(
    claim_engine: Engine,
    session_factory: sessionmaker,
    max_retries: int = 3,
) -> int:
    """回收到期租约：退避重排或终态失败；写 task_events。"""
    now = utcnow()
    expired_ids: list[int] = []
    with immediate_tx(claim_engine) as conn:
        rows = conn.execute(
            text(
                """
                SELECT id FROM automation_tasks
                WHERE status IN ('running','verifying')
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= :now
                """
            ),
            {"now": now},
        ).fetchall()
        expired_ids = [r[0] for r in rows]
    if not expired_ids:
        return 0
    handled = 0
    with session_factory() as session:
        for tid in expired_ids:
            task = session.get(AutomationTask, tid)
            if task is None:
                continue
            # retries 由 requeue_transient 统一递增，这里不再手动 +1（避免双计）
            if not requeue_transient(session, task, max_retries, error="lease expired (worker lost?)"):
                pass  # 已转 failed
            else:
                task.claimed_by = None
                task.lease_expires_at = None
            add_event(session, tid, "lease_reclaimed", {"retries": task.retries})
            handled += 1
        session.commit()
    return handled
