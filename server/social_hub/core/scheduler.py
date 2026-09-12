"""后台调度：租约回收 + 队列深度指标 + /readyz 心跳源（APScheduler）。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select

from ..db import get_claim_engine, get_session_factory
from ..models import AutomationTask, WorkerHeartbeat
from .queue import reclaim_expired
from .state import ACTIVE, utcnow

log = logging.getLogger(__name__)


def reclaim_job(max_retries: int) -> None:
    n = reclaim_expired(get_claim_engine(), get_session_factory(), max_retries)
    if n:
        log.warning("reclaimed %d expired leases", n, extra={"event": "lease_reclaimed"})


def gauges_job() -> None:
    from . import metrics

    with get_session_factory()() as session:
        # 一条 GROUP BY 拿全量，避免每个活跃状态各发一条 COUNT
        rows = session.execute(
            select(AutomationTask.status, func.count())
            .where(AutomationTask.status.in_(ACTIVE))
            .group_by(AutomationTask.status)
        ).all()
        depth = sum(n for _s, n in rows)
        metrics.set_gauge("shub_queue_depth", float(depth), help_="Active (non-terminal) automation tasks")


def scheduler_beat() -> None:
    with get_session_factory()() as session:
        row = session.get(WorkerHeartbeat, "scheduler")
        if row is None:
            session.add(WorkerHeartbeat(name="scheduler", ok=True, detail="apscheduler"))
        else:
            row.at = utcnow()
            row.ok = True
        session.commit()


class HubScheduler:
    def __init__(self, max_retries: int = 3):
        self._sched = BackgroundScheduler(timezone="UTC")
        self._max_retries = max_retries

    def start(self) -> None:
        from datetime import datetime, timezone

        # next_run_time=now：首次立即执行（/readyz 心跳与租约回收不能等第一个周期）
        now = datetime.now(timezone.utc)
        self._sched.add_job(scheduler_beat, "interval", seconds=10, id="beat", max_instances=1,
                            next_run_time=now)
        self._sched.add_job(lambda: reclaim_job(self._max_retries), "interval", seconds=15,
                            id="reclaim", max_instances=1, next_run_time=now)
        self._sched.add_job(gauges_job, "interval", seconds=30, id="gauges", max_instances=1,
                            next_run_time=now)
        self._sched.start()
        log.info("scheduler started", extra={"event": "start"})

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def is_alive(self) -> bool:
        return self._sched.running
