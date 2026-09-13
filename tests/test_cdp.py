"""CDP 平台测试：舰队（注入假件）、拦截守卫、fixture 回放 flow、契约套件全适配器。

选择器真机校准前的行为锁：flow 顺序、上传/填充参数、拦截转状态、断点续跑。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from social_hub.adapters.base import (
    CaptchaWaitError,
    NeedsLoginError,
    PermanentError,
)
from social_hub.adapters.contract import check_metadata, check_unsupported_actions
from social_hub.adapters.registry import get_adapter, registered_platforms
from social_hub.core import fleet as fleet_mod
from social_hub.core.fleet import ChromeFleet

from fake_cdp import FakeBrowser

# ---- fixture HTML（与各平台 selectors 注册表一一对应；不含登录/验证码标记）----
XHS_HTML = """
<html><body><div class="channel-tab"></div>
<div id="title-textarea"><input/></div>
<div class="ql-editor" contenteditable="true"></div>
<input type="file" id="upload"/>
<button>发 布</button></body></html>
"""
ZHIHU_HTML = """
<html><body>
<textarea placeholder="请输入标题（最多 100 个字）"></textarea>
<div class="public-DraftEditor-content" contenteditable="true"></div>
<button>发布</button></body></html>
"""
DOUYIN_HTML = """
<html><body>
<input type="file" id="media"/>
<div class="notranslate" contenteditable="true"></div>
<div class="zone-container" contenteditable="true"></div>
<button>发 布</button></body></html>
"""
CHANNELS_HTML = """
<html><body>
<input type="file" id="file"/>
<div id="post-content"><textarea></textarea></div>
<button>发 表</button></body></html>
"""
LOGIN_HTML = '<html><body><div class="login-container"></div></body></html>'
CAPTCHA_HTML = '<html><body><div class="captcha-slider"></div></body></html>'
# —— 参考项目真机选择器对应 fixture ——
XHS_REAL_HTML = """
<html><body>
<div class="creator-tab">上传图文 上传视频</div>
<div class="upload-area"><input class="upload-input" type="file"/></div>
<div class="d-input"><input/></div>
<div class="tiptap ProseMirror" contenteditable="true"></div>
<div class="publish-page-publish-btn"><button class="bg-red">发 布</button></div>
</body></html>
"""
DOUYIN_LOGIN_HTML = """
<html><body><div class="web-login"><span>扫码登录</span><span>手机号登录</span></div></body></html>
"""
KS_HTML = """
<html><body>
<button class="_upload-btn-abc">上传视频</button>
<input type="file" id="f"/>
<div contenteditable="true"></div>
<button>发布</button><button>确 认</button>
</body></html>
"""
BJH_HTML = """
<html><body>
<input type="file" accept="video/mp4"/>
<div class="contentEditable-x">标题占位</div>
<button>发布</button>
</body></html>
"""
TT_HTML = """
<html><body>
<input placeholder="请输入标题（最多 30 字）"/>
<div class="ProseMirror" contenteditable="true"></div>
<input type="file"/>
<button>发 布</button>
</body></html>
"""
CSDN_HTML = """
<html><body>
<input placeholder="标题"/>
<div class="cm-content" contenteditable="true"></div>
<button>发布博客</button>
</body></html>
"""


@pytest.fixture()
def cdp_env(env, monkeypatch):
    """env + 舰队假件：connector 按记录返回带 fixture HTML 的 FakeBrowser。"""
    calls: dict = {"launched": [], "connected": []}

    def connector(url: str) -> FakeBrowser:
        calls["connected"].append(url)
        return FakeBrowser(calls["html"])

    def prober(port: int) -> dict | None:
        return {"Browser": "fake"} if port >= 9300 else None

    def launcher(argv) -> None:
        calls["launched"].append(argv)

    fleet_mod.reset_fleet()
    fleet_mod._fleet = ChromeFleet(connector=connector, prober=prober, launcher=launcher)
    fleet_mod._fleet.calls = calls
    yield fleet_mod._fleet
    fleet_mod.reset_fleet()


def _cdp_ctx(make_task, platform: str, html: str, alias: str = "main", cover: bool = True,
             title: str = "hello social-hub", port_offset: int | None = None):
    if port_offset is None:
        port_offset = {"xhs": 0, "zhihu": 1, "douyin": 2, "channels": 3}.get(platform, 99)
    make_task(platform=platform, alias=alias, cover=cover, title=title, cdp_port=9300 + port_offset)
    from social_hub.config import get_settings
    from social_hub.db import get_session_factory
    from social_hub.vault.service import get_account

    with get_session_factory()() as s:
        acc = get_account(s, platform, alias)
        from social_hub.models import AutomationTask

        task = s.query(AutomationTask).order_by(AutomationTask.id.desc()).first()
        from social_hub.adapters.base import ActionContext

        return ActionContext(task=task, account=acc, session_factory=get_session_factory(),
                             media_dir=get_settings().media_dir)


def test_fleet_launches_when_port_down_and_never_kills():
    seen: dict = {}
    calls = {"argv": [], "launched": False}

    def prober(port: int):
        return {"ok": 1} if calls["launched"] else None

    def launcher(argv) -> None:
        calls["argv"].append(argv)
        calls["launched"] = True

    fleet = ChromeFleet(
        connector=lambda url: SimpleNamespace(
            contexts=[], new_context=lambda: None,
            disconnect=lambda: seen.setdefault("disconnected", True)),
        prober=prober, launcher=launcher,
    )
    account = SimpleNamespace(cdp_port=9301, chrome_profile="/tmp/prof-x", platform="xhs", alias="main")
    handle = fleet.ensure(account)
    assert calls["argv"], "port down → 必须拉起 chrome"
    argv = calls["argv"][0]
    assert "--remote-debugging-port=9301" in argv and "--user-data-dir=/tmp/prof-x" in argv
    handle.close()
    assert seen.get("disconnected")


def test_fleet_refuses_port_below_9300():
    fleet = ChromeFleet(prober=lambda port: {"ok": 1})
    account = SimpleNamespace(cdp_port=9222, chrome_profile=None, platform="xhs", alias="main")
    with pytest.raises(ValueError, match="9222"):
        fleet.ensure(account)


def test_cdp_guard_blocks_login_and_captcha(make_task, cdp_env):
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = LOGIN_HTML
    ctx = _cdp_ctx(make_task, "xhs", LOGIN_HTML)
    with pytest.raises(NeedsLoginError):
        adapter.publish(ctx)
    cdp_env.calls["html"] = CAPTCHA_HTML
    with pytest.raises(CaptchaWaitError):
        adapter.publish(ctx)


def test_xhs_publish_flow_replay(make_task, cdp_env):
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, cover=True)
    result = adapter.publish(ctx)
    assert result.submitted
    assert cdp_env.calls["connected"]  # 确认走过一次连接
    # 断点续跑：evidence 已有回执 → 不再触碰浏览器
    receipt = json.loads(ctx.task.evidence)["publish_receipt"]
    assert receipt["platform"] == "xhs"
    before = len(cdp_env.calls["connected"])
    r2 = adapter.publish(ctx)
    assert r2.detail == receipt
    assert len(cdp_env.calls["connected"]) == before  # 未重新连接


def test_xhs_title_limit_and_image_required(make_task, cdp_env):
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_HTML, title="标" * 21, cover=True)
    with pytest.raises(PermanentError, match="title too long"):
        adapter.publish(ctx)
    ctx2 = _cdp_ctx(make_task, "xhs", XHS_HTML, alias="main2", cover=False)
    with pytest.raises(PermanentError, match="图片"):
        adapter.publish(ctx2)


def test_zhihu_douyin_channels_flow_replay(make_task, cdp_env):
    for platform, html in (("zhihu", ZHIHU_HTML), ("douyin", DOUYIN_HTML), ("channels", CHANNELS_HTML)):
        cdp_env.calls["html"] = html
        adapter = get_adapter(platform)
        ctx = _cdp_ctx(make_task, platform, html, alias=f"a-{platform}", cover=True)
        result = adapter.publish(ctx)
        assert result.submitted, platform
        assert json.loads(ctx.task.evidence)["publish_receipt"]["platform"] == platform


def test_new_platforms_flow_replay(make_task, cdp_env):
    """快手/百家号/头条/CSDN：真机来源选择器的 fixture 回放。"""
    port_offset = {"kuaishou": 4, "baijiahao": 5, "toutiao": 6, "csdn": 7}
    for platform, html in (("kuaishou", KS_HTML), ("baijiahao", BJH_HTML),
                           ("toutiao", TT_HTML), ("csdn", CSDN_HTML)):
        cdp_env.calls["html"] = html
        adapter = get_adapter(platform)
        ctx = _cdp_ctx(make_task, platform, html, alias=f"a-{platform}", cover=True,
                       port_offset=port_offset[platform])
        result = adapter.publish(ctx)
        assert result.submitted, platform
        assert json.loads(ctx.task.evidence)["publish_receipt"]["platform"] == platform


def test_xhs_real_selectors_replay(make_task, cdp_env):
    """XiaohongshuSkills 真机选择器回放：tab→上传→标题→正文→发布按钮。"""
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, cover=True, alias="xhs-real")
    result = adapter.publish(ctx)
    assert result.submitted
    assert json.loads(ctx.task.evidence)["publish_receipt"]["platform"] == "xhs"


def test_text_marker_login_guard(make_task, cdp_env):
    """text: 文案标记（douyin「扫码登录」）→ needs_login。"""
    adapter = get_adapter("douyin")
    cdp_env.calls["html"] = DOUYIN_LOGIN_HTML
    ctx = _cdp_ctx(make_task, "douyin", DOUYIN_LOGIN_HTML, cover=True, alias="dy-login")
    with pytest.raises(NeedsLoginError):
        adapter.publish(ctx)


def test_contract_suite_all_adapters(make_task, cdp_env):
    """契约套件（元数据 + 未支持动作）覆盖全部内置适配器。"""
    assert set(registered_platforms()) >= {
        "gzh", "bili", "juejin", "xhs", "zhihu", "douyin", "channels",
        "kuaishou", "baijiahao", "toutiao", "csdn", "mock"}
    for name in registered_platforms():
        adapter = get_adapter(name)
        check_metadata(adapter)
        check_unsupported_actions(adapter, SimpleNamespace())
