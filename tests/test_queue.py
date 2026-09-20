"""持久队列语义：认领/租约/心跳/回收/限频/幂等。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from social_hub.content.draft_service import create_draft
from social_hub.core.queue import claim_next, heartbeat, reclaim_expired
from social_hub.core.state import utcnow
from social_hub.core.taskops import enqueue_publish
from social_hub.db import get_claim_engine, get_session_factory, session
from social_hub.models import AutomationTask
from social_hub.vault.service import create_account, get_account


def _enqueue(platform: str = "mock", alias: str = "demo", title: str = "t", creds=None) -> int:
    with session() as s:
        if get_account(s, platform, alias) is None:
            create_account(s, platform, alias, creds or {"token": "x"})
        d = create_draft(s, title=title, platform=platform)
        t = enqueue_publish(s, d.id, platform, alias)
        s.commit()
        return t.id


def _status(tid: int) -> str:
    with session() as s:
        return s.get(AutomationTask, tid).status


def test_claim_sets_running_and_lease(env):
    tid = _enqueue()
    claimed = claim_next(get_claim_engine(), get_session_factory(), "w1", lease_seconds=60)
    assert claimed == tid
    with session() as s:
        t = s.get(AutomationTask, tid)
        assert t.status == "running"
        assert t.claimed_by == "w1"
        assert t.lease_expires_at > utcnow()


def test_claim_none_when_empty(env):
    assert claim_next(get_claim_engine(), get_session_factory(), "w", 60) is None


def test_claim_skips_scheduled_future(env):
    with session() as s:
        create_account(s, "mock", "demo", {})
        d = create_draft(s, title="later", platform="mock")
        t = enqueue_publish(s, d.id, "mock", "demo", scheduled_at=utcnow() + timedelta(hours=1))
        s.commit()
        tid = t.id
    assert claim_next(get_claim_engine(), get_session_factory(), "w", 60) is None
    assert _status(tid) == "queued"


def test_idempotent_enqueue(env):
    with session() as s:
        create_account(s, "mock", "demo", {})
        d = create_draft(s, title="x", platform="mock")
        t1 = enqueue_publish(s, d.id, "mock", "demo")
        t2 = enqueue_publish(s, d.id, "mock", "demo")
        s.commit()
        assert t1.id == t2.id


def test_partial_unique_index_blocks_duplicate_active(env):
    from social_hub.core.taskops import idem_key

    with session() as s:
        acc = create_account(s, "mock", "demo", {})
        key = idem_key("publish", "mock", acc.id, "variant:999")
        s.add(AutomationTask(action_type="publish", payload="{}", idem_key=key,
                             account_id=acc.id, platform="mock", lane="api", status="queued"))
        s.flush()
        s.add(AutomationTask(action_type="publish", payload="{}", idem_key=key,
                             account_id=acc.id, platform="mock", lane="api", status="queued"))
        with pytest.raises(Exception):
            s.flush()
        s.rollback()  # IntegrityError 后会话需回滚才能安全关闭


def test_min_interval_rate_limit(env):
    tid = _enqueue()
    claim_next(get_claim_engine(), get_session_factory(), "w", 60)
    with session() as s:  # 第一单完成
        t = s.get(AutomationTask, tid)
        t.status = "done"
        t.finished_at = utcnow()
        s.commit()
    tid2 = _enqueue(title="second")
    # 同账号默认 min_interval_min=120 → 第二单不可认领
    assert claim_next(get_claim_engine(), get_session_factory(), "w", 60) is None
    assert _status(tid2) == "queued"


def test_per_day_cap(env):
    with session() as s:
        create_account(s, "mock", "capped", {}, rate_limit={"per_day": 1, "min_interval_min": 0})
    for i in range(1):
        tid = _enqueue(alias="capped", title=f"n{i}")
        claim_next(get_claim_engine(), get_session_factory(), "w", 60)
        with session() as s:
            t = s.get(AutomationTask, tid)
            t.status = "done"
            t.finished_at = utcnow()
            s.commit()
    tid2 = _enqueue(alias="capped", title="over")
    assert claim_next(get_claim_engine(), get_session_factory(), "w", 60) is None
    assert _status(tid2) == "queued"


def test_heartbeat_renews_lease(env):
    tid = _enqueue()
    claim_next(get_claim_engine(), get_session_factory(), "w", 60)
    with session() as s:
        t = s.get(AutomationTask, tid)
        t.lease_expires_at = utcnow() + timedelta(seconds=1)
        s.commit()
    assert heartbeat(get_claim_engine(), tid, "w", 60) is True
    with session() as s:
        t = s.get(AutomationTask, tid)
        assert t.lease_expires_at > utcnow() + timedelta(seconds=30)


def test_reclaim_expired_requeues(env):
    tid = _enqueue()
    claim_next(get_claim_engine(), get_session_factory(), "w1", 60)
    with session() as s:  # 模拟 worker 失联
        t = s.get(AutomationTask, tid)
        t.lease_expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert reclaim_expired(get_claim_engine(), get_session_factory(), max_retries=3) == 1
    with session() as s:
        t = s.get(AutomationTask, tid)
        assert t.status == "queued"
        assert t.retries == 1
        assert t.claimed_by is None


def test_reclaim_expired_verifying_requeues_keeps_receipt(env):
    """P0 闭环：verifying 租约到期必须回收。否则核验阶段崩溃任务永久卡住，
    续跑闸门（publish_receipt）永远走不到。回收后证据保留，重跑只核验。"""
    tid = _enqueue()
    claim_next(get_claim_engine(), get_session_factory(), "w1", 60)
    with session() as s:
        t = s.get(AutomationTask, tid)
        t.status = "verifying"
        t.evidence = '{"publish_id":"pub-keep"}'
        t.lease_expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert reclaim_expired(get_claim_engine(), get_session_factory(), max_retries=3) == 1
    with session() as s:
        t = s.get(AutomationTask, tid)
        assert t.status == "queued"
        assert t.retries == 1
        assert t.claimed_by is None
        assert '"publish_id": "pub-keep"' in t.evidence or '"publish_id":"pub-keep"' in t.evidence


def test_reclaim_exhausted_fails(env):
    tid = _enqueue()
    claim_next(get_claim_engine(), get_session_factory(), "w1", 60)
    with session() as s:
        t = s.get(AutomationTask, tid)
        t.retries = 3
        t.lease_expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    reclaim_expired(get_claim_engine(), get_session_factory(), max_retries=3)
    with session() as s:
        t = s.get(AutomationTask, tid)
        assert t.status == "failed"
        assert t.error_class == "TransientExhausted"


def test_drain_claims_with_extended_lease(env, monkeypatch):
    """CLI 内联执行没有心跳线程：drain 必须用加长租约认领，
    否则执行中租约到期 → 常驻 daemon 回收 → 任务被重复执行（红线）。"""
    from social_hub.core import orchestrator as orch_mod

    tid = _enqueue()
    captured: dict = {}
    orig = orch_mod.claim_next

    def spy(engine, sf, worker, lease, *a, **kw):
        captured["lease"] = lease
        return orig(engine, sf, worker, lease, *a, **kw)

    monkeypatch.setattr(orch_mod, "claim_next", spy)
    orch = orch_mod.Orchestrator(worker_prefix="drain-test")
    orch.drain_until_terminal(tid, timeout=120)
    assert _status(tid) == "done"
    assert captured["lease"] >= env.lease_seconds + 120
