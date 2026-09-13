"""测试公共夹具：隔离数据目录、注册内置+mock 适配器、任务/上下文工厂。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from social_hub import config as cfg
from social_hub import db as dbm
from social_hub.adapters import registry
from social_hub.adapters.base import ActionContext  # noqa: F401
from social_hub.adapters.mock import MockAdapter
from social_hub.core import metrics


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """隔离数据目录 + 重置全局单例/注册表。"""
    monkeypatch.setenv("SOCIAL_HUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOCIAL_HUB_LEASE_SECONDS", "60")
    cfg.reset_settings()
    dbm.reset_db_cache()
    metrics.reset()
    registry.PLATFORMS.clear()
    registry.load_builtin_adapters()
    registry.register(MockAdapter())
    dbm.init_db()
    yield cfg.get_settings()
    dbm.reset_db_cache()


@pytest.fixture()
def make_task(env):
    """工厂：建账号+媒体+草稿变体+任务（不入队执行），返回 ctx 与句柄。"""

    def _make(platform: str = "mock", alias: str = "demo", title: str = "hello social-hub",
             body: str = "<p>hi</p>", cover: bool = False, creds: dict | None = None,
             cdp_port: int | None = None):
        from social_hub.content.draft_service import create_draft
        from social_hub.core.taskops import enqueue_publish
        from social_hub.db import session
        from social_hub.media.service import ingest
        from social_hub.models import AutomationTask, DraftVariant
        from social_hub.vault.service import create_account, get_account

        with session() as s:
            create_account(s, platform, alias, creds or {"token": "demo"}, cdp_port=cdp_port)
            cover_id = None
            if cover:
                f = env.media_dir / "seed-cover.jpg"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"\xff\xd8fakejpg")
                cover_id = ingest(s, env.media_dir, f).id
            d = create_draft(s, title=title, body=body, platform=platform, cover_media_id=cover_id)
            task = enqueue_publish(s, d.id, platform, alias)
            s.commit()
            task_id = task.id

        with session() as s:
            acc = get_account(s, platform, alias)
            t = s.get(AutomationTask, task_id)
            v = s.get(DraftVariant, json.loads(t.payload)["variant_id"])
            ctx = ActionContext(task=t, account=acc, session_factory=dbm.get_session_factory(),
                                media_dir=env.media_dir)
            return {"ctx": ctx, "task_id": task_id, "draft_id": d.id, "variant_id": v.id,
                    "account": acc, "account_alias": alias}

    return _make
