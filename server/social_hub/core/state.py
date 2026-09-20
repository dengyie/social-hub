"""任务状态机：唯一合法迁移表 + 时间工具。"""

from __future__ import annotations

from datetime import datetime, timezone

# 终态
TERMINAL = {"done", "failed", "canceled"}
# 活跃（占幂等唯一索引）
ACTIVE = {"queued", "running", "verifying", "needs_login", "captcha_wait"}
# 需要人介入的可恢复态（不是失败，见设计文档 §8.3）
RECOVERABLE = {"needs_login", "captcha_wait"}

ALLOWED: dict[str, set[str]] = {
    "queued": {"running", "canceled"},
    "running": {"verifying", "done", "needs_login", "captcha_wait", "failed", "queued"},  # done=preview 只填不发；queued=瞬时错误退避重试
    "verifying": {"done", "failed", "queued"},
    "needs_login": {"queued", "canceled", "failed"},
    "captcha_wait": {"queued", "canceled", "failed"},
    "done": set(),
    "failed": {"queued"},  # 人工重排
    "canceled": set(),
}


class InvalidTransition(Exception):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in ALLOWED.get(src, set())


def utcnow() -> datetime:
    """SQLite 存 naive UTC。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
