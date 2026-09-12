"""编排器 worker：认领 → 执行动作 → 核验 → 状态迁移；租约心跳独立线程。

发布主链路（设计文档 §8.1）：running → (adapter.publish) → verifying → (adapter.verify) → done。
错误分类驱动状态机：Transient→退避重排 / Credentials+NeedsLogin→needs_login / Captcha→captcha_wait / 其余→failed。
daemon 崩溃后任务由租约回收接管，绝不自动重复发布（§6.1）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from ..adapters.base import (
    ActionContext,
    AdapterError,
    CaptchaWaitError,
    CredentialsError,
    NeedsLoginError,
    PermanentError,
    TransientError,
)
from ..adapters.registry import get_adapter
from ..config import get_settings
from ..db import get_claim_engine, get_session_factory
from ..models import Account, AutomationTask, WorkerHeartbeat
from . import metrics
from .queue import claim_next, heartbeat, reclaim_expired
from .state import TERMINAL, utcnow
from .taskops import add_event, requeue_transient, task_metrics, transition

log = logging.getLogger(__name__)

ERROR_STATE_MAP: list[tuple[type, str]] = [
    (TransientError, "queued"),
    (CredentialsError, "needs_login"),
    (NeedsLoginError, "needs_login"),
    (CaptchaWaitError, "captcha_wait"),
    (PermanentError, "failed"),
]


def _error_state(e: AdapterError) -> str:
    for cls, state in ERROR_STATE_MAP:
        if isinstance(e, cls):
            return state
    return "failed"


class Orchestrator:
    def __init__(self, worker_prefix: str = "worker"):
        self.settings = get_settings()
        self.worker_id = f"{worker_prefix}-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- 生命周期 ----
    def start(self, workers: int | None = None) -> None:
        n = workers or self.settings.workers
        for i in range(n):
            t = threading.Thread(target=self._loop, name=f"{self.worker_id}-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        hb = threading.Thread(target=self._heartbeat_loop, name=f"{self.worker_id}-hb", daemon=True)
        hb.start()
        self._threads.append(hb)
        log.info("orchestrator started", extra={"worker": self.worker_id, "event": "start"})

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def _beat(self) -> None:
        with get_session_factory()() as session:
            row = session.get(WorkerHeartbeat, self.worker_id)
            if row is None:
                session.add(WorkerHeartbeat(name=self.worker_id, ok=True, detail="orchestrator"))
            else:
                row.at = utcnow()
                row.ok = True
            session.commit()

    def _heartbeat_loop(self) -> None:
        interval = max(self.settings.lease_seconds / 3, 5)
        while not self._stop.is_set():
            try:
                self._beat()
                self._renew_leases()
            except Exception:
                log.exception("heartbeat failed", extra={"worker": self.worker_id})
            self._stop.wait(interval)

    def _renew_leases(self) -> None:
        """为本 worker 名下 running 任务逐个续租（长任务防租约到期被回收）。"""
        from sqlalchemy import select

        with get_session_factory()() as session:
            ids = list(session.execute(
                select(AutomationTask.id).where(
                    AutomationTask.claimed_by == self.worker_id,
                    AutomationTask.status == "running",
                )
            ).scalars())
        ce = get_claim_engine()
        for tid in ids:
            heartbeat(ce, tid, self.worker_id, self.settings.lease_seconds)

    # ---- 主循环 ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("run_once crashed", extra={"worker": self.worker_id})
                worked = False
            if not worked:
                self._stop.wait(0.5)

    def run_once(self) -> bool:
        """认领并执行一个任务；无任务返回 False。"""
        task_id = claim_next(
            get_claim_engine(),
            get_session_factory(),
            self.worker_id,
            self.settings.lease_seconds,
            self.settings.max_retries,
        )
        if task_id is None:
            return False
        self._execute(task_id)
        return True

    # ---- 执行 ----
    def _execute(self, task_id: int) -> None:
        sf = get_session_factory()
        adapter = None
        with sf() as session:
            task = session.get(AutomationTask, task_id)
            if task is None or task.status != "running":
                return
            account = session.get(Account, task.account_id)
            ctx = ActionContext(task=task, account=account, session_factory=sf,
                                media_dir=self.settings.media_dir, session=session)
            log.info("task running", extra={"task_id": task.id, "platform": task.platform, "worker": self.worker_id})
            try:
                adapter = get_adapter(task.platform)
                prior = json.loads(task.evidence or "{}")
                already_submitted = bool(prior.get("publish_id"))
                if task.action_type == "publish" and already_submitted:
                    # 断点续跑：上次运行已提交发布（verifying 阶段中断）——绝不重复 publish，直接核验
                    evidence = adapter.verify(ctx)
                    merged = json.loads(task.evidence or "{}")
                    merged["verify"] = evidence.raw
                    add_event(session, task_id, "verified", {"url": evidence.url, "resumed": True})
                    transition(session, task, "done", {"url": evidence.url},
                               result_ref=evidence.url, evidence=json.dumps(merged, ensure_ascii=False))
                    task_metrics(task, "done")
                    session.commit()
                    log.info("task done (resumed)", extra={"task_id": task_id, "platform": task.platform,
                                                           "event": "done", "worker": self.worker_id})
                elif task.action_type == "publish":
                    result = adapter.publish(ctx)
                    transition(session, task, "verifying", {"detail": result.detail},
                               evidence=json.dumps(result.detail, ensure_ascii=False))
                    task_metrics(task, "verifying")
                    session.commit()

                    evidence = adapter.verify(ctx)
                    fresh = session.get(AutomationTask, task_id)
                    if fresh.status == "verifying":
                        merged = json.loads(fresh.evidence or "{}")
                        merged["verify"] = evidence.raw
                        add_event(session, task_id, "verified", {"url": evidence.url})
                        transition(session, fresh, "done", {"url": evidence.url},
                                   result_ref=evidence.url, evidence=json.dumps(merged, ensure_ascii=False))
                        task_metrics(fresh, "done")
                        session.commit()
                        log.info("task done", extra={"task_id": task_id, "platform": task.platform,
                                                     "event": "done", "worker": self.worker_id})
                else:
                    handler = getattr(adapter, task.action_type, None)
                    if handler is None:
                        raise PermanentError(f"adapter {task.platform} has no action '{task.action_type}'")
                    out = handler(ctx) or {}
                    transition(session, task, "done", out, result_ref=out.get("result_ref"))
                    task_metrics(task, "done")
                    session.commit()
            except AdapterError as e:
                session.rollback()
                self._handle_adapter_error(sf, task_id, e)
            except Exception as e:  # 未分类异常按瞬时处理（退避重排）
                log.exception("task crashed", extra={"task_id": task_id, "worker": self.worker_id})
                session.rollback()
                self._handle_adapter_error(sf, task_id, TransientError(f"unclassified: {e}"))

    def _handle_adapter_error(self, sf, task_id: int, e: AdapterError) -> None:
        state = _error_state(e)
        with sf() as session:
            task = session.get(AutomationTask, task_id)
            if task is None or task.status not in ("running", "verifying"):
                return
            if state == "queued":
                requeued = requeue_transient(session, task, self.settings.max_retries, error=str(e))
                task_metrics(task, "requeued" if requeued else "failed")
            else:
                transition(session, task, state, {"error": str(e)}, error=str(e),
                           error_class=type(e).__name__)
                task_metrics(task, state)
            session.commit()
        log.warning("task error", extra={"task_id": task_id, "event": state, "platform": task.platform,
                                         "worker": self.worker_id})

    # ---- 便捷入口 ----
    def drain_until_terminal(self, task_id: int, timeout: float = 120.0, stall_after: float = 20.0) -> str:
        """CLI --wait：持续 run_once 直到目标任务终态（M0 单机串行足够）。

        任务长时间停在 queued（限频窗口/无 worker）→ stall_after 秒后快速失败，不空转。
        """
        deadline = time.monotonic() + timeout
        last_status, last_change = None, time.monotonic()
        while time.monotonic() < deadline:
            with get_session_factory()() as session:
                task = session.get(AutomationTask, task_id)
                if task is None:
                    raise ValueError(f"task #{task_id} not found")
                status = task.status
                if status in TERMINAL:
                    return status
            if status != last_status:
                last_status, last_change = status, time.monotonic()
            elif time.monotonic() - last_change > stall_after:
                raise TimeoutError(f"task #{task_id} stuck in '{status}' for {stall_after}s "
                                   f"(rate-limited account or no worker?)")
            self.run_once()
            time.sleep(0.2)
        raise TimeoutError(f"task #{task_id} not terminal within {timeout}s")
