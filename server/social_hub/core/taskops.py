"""任务操作：状态迁移（唯一入口，强制合法迁移表）+ 时间线追加写。"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.orm import Session

from . import metrics
from .state import ACTIVE, TERMINAL, can_transition, utcnow
from ..models import AutomationTask, TaskEvent


def add_event(session: Session, task_id: int, kind: str, data: dict | None = None) -> None:
    session.add(TaskEvent(task_id=task_id, kind=kind, data=json.dumps(data or {}, ensure_ascii=False)))


def transition(session: Session, task: AutomationTask, to: str, data: dict | None = None, **sets) -> None:
    if not can_transition(task.status, to):
        raise ValueError(f"illegal transition {task.status} -> {to} (task #{task.id})")
    task.status = to
    for k, v in sets.items():
        setattr(task, k, v)
    if to in TERMINAL:
        task.finished_at = utcnow()
        task.lease_expires_at = None
    add_event(session, task.id, to, data)


def backoff_delay(retries: int) -> int:
    """瞬时错误退避：30s * 2^n，封顶 30 分钟。"""
    return min(30 * (2**retries), 1800)


def requeue_transient(session: Session, task: AutomationTask, max_retries: int, error: str) -> bool:
    """瞬时错误退避重排；超过重试上限则终态失败。返回是否重排。"""
    if task.retries + 1 > max_retries:
        transition(session, task, "failed", {"error": error, "error_class": "TransientExhausted"},
                   error=error, error_class="TransientExhausted")
        return False
    task.retries += 1
    delay = backoff_delay(task.retries)
    transition(session, task, "queued",
               {"error": error, "retry": task.retries, "next_attempt_in_s": delay},
               error=error, error_class="TransientError")
    task.scheduled_at = utcnow() + timedelta(seconds=delay)
    return True


def task_metrics(task: AutomationTask, status: str) -> None:
    metrics.inc_counter("shub_tasks_total", {"platform": task.platform, "action": task.action_type, "status": status},
                        help_="Automation tasks by platform/action/status")


def idem_key(action: str, platform: str, account_id: int, payload_digest: str) -> str:
    import hashlib

    raw = f"{action}|{platform}|{account_id}|{payload_digest}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def enqueue_publish(session: Session, draft_id: int, platform: str, account_alias: str,
                    scheduled_at=None, preview: bool = False) -> AutomationTask:
    """草稿变体 × 账号 → 发布任务（幂等：活跃任务存在则返回原任务）。

    preview=True：只填表不点发布（CDP；编排器读 payload.preview）。
    未校准平台（adapter.calibrate_only）允许显式入队，但 fan-out 会跳过。
    """
    import json as _json

    from sqlalchemy import select

    from ..adapters.registry import get_adapter
    from ..models import AutomationTask, DraftVariant
    from ..vault.service import get_account

    get_adapter(platform)  # 未知平台早失败
    variant = session.execute(
        select(DraftVariant).where(DraftVariant.draft_id == draft_id, DraftVariant.platform == platform)
    ).scalar_one_or_none()
    if variant is None:
        raise ValueError(f"draft #{draft_id} has no variant for platform '{platform}'")
    if variant.status != "ready":
        raise ValueError(f"variant #{variant.id} not ready")
    account = get_account(session, platform, account_alias)
    if account is None:
        raise ValueError(f"account {platform}:{account_alias} not found")
    if account.status != "active":
        raise ValueError(f"account {platform}:{account_alias} is {account.status}")
    key = idem_key("publish", platform, account.id, f"variant:{variant.id}")
    # 只查活跃任务：命中 ux_active_idem 部分唯一索引，至多一行；
    # 终态任务随历史无限增长，全量捞取会让入队线性变慢
    existing = session.execute(
        select(AutomationTask).where(AutomationTask.idem_key == key, AutomationTask.status.in_(ACTIVE))
    ).scalar_one_or_none()
    if existing is not None:
        if preview and existing.status == "queued":
            data = _json.loads(existing.payload or "{}")
            data["preview"] = True
            existing.payload = _json.dumps(data, ensure_ascii=False)
        return existing  # 幂等命中
    payload = {"variant_id": variant.id}
    if preview:
        payload["preview"] = True
    task = AutomationTask(
        action_type="publish",
        payload=_json.dumps(payload, ensure_ascii=False),
        idem_key=key,
        account_id=account.id,
        platform=platform,
        lane=account.lane,
        scheduled_at=scheduled_at,
    )
    session.add(task)
    session.flush()
    add_event(session, task.id, "queued", {"draft_id": draft_id, "variant_id": variant.id})
    return task


def enqueue_fanout(session: Session, draft_id: int, scheduled_at=None) -> tuple[list[AutomationTask], list[dict]]:
    """一键全平台扇出（设计文档 §8.4）：草稿的每个 ready 变体 × 该平台 active 账号入队。

    单平台无账号 → 记入 skipped，不拖垮整波；幂等：变体已有活跃任务则复用。
    返回 (tasks, skipped)，skipped = [{"platform": ..., "reason": ...}]。
    """
    from sqlalchemy import select

    from ..adapters.registry import get_adapter
    from ..models import Account, DraftVariant

    variants = list(session.execute(
        select(DraftVariant).where(DraftVariant.draft_id == draft_id, DraftVariant.status == "ready")
    ).scalars())
    if not variants:
        raise ValueError(f"draft #{draft_id} has no ready variants")
    tasks: list[AutomationTask] = []
    skipped: list[dict] = []
    for v in variants:
        account = session.execute(
            select(Account).where(Account.platform == v.platform, Account.status == "active")
            .order_by(Account.id)
        ).scalars().first()
        if account is None:
            skipped.append({"platform": v.platform, "reason": "no active account"})
            continue
        try:
            adapter = get_adapter(v.platform)
        except ValueError as e:
            skipped.append({"platform": v.platform, "reason": str(e)})
            continue
        if getattr(adapter, "calibrate_only", False):
            skipped.append({"platform": v.platform,
                            "reason": "calibrate-only (explicit `shub publish` required)"})
            continue
        try:
            tasks.append(enqueue_publish(session, draft_id, v.platform, account.alias, scheduled_at))
        except ValueError as e:
            skipped.append({"platform": v.platform, "reason": str(e)})
    return tasks, skipped


def requeue_task(session: Session, task_id: int) -> AutomationTask:
    """人工重排（needs_login/failed/captcha_wait → queued），清空终态字段。"""
    task = session.get(AutomationTask, task_id)
    if task is None:
        raise ValueError(f"task #{task_id} not found")
    if task.status == "queued":
        return task
    transition(session, task, "queued", {"requeued_by": "manual"},
               error=None, error_class=None, finished_at=None, result_ref=None)
    task.retries = 0
    task.scheduled_at = None
    return task
