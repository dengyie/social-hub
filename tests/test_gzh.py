"""公众号适配器：两段式幂等 / 错误映射 / verify（httpx MockTransport，不打真实 API）。"""

from __future__ import annotations

import json
from httpx import MockTransport, Response

import pytest

from social_hub.adapters.base import ActionContext, CredentialsError, PermanentError, TransientError
from social_hub.adapters.gzh.adapter import GzhAdapter
from social_hub.adapters.gzh.client import GzhClient

OK_URL = "https://mp.weixin.qq.com/s/abc"


def _handler(calls: list, *, publish_status: int = 0, token_err: int | None = None, draft_err: int | None = None):
    def h(request: "httpx.Request") -> Response:  # noqa: F821
        path = request.url.path
        calls.append(path)
        if path == "/cgi-bin/token":
            if token_err:
                return Response(200, json={"errcode": token_err, "errmsg": "invalid credential"})
            return Response(200, json={"access_token": "T", "expires_in": 7200})
        if path == "/cgi-bin/material/add_material":
            return Response(200, json={"media_id": "THUMB1", "url": "https://mmbiz/x.jpg"})
        if path == "/cgi-bin/draft/add":
            if draft_err:
                return Response(200, json={"errcode": draft_err, "errmsg": "rate limited"})
            return Response(200, json={"media_id": "DRAFT1"})
        if path == "/cgi-bin/freepublish/submit":
            return Response(200, json={"publish_id": "PUB1"})
        if path == "/cgi-bin/freepublish/get":
            return Response(200, json={
                "publish_status": publish_status,
                "article_detail": {"item": [{"idx": 1, "url": OK_URL}]},
            })
        return Response(404, json={"errcode": -2, "errmsg": "not found"})

    return h


@pytest.fixture()
def gzh_ctx(make_task):
    """真实 DB 账号/草稿/媒体 + gzh 任务上下文。"""
    out = make_task(platform="gzh", alias="main", title="测试文章",
                    creds={"app_id": "wx1", "app_secret": "s"}, cover=True)
    return out


def _adapter(monkeypatch, calls: list, **kw) -> GzhAdapter:
    a = GzhAdapter()
    a.poll_interval = 0
    a.poll_attempts = kw.pop("poll_attempts", 3)
    status = kw.pop("publish_status", 0)
    token_err = kw.pop("token_err", None)
    draft_err = kw.pop("draft_err", None)

    def _client(self, ctx):
        return GzhClient("wx1", "s", transport=MockTransport(
            _handler(calls, publish_status=status, token_err=token_err, draft_err=draft_err)))

    monkeypatch.setattr(GzhAdapter, "_client", _client)
    return a


def test_publish_verify_happy(env, gzh_ctx, monkeypatch):
    calls: list = []
    a = _adapter(monkeypatch, calls)
    ctx = gzh_ctx["ctx"]
    result = a.publish(ctx)
    assert result.submitted
    assert result.detail["publish_id"] == "PUB1"
    assert calls.count("/cgi-bin/draft/add") == 1

    ev = a.verify(ctx)
    assert ev.ok and ev.url == OK_URL


def test_resume_no_duplicate_draft(env, gzh_ctx, monkeypatch):
    """evidence 已有 draft_media_id/publish_id 时重跑：不重建草稿、不重复提交发布。"""
    calls: list = []
    a = _adapter(monkeypatch, calls)
    ctx = gzh_ctx["ctx"]
    a.publish(ctx)
    first = list(calls)
    a.publish(ctx)  # 模拟退避重试后重跑
    second = calls[len(first):]
    assert "/cgi-bin/draft/add" not in second
    assert "/cgi-bin/material/add_material" not in second
    assert "/cgi-bin/freepublish/submit" not in second


def test_missing_cover_rejected(env, make_task):
    out = make_task(platform="gzh", alias="main", title="无封面", cover=False,
                    creds={"app_id": "wx1", "app_secret": "s"})
    with pytest.raises(PermanentError):
        GzhAdapter().publish(out["ctx"])


def test_video_cover_rejected_for_image_thumb(env, make_task, monkeypatch):
    """review P1 根因回归（API 通道）：公众号 thumb_media_id 只收图片——
    视频封面必须在任何 API 调用前被拒绝（fail-fast，零副作用）。"""
    out = make_task(platform="gzh", alias="mainvid", title="视频封面", cover=True,
                    cover_kind="video", creds={"app_id": "wx1", "app_secret": "s"})
    calls: list = []
    a = _adapter(monkeypatch, calls)
    with pytest.raises(PermanentError, match="kind=image"):
        a.publish(out["ctx"])
    assert calls == [], "kind 校验失败前不得发起任何微信 API 调用"


def test_title_limit_enforced(env, make_task):
    out = make_task(platform="gzh", alias="main", title="标" * 65, cover=True,
                    creds={"app_id": "wx1", "app_secret": "s"})
    with pytest.raises(PermanentError):
        GzhAdapter().publish(out["ctx"])


def test_missing_credentials_is_credentials_error(env, make_task):
    out = make_task(platform="gzh", alias="nocreds", creds={})
    with pytest.raises(CredentialsError):
        GzhAdapter()._client(out["ctx"])


def test_check_login_maps_expired(env, make_task, monkeypatch):
    out = make_task(platform="gzh", alias="main2", creds={"app_id": "wx1", "app_secret": "bad"})
    calls: list = []
    a = _adapter(monkeypatch, calls, token_err=40001)
    assert a.check_login(out["ctx"]) == "expired"


def test_rate_limit_maps_transient(env, gzh_ctx, monkeypatch):
    calls: list = []
    a = _adapter(monkeypatch, calls, draft_err=45009)
    with pytest.raises(TransientError):
        a.publish(gzh_ctx["ctx"])


def test_publish_failed_status_maps_permanent(env, gzh_ctx, monkeypatch):
    calls: list = []
    a = _adapter(monkeypatch, calls, publish_status=3)
    with pytest.raises(PermanentError):
        a.publish(gzh_ctx["ctx"])


def test_still_publishing_maps_transient_on_verify(env, gzh_ctx, monkeypatch):
    calls: list = []
    a = _adapter(monkeypatch, calls, publish_status=0)
    ctx = gzh_ctx["ctx"]
    a.publish(ctx)
    calls.clear()
    a2 = _adapter(monkeypatch, calls, publish_status=1)
    a2.poll_attempts = 1
    with pytest.raises(TransientError):
        a2.verify(ctx)
