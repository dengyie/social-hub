"""B站（biliup-rs 子进程封装）+ 掘金（Cookie API）+ fanout 扇出测试。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from social_hub.adapters.base import NeedsLoginError, PermanentError, TransientError
from social_hub.adapters.bili.client import BiliClient
from social_hub.adapters.registry import get_adapter
from social_hub.content.draft_service import create_draft
from social_hub.core.taskops import enqueue_fanout, enqueue_publish
from social_hub.db import session
from social_hub.vault.service import create_account
from social_hub.web.app import create_app


# ---- bili：输出解析 / 登录态 / 两段式防重传 ----
def test_bili_upload_parses_bv(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"\x00\x00fakevideo")
    client = BiliClient(tmp_path / "bili", runner=lambda argv, timeout: "投稿成功 BV1xx411c7mD 转码中")
    bv = client.upload(tmp_path / "v.mp4", "t", "d", ["a", "b"])
    assert bv == "BV1xx411c7mD"


def test_bili_upload_no_bv_is_transient(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"\x00\x00fakevideo")
    client = BiliClient(tmp_path / "bili", runner=lambda argv, timeout: "quota exceeded, no bv")
    with pytest.raises(TransientError):
        client.upload(tmp_path / "v.mp4", "t", "d", [])


def test_bili_publish_resume_never_reuploads(env, make_task, tmp_path, monkeypatch):
    adapter = get_adapter("bili")
    ctx = make_task(platform="bili", alias="main", body="视频简介")["ctx"]
    # 伪造断点二：bvid 已落盘
    ctx.task.evidence = json.dumps({"video_path": "/x/v.mp4", "bvid": "BV1xx411c7mD"})
    calls = {"n": 0}

    def runner(argv, timeout):
        calls["n"] += 1
        return "ok BV1re UPLOAD"

    monkeypatch.setattr(adapter, "_client", lambda: _stub_client(runner))
    result = adapter.publish(ctx)
    assert result.detail["bvid"] == "BV1xx411c7mD"
    assert calls["n"] == 0  # 断点续跑：绝不重传


def test_bili_publish_requires_login(env, make_task, tmp_path, monkeypatch):
    adapter = get_adapter("bili")
    ctx = make_task(platform="bili", alias="main2", body="desc")["ctx"]
    ctx.task.evidence = json.dumps({"video_path": "/x/v.mp4"})  # 越过视频检查，直达登录检查
    monkeypatch.setattr(adapter, "_client", lambda: _stub_client(None, cookies=False))
    with pytest.raises(NeedsLoginError):
        adapter.publish(ctx)


def test_bili_video_kind_guard(env, make_task):
    adapter = get_adapter("bili")
    ctx = make_task(platform="bili", alias="main3", cover=True, body="d")["ctx"]
    # cover 是 image kind → 必须拒绝（防把图片投成视频）
    with pytest.raises(PermanentError, match="kind=video"):
        adapter.publish(ctx)


class _stub_client:
    """BiliClient 替身：不跑子进程。cookies=True 模拟已登录。"""

    def __init__(self, runner=None, cookies=True):
        self._runner = runner
        self._cookies = cookies

    def check_login(self) -> str:
        return "ok" if self._cookies else "unknown"

    def upload(self, file_path, title, desc, tags):
        return self._runner([], 0)


# ---- juejin：errcode 分类 / 三段式证据 ----
def _jj_ctx(make_task, alias="jj", title="juejin 文章"):
    return make_task(platform="juejin", alias=alias, title=title, body="# md 正文",
                     creds={"cookie": "sessionid=abc"})["ctx"]


def test_juejin_publish_three_stage(env, make_task, monkeypatch):
    adapter = get_adapter("juejin")
    ctx = _jj_ctx(make_task)
    stages = {"draft": 0, "publish": 0}

    class FakeJJ:
        def create_draft(self, title, md):
            stages["draft"] += 1
            return f"draft-{stages['draft']}"

        def publish(self, draft_id):
            stages["publish"] += 1
            return "art-1"

        def detail(self, article_id):
            return {"article_id": article_id}

        def close(self):
            pass

    monkeypatch.setattr(adapter, "_client", lambda ctx: FakeJJ())
    result = adapter.publish(ctx)
    assert result.detail["article_id"] == "art-1"
    evidence = adapter.verify(ctx)
    assert evidence.url == "https://juejin.cn/post/art-1"
    # 重跑：draft/publish 都不重复（幂等）
    adapter.publish(ctx)
    assert stages == {"draft": 1, "publish": 1}


def test_juejin_cookie_error_maps_credentials(env):
    import httpx

    from social_hub.adapters.base import CredentialsError
    from social_hub.adapters.juejin.client import JuejinClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"err_no": "010161", "err_msg": "token 失效"})

    client = JuejinClient("cookie=x", transport=httpx.MockTransport(handler))
    with pytest.raises(CredentialsError):
        client.create_draft("t", "b")


# ---- fanout 一键全平台 ----
def test_fanout_skips_platforms_without_account(env):
    with session() as s:
        create_account(s, "mock", "only-mock", {})
        d = create_draft(s, title="扇出", platform="mock")
        # 再造一个无账号平台的变体
        from social_hub.models import DraftVariant

        s.add(DraftVariant(draft_id=d.id, platform="juejin", title="扇出", body=""))
        s.commit()
        did = d.id
    tasks, skipped = None, None
    with session() as s:
        tasks, skipped = enqueue_fanout(s, did)
        s.commit()
    assert len(tasks) == 1 and tasks[0].platform == "mock"
    assert skipped == [{"platform": "juejin", "reason": "no active account"}]


def test_fanout_api_endpoint(env):
    with TestClient(create_app()) as client:
        client.post("/api/v1/accounts", json={"platform": "mock", "alias": "a1", "vars": {"token": "x"}})
        did = client.post("/api/v1/drafts", json={"title": "api 扇出", "platform": "mock"}).json()["id"]
        r = client.post("/api/v1/publish/fanout", json={"draft_id": did})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["tasks"] and body["tasks"][0]["platform"] == "mock"
        assert body["skipped"] == []
        # 幂等：重复扇出复用任务
        r2 = client.post("/api/v1/publish/fanout", json={"draft_id": did})
        assert r2.json()["tasks"][0]["task_id"] == body["tasks"][0]["task_id"]


def test_enqueue_publish_idempotent_still_works(env):
    with session() as s:
        create_account(s, "mock", "demo", {})
        d = create_draft(s, title="dup", platform="mock")
        t1 = enqueue_publish(s, d.id, "mock", "demo")
        t2 = enqueue_publish(s, d.id, "mock", "demo")
        s.commit()
        assert t1.id == t2.id


def test_unknown_platform_value_error_not_keyerror(env):
    """review P2 回归：未知平台统一 ValueError（endpoint 400 语义），不是 KeyError。"""
    from social_hub.adapters.registry import get_adapter

    with pytest.raises(ValueError, match="unknown platform"):
        get_adapter("typo-platform")
    with session() as s:
        create_account(s, "mock", "demo", {})
        d = create_draft(s, title="t", platform="mock")
        with pytest.raises(ValueError, match="unknown platform"):
            enqueue_publish(s, d.id, "typo-platform", "demo")


def test_create_draft_rejects_unknown_platform(env):
    """入口即校验：typo 平台不能进库（否则发布时 500）。"""
    with session() as s:
        with pytest.raises(ValueError, match="unknown platform"):
            create_draft(s, title="t", platform="typo-platform")
