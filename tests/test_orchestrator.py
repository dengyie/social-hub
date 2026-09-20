"""编排器回归：证据合并 / 续跑不重发 / 12h 登录缓存 / preview 短路。"""

from __future__ import annotations

import json
from datetime import timedelta

from social_hub.adapters.registry import get_adapter
from social_hub.core.orchestrator import (
    Orchestrator,
    SUBMITTED_EVIDENCE_KEYS,
    evidence_is_submitted,
    merge_evidence,
)
from social_hub.core.queue import heartbeat
from social_hub.core.state import utcnow
from social_hub.db import get_claim_engine, session
from social_hub.models import Account, AutomationTask
from social_hub.vault.service import LOGIN_CHECK_TTL, login_check_is_fresh, touch_login_check


def test_merge_evidence_never_replaces_receipt():
    """P0：CDP 回执在 adapter._commit_receipt，detail 是内层 dict——整段替换会丢掉闸门。"""
    existing = json.dumps({"publish_receipt": {"platform": "xhs", "images": 1}})
    detail = {"platform": "xhs", "images": 1, "note_url": None}  # PublishResult.detail 不含外层键
    merged = json.loads(merge_evidence(existing, detail))
    assert merged["publish_receipt"]["platform"] == "xhs"
    assert merged["images"] == 1


def test_evidence_is_submitted_covers_all_lanes():
    assert SUBMITTED_EVIDENCE_KEYS == ("publish_id", "publish_receipt", "bvid", "article_id")
    assert evidence_is_submitted({"publish_id": "PUB1"})
    assert evidence_is_submitted({"publish_receipt": {"platform": "xhs"}})
    assert evidence_is_submitted({"bvid": "BV1xx"})
    assert evidence_is_submitted({"article_id": "art-1"})
    assert not evidence_is_submitted({"preview": {"platform": "xhs"}})
    assert not evidence_is_submitted({})


def test_login_check_freshness_ttl():
    acc = Account(platform="mock", alias="x", lane="api")
    assert login_check_is_fresh(acc) is False
    now = utcnow()
    acc.last_login_check = now - timedelta(hours=11, minutes=59)
    assert login_check_is_fresh(acc, now=now) is True
    acc.last_login_check = now - LOGIN_CHECK_TTL
    assert login_check_is_fresh(acc, now=now) is False


def test_orchestrator_expired_cache_blocks_publish(env, make_task):
    """12h 内 login_state=expired → 不碰 publish，转 needs_login。"""
    out = make_task(platform="mock", alias="exp")
    with session() as s:
        acc = s.get(Account, out["account"].id)
        touch_login_check(s, acc, "expired")
        s.commit()
    Orchestrator(worker_prefix="t").run_once()
    with session() as s:
        t = s.get(AutomationTask, out["task_id"])
        assert t.status == "needs_login"
        assert "expired" in (t.error or "")


def test_orchestrator_fresh_ok_skips_recheck(env, make_task, monkeypatch):
    """缓存仍新鲜且 login_state=ok → 跳过 check_login，直接 publish。"""
    out = make_task(platform="mock", alias="okcache")
    adapter = get_adapter("mock")
    calls = {"n": 0}
    orig = adapter.check_login

    def counted(ctx):
        calls["n"] += 1
        return orig(ctx)

    monkeypatch.setattr(adapter, "check_login", counted)
    with session() as s:
        acc = s.get(Account, out["account"].id)
        touch_login_check(s, acc, "ok")
        s.commit()
    Orchestrator(worker_prefix="t").run_once()
    assert calls["n"] == 0
    with session() as s:
        t = s.get(AutomationTask, out["task_id"])
        assert t.status == "done"
        ev = json.loads(t.evidence or "{}")
        assert ev.get("publish_id")
        assert t.result_ref and "mock.example" in t.result_ref


def test_orchestrator_stale_cache_rechecks_login(env, make_task, monkeypatch):
    out = make_task(platform="mock", alias="stale")
    adapter = get_adapter("mock")
    calls = {"n": 0}
    orig = adapter.check_login

    def counted(ctx):
        calls["n"] += 1
        return orig(ctx)

    monkeypatch.setattr(adapter, "check_login", counted)
    with session() as s:
        acc = s.get(Account, out["account"].id)
        acc.login_state = "ok"
        acc.last_login_check = utcnow() - timedelta(hours=13)
        s.commit()
    Orchestrator(worker_prefix="t").run_once()
    assert calls["n"] == 1
    with session() as s:
        t = s.get(AutomationTask, out["task_id"])
        assert t.status == "done"


def test_orchestrator_preview_rejected_on_api_lane(env, make_task):
    out = make_task(platform="mock", alias="prev")
    with session() as s:
        t = s.get(AutomationTask, out["task_id"])
        payload = json.loads(t.payload)
        payload["preview"] = True
        t.payload = json.dumps(payload)
        s.commit()
    Orchestrator(worker_prefix="t").run_once()
    with session() as s:
        t = s.get(AutomationTask, out["task_id"])
        assert t.status == "failed"
        assert "preview" in (t.error or "")


def test_orchestrator_resume_after_verifying_reclaim_skips_publish(env, make_task, monkeypatch):
    """P0 闭环：verifying 租约回收 → queued → 再认领。已有 publish_id 只核验，不重跑 publish。"""
    out = make_task(platform="mock", alias="resume-v")
    adapter = get_adapter("mock")
    publishes = {"n": 0}
    orig = adapter.publish

    def counted(ctx):
        publishes["n"] += 1
        return orig(ctx)

    monkeypatch.setattr(adapter, "publish", counted)
    with session() as s:
        acc = s.get(Account, out["account"].id)
        touch_login_check(s, acc, "ok")
        t = s.get(AutomationTask, out["task_id"])
        t.status = "queued"
        t.evidence = json.dumps({"publish_id": "pub-keep"})
        t.claimed_by = None
        t.lease_expires_at = None
        s.commit()
    Orchestrator(worker_prefix="resume-v").run_once()
    assert publishes["n"] == 0
    with session() as s:
        t = s.get(AutomationTask, out["task_id"])
        assert t.status == "done"
        ev = json.loads(t.evidence or "{}")
        assert ev["publish_id"] == "pub-keep"
        assert t.result_ref and "mock.example" in t.result_ref


def test_heartbeat_renews_verifying_lease(env, make_task):
    """verifying 阶段长核验必须能续租（否则 daemon 心跳会漏掉、租约过期）。"""
    out = make_task(platform="mock", alias="hbv")
    from social_hub.core.queue import claim_next
    from social_hub.db import get_session_factory

    tid = claim_next(get_claim_engine(), get_session_factory(), "w", 60)
    assert tid == out["task_id"]
    with session() as s:
        t = s.get(AutomationTask, tid)
        t.status = "verifying"
        t.lease_expires_at = utcnow() + timedelta(seconds=1)
        s.commit()
    assert heartbeat(get_claim_engine(), tid, "w", 60) is True
    with session() as s:
        t = s.get(AutomationTask, tid)
        assert t.lease_expires_at > utcnow() + timedelta(seconds=30)
