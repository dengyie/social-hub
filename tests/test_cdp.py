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
from social_hub.adapters.contract import check_capability_truthfulness, check_metadata, check_unsupported_actions
from social_hub.adapters.registry import get_adapter, registered_platforms
from social_hub.core import fleet as fleet_mod
from social_hub.core.fleet import ChromeFleet

from fake_cdp import FakeBrowser, FakePage

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
<span>发表视频</span>
<input type="file" id="file"/>
<div id="post-content"><textarea></textarea></div>
<div class="input-editor" contenteditable="true"></div>
<input placeholder="填写短标题有机会获得更多流量"/>
<button>发 表</button></body></html>
"""
LOGIN_HTML = '<html><body><div class="login-container"></div></body></html>'
CAPTCHA_HTML = '<html><body><div class="captcha-slider"></div></body></html>'
# —— 参考项目真机选择器对应 fixture ——
XHS_REAL_HTML = """
<html><body>
<div class="creator-tab">上传图文 上传视频</div>
<div class="upload-area">
  <input class="upload-input" type="file" accept=".mp4,.mov,.flv,.f4v,.mkv,.rm,.rmvb,.m4v,.mpg,.mpeg,.ts"/>
  <input class="upload-input" type="file" accept=".jpg,.jpeg,.png,.webp"/>
</div>
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
<div role="tablist">
  <div role="tab">视频</div>
  <div role="tab">图文</div>
</div>
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
<button data-testid="publish-btn">发布</button>
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
WEIBO_HTML = """
<html><body>
<textarea placeholder="有什么新鲜事想分享给大家？"></textarea>
<input type="file"/>
<button>发布</button>
</body></html>
"""
ALIPAY_HTML = """
<html><body>
<a>发布视频推荐分辨率720p及以上，建议1080p</a>
<input type="file"/>
<input placeholder="一个好的标题，能获得更多人的喜欢哦"/>
<textarea placeholder="填写作品描述，让你的作品更容易被看到"></textarea>
<button>确认发布</button>
</body></html>
"""


@pytest.fixture()
def cdp_env(env, monkeypatch):
    """env + 舰队假件：connector 按记录返回带 fixture HTML 的 FakeBrowser。"""
    calls: dict = {"launched": [], "connected": []}

    def connector(url: str) -> FakeBrowser:
        browser = FakeBrowser(calls["html"], page_cls=calls.get("page_cls"))
        calls["connected"].append(browser)
        return browser

    def prober(port: int) -> dict | None:
        return {"Browser": "fake"} if port >= 9300 else None

    def launcher(argv) -> None:
        calls["launched"].append(argv)

    fleet_mod.reset_fleet()
    fleet_mod._fleet = ChromeFleet(connector=connector, prober=prober, launcher=launcher)
    fleet_mod._fleet.calls = calls
    yield fleet_mod._fleet
    fleet_mod.reset_fleet()


CDP_PORT_OFFSET = {"xhs": 0, "zhihu": 1, "douyin": 2, "channels": 3,
                   "kuaishou": 4, "baijiahao": 5, "toutiao": 6, "csdn": 7,
                   "weibo": 8, "alipay": 9}


def _cdp_ctx(make_task, platform: str, html: str, alias: str = "main", cover: bool = True,
             title: str = "hello social-hub", port_offset: int | None = None,
             cover_kind: str = "image"):
    if port_offset is None:
        port_offset = CDP_PORT_OFFSET.get(platform, 99)
    make_task(platform=platform, alias=alias, cover=cover, title=title,
              cdp_port=9300 + port_offset, cover_kind=cover_kind)
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


class _StubEl:
    """click_exact_text 候选桩件：inner_text 文案 + 可控 click 行为。"""

    def __init__(self, text: str, raise_on_click: bool = False):
        self._text = text
        self._raise = raise_on_click
        self.clicked = False

    def inner_text(self) -> str:
        return self._text

    def click(self, timeout: int | None = None):
        if self._raise:
            raise TimeoutError("element is outside of the viewport")
        self.clicked = True


class _StubPage:
    def __init__(self, elements):
        self._elements = elements

    def query_selector_all(self, sel):
        return self._elements


def test_click_exact_text_skips_unreachable_candidates(env):
    """同文案的视口外/隐藏节点不卡死：首个 click 超时 → 换下一个候选（真机 2026-09-14 实证）。"""
    ad = get_adapter("xhs")
    offscreen = _StubEl("上传图文", raise_on_click=True)
    good = _StubEl("上传图文")
    assert ad.click_exact_text(_StubPage([offscreen, good]), "上传图文") is True
    assert good.clicked and not offscreen.clicked


def test_click_exact_text_all_candidates_fail_returns_false(env):
    ad = get_adapter("xhs")
    bad = _StubEl("上传图文", raise_on_click=True)
    assert ad.click_exact_text(_StubPage([bad]), "上传图文") is False
    assert ad.click_exact_text(_StubPage([_StubEl("其他")]), "上传图文") is False


class _BoxEl:
    """带 bounding_box 的候选桩件：验证视口预筛。"""

    def __init__(self, text: str, box: dict):
        self._text = text
        self._box = box
        self.clicked = False

    def inner_text(self) -> str:
        return self._text

    def bounding_box(self) -> dict:
        return self._box

    def click(self, timeout: int | None = None):
        assert self._box["x"] >= 0, "视口外节点不允许被点击"
        self.clicked = True


def test_click_exact_text_prefilters_offscreen_candidates(env):
    """根因修复：同文案候选先按可见性+视口预筛，视口内节点首点命中，模板节点不参与点击。"""
    ad = get_adapter("xhs")
    offscreen = _BoxEl("上传图文", {"x": -9726, "y": -9918, "width": 96, "height": 40})
    visible = _BoxEl("上传图文", {"x": 385, "y": 81, "width": 96, "height": 40})
    assert ad.click_exact_text(_StubPage([offscreen, visible]), "上传图文") is True
    assert visible.clicked and not offscreen.clicked


def test_first_has_bounded_poll(env):
    """上传/切 tab 后表单异步渲染：first_has 有界轮询到挂载为止，超时返回 None。"""
    ad = get_adapter("xhs")
    calls = {"n": 0}

    def scripted_has(page, name):
        calls["n"] += 1
        return calls["n"] >= 3  # 前两轮未挂载，第三轮出现

    ad.has = scripted_has
    assert ad.first_has(_StubPage([]), "title_input", "title_input_alt", timeout=5) == "title_input"
    ad.has = lambda page, name: False
    assert ad.first_has(_StubPage([]), "title_input", timeout=0.6) is None


def test_fleet_shared_port_attaches_when_up_never_launches():
    """共享 9222（daily-checkin 浏览器）：在跑就附着，绝不代启。"""
    connected: list[str] = []
    launched: list[list] = []
    fleet = ChromeFleet(
        connector=lambda url: connected.append(url)
        or SimpleNamespace(contexts=[], new_context=lambda: None, disconnect=lambda: None),
        prober=lambda port: {"Browser": "fake"} if port == 9222 else None,
        launcher=lambda argv: launched.append(argv),
    )
    account = SimpleNamespace(cdp_port=9222, chrome_profile=None, platform="xhs", alias="main")
    fleet.ensure(account)
    assert connected == ["http://127.0.0.1:9222"]
    assert not launched, "共享端口绝不允许代启"


def test_fleet_shared_port_down_refuses_to_launch():
    """共享 9222 不在跑：报错并给启动指引，绝不代启（生命周期归 daily-checkin）。"""
    launched: list[list] = []
    fleet = ChromeFleet(
        connector=lambda url: None,
        prober=lambda port: None,
        launcher=lambda argv: launched.append(argv),
    )
    account = SimpleNamespace(cdp_port=9222, chrome_profile=None, platform="xhs", alias="main")
    with pytest.raises(RuntimeError, match="9222"):
        fleet.ensure(account)
    assert not launched


def test_fleet_refuses_other_ports_below_9300():
    fleet = ChromeFleet(prober=lambda port: {"ok": 1})
    account = SimpleNamespace(cdp_port=9250, chrome_profile=None, platform="xhs", alias="main")
    with pytest.raises(ValueError, match="9300"):
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
    with pytest.raises(PermanentError, match="媒体"):
        adapter.publish(ctx2)
    # 无媒体仍拒绝（图文/视频都必需封面）；kind 分流见 test_xhs_video_selectors_replay


def test_zhihu_douyin_channels_flow_replay(make_task, cdp_env):
    for platform, html in (("zhihu", ZHIHU_HTML), ("douyin", DOUYIN_HTML), ("channels", CHANNELS_HTML)):
        cdp_env.calls["html"] = html
        adapter = get_adapter(platform)
        ctx = _cdp_ctx(make_task, platform, html, alias=f"a-{platform}", cover=True)
        result = adapter.publish(ctx)
        assert result.submitted, platform
        assert json.loads(ctx.task.evidence)["publish_receipt"]["platform"] == platform


def test_new_platforms_flow_replay(make_task, cdp_env):
    """快手/百家号/头条/CSDN/微博/支付宝：预置选择器的 fixture 回放。

    review P1：baijiahao 仍是视频投稿；kuaishou 现支持图文+视频（见
    test_kuaishou_image_tab_replay）。错配媒体必须在提交前拒绝。
    """
    cases = (("kuaishou", KS_HTML, "video"), ("baijiahao", BJH_HTML, "video"),
             ("toutiao", TT_HTML, "image"), ("csdn", CSDN_HTML, "image"),
             ("weibo", WEIBO_HTML, "image"), ("alipay", ALIPAY_HTML, "video"))
    for platform, html, kind in cases:
        cdp_env.calls["html"] = html
        adapter = get_adapter(platform)
        ctx = _cdp_ctx(make_task, platform, html, alias=f"a-{platform}", cover=True,
                       cover_kind=kind)
        result = adapter.publish(ctx)
        assert result.submitted, platform
        assert json.loads(ctx.task.evidence)["publish_receipt"]["platform"] == platform


def test_media_kind_guard_rejects_mismatched_media(make_task, cdp_env):
    """review P1 回归：媒体 kind 错配一律在提交前拒绝（PermanentError，零浏览器副作用）。"""
    cases = (("baijiahao", BJH_HTML, "video", "image"),)
    for platform, html, ok_kind, bad_kind in cases:
        adapter = get_adapter(platform)
        cdp_env.calls["html"] = html
        ctx = _cdp_ctx(make_task, platform, html, alias=f"g-{platform}", cover=True,
                       cover_kind=bad_kind)
        with pytest.raises(PermanentError, match="kind="):
            adapter.publish(ctx)
        # 错配拒绝发生在 _validated_snapshot：不得触碰浏览器
        assert cdp_env.calls["connected"] == [], f"{platform} 错配校验前就连接了浏览器"


def test_xhs_real_selectors_replay(make_task, cdp_env):
    """XiaohongshuSkills 真机选择器回放：tab→上传→标题→正文→发布按钮。

    断言 upload 动作精确命中 accept 含 .jpg 的图文输入框，而不是视频输入框。
    """
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, cover=True, alias="xhs-real")
    result = adapter.publish(ctx)
    assert result.submitted
    assert json.loads(ctx.task.evidence)["publish_receipt"]["platform"] == "xhs"
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    uploads = [a for a in page.actions if a[0] == "upload"]
    assert uploads, "必须执行了上传动作"
    # 断言选中的是包含 accept*='.jpg' 的选择器
    assert "accept*='.jpg'" in uploads[0][1]
    receipt = json.loads(ctx.task.evidence)["publish_receipt"]
    assert receipt.get("note_kind") == "image"
    gotos = [a[1] for a in page.actions if a[0] == "goto"]
    assert any("target=image" in (g or "") for g in gotos), gotos


def test_xhs_video_selectors_replay(make_task, cdp_env):
    """SAU target=video + Skills「上传视频」+ accept=mp4；标题框作为转码就绪信号。"""
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, cover=True, cover_kind="video",
                   alias="xhs-video")
    result = adapter.publish(ctx)
    assert result.submitted
    receipt = json.loads(ctx.task.evidence)["publish_receipt"]
    assert receipt["platform"] == "xhs"
    assert receipt.get("note_kind") == "video"
    assert receipt.get("images") == 0
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    uploads = [a for a in page.actions if a[0] == "upload"]
    assert uploads, "必须执行了视频上传"
    assert "accept*='.mp4'" in uploads[0][1]
    gotos = [a[1] for a in page.actions if a[0] == "goto"]
    assert any("target=video" in (g or "") for g in gotos), gotos


def test_require_has_raises_when_missing(env):
    ad = get_adapter("xhs")
    ad.has = lambda page, name: False
    with pytest.raises(PermanentError, match="未命中"):
        ad.require_has(_StubPage([]), "title_input", timeout=0.6)


def test_kuaishou_image_tab_replay(make_task, cdp_env):
    """SAU KSNote：图文走 role=tab「图文」，不再把图片当视频错配拒绝。"""
    adapter = get_adapter("kuaishou")
    cdp_env.calls["html"] = KS_HTML
    ctx = _cdp_ctx(make_task, "kuaishou", KS_HTML, alias="ks-image", cover=True,
                   cover_kind="image")
    result = adapter.publish(ctx)
    assert result.submitted
    receipt = json.loads(ctx.task.evidence)["publish_receipt"]
    assert receipt.get("note_kind") == "image"
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any((len(a) > 2 and a[2] == "图文") for a in clicks), clicks


def test_channels_home_entry_replay(make_task, cdp_env):
    """SAU：先进 /platform 再点「发表视频」；直开 /post/create 表单不挂载。"""
    adapter = get_adapter("channels")
    assert adapter.publish_url.rstrip("/").endswith("/platform")
    assert "/post/create" not in adapter.publish_url
    cdp_env.calls["html"] = CHANNELS_HTML
    ctx = _cdp_ctx(make_task, "channels", CHANNELS_HTML, alias="ch-home",
                   cover=True, cover_kind="video")
    result = adapter.publish(ctx)
    assert result.submitted
    receipt = json.loads(ctx.task.evidence)["publish_receipt"]
    assert receipt.get("note_kind") == "video"
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    gotos = [a[1] for a in page.actions if a[0] == "goto"]
    assert any(g.rstrip("/").endswith("/platform") for g in gotos), gotos
    assert not any("/post/create" in (g or "") for g in gotos), gotos
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any((len(a) > 2 and a[2] == "发表视频") for a in clicks), clicks
    fills = [a for a in page.actions if a[0] == "fill"]
    assert any("短标题" in a[1] for a in fills), fills


def test_alipay_sau_entry_url(make_task, cdp_env):
    """SAU：c.alipay.com 内容创作入口必须带 _appScene=CONTENT，否则跳开通页。"""
    adapter = get_adapter("alipay")
    assert adapter.calibrate_only
    assert "_appScene=CONTENT" in adapter.publish_url
    assert adapter.publish_url.startswith("https://c.alipay.com/")
    cdp_env.calls["html"] = ALIPAY_HTML
    ctx = _cdp_ctx(make_task, "alipay", ALIPAY_HTML, alias="ap-sau", cover=True,
                   cover_kind="video")
    result = adapter.publish(ctx)
    assert result.submitted
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    gotos = [a[1] for a in page.actions if a[0] == "goto"]
    assert any("_appScene=CONTENT" in (g or "") for g in gotos), gotos
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any((len(a) > 2 and "发布视频" in a[2]) for a in clicks), clicks


class LateBaijiahaoTitlePage(FakePage):
    """标题区在上传后才挂载（SAU wait visible 180s）；瞬时 has 会竞态。"""

    def query_selector(self, sel):
        if "contentEditable" in sel:
            self._title_q = getattr(self, "_title_q", 0) + 1
            if self._title_q < 3:
                return None
        return super().query_selector(sel)


def test_baijiahao_title_wait_after_upload(make_task, cdp_env):
    adapter = get_adapter("baijiahao")
    cdp_env.calls["html"] = BJH_HTML
    cdp_env.calls["page_cls"] = LateBaijiahaoTitlePage
    ctx = _cdp_ctx(make_task, "baijiahao", BJH_HTML, alias="bjh-wait",
                   cover=True, cover_kind="video")
    result = adapter.publish(ctx)
    assert result.submitted
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    assert page._title_q >= 3
    types = [a for a in page.actions if a[0] == "type"]
    assert types, page.actions



def test_text_marker_login_guard(make_task, cdp_env):
    """text: 文案标记（douyin「扫码登录」）→ needs_login。"""
    adapter = get_adapter("douyin")
    cdp_env.calls["html"] = DOUYIN_LOGIN_HTML
    ctx = _cdp_ctx(make_task, "douyin", DOUYIN_LOGIN_HTML, cover=True, alias="dy-login")
    with pytest.raises(NeedsLoginError):
        adapter.publish(ctx)


def test_contract_suite_all_adapters(make_task, cdp_env):
    """契约套件（元数据 + 能力真实性 + 未支持动作）覆盖全部内置适配器。"""
    assert set(registered_platforms()) >= {
        "gzh", "bili", "juejin", "xhs", "zhihu", "douyin", "channels",
        "kuaishou", "baijiahao", "toutiao", "csdn", "weibo", "alipay", "mock"}
    for name in registered_platforms():
        adapter = get_adapter(name)
        check_metadata(adapter)
        check_capability_truthfulness(adapter)  # review P2：声明的能力必须有对应承载
        check_unsupported_actions(adapter, SimpleNamespace())


# ---- review 修复回归：driver 复用 / 重定向 / 确认弹窗 / 句柄释放 / QR 落盘 ----
def test_fleet_playwright_driver_reused(env, monkeypatch):
    """P1 回归：两次 ensure 只允许 start 一次 playwright driver（真函数 + 假模块源）。"""
    import sys
    import types

    from social_hub.core import fleet as fleet_mod

    starts = {"n": 0}

    class FakePw:
        class chromium:
            @staticmethod
            def connect_over_cdp(url, timeout=0):
                return FakeBrowser("<html></html>")

    def fake_sync_playwright():
        starts["n"] += 1
        return SimpleNamespace(start=lambda: FakePw())

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    fleet_mod.reset_playwright()
    fleet_mod.reset_fleet()
    fleet_mod._fleet = fleet_mod.ChromeFleet(prober=lambda port: {"ok": 1})
    account = SimpleNamespace(cdp_port=9310, chrome_profile="/tmp/p1", platform="xhs", alias="a")
    fleet_mod._fleet.ensure(account).close()
    fleet_mod._fleet.ensure(account).close()
    assert starts["n"] == 1


def test_fleet_playwright_start_race_thread_local(env, monkeypatch):
    """P1 回归：多 worker 线程并发执行时，每线程持有独立 driver 实例（避免跨线程崩溃），

    并且所有 driver 都会被正确注册和回收。
    """
    import sys
    import threading
    import time
    import types

    from social_hub.core import fleet as fleet_mod

    starts = {"n": 0}

    class FakePw:
        class chromium:
            @staticmethod
            def connect_over_cdp(url, timeout=0):
                return FakeBrowser("<html></html>")

    def fake_sync_playwright():
        starts["n"] += 1
        return SimpleNamespace(start=lambda: FakePw())

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    fleet_mod.reset_playwright()
    fleet_mod.reset_fleet()
    fleet_mod._fleet = fleet_mod.ChromeFleet(prober=lambda port: {"ok": 1})
    account = SimpleNamespace(cdp_port=9311, chrome_profile="/tmp/p2", platform="xhs", alias="a")

    results: list = []

    def worker():
        try:
            fleet_mod._fleet.ensure(account).close()
            # 同线程再次 ensure，应复用本线程 driver
            fleet_mod._fleet.ensure(account).close()
            results.append("ok")
        except Exception as e:  # pragma: no cover  仅在实现退化时触发
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert results == ["ok", "ok"]
    # 2 个不同线程各自 start 一次 driver，同线程复用：总计 starts 刚好为 2
    assert starts["n"] == 2
    assert len(fleet_mod._all_drivers) == 2
    fleet_mod.reset_playwright()
    assert len(fleet_mod._all_drivers) == 0


def test_login_redirect_marker_reports_expired(make_task):
    """login_redirect_marker（xhs /login 跳转）→ check_login=expired。"""
    from social_hub.adapters.registry import get_adapter
    from social_hub.config import get_settings
    from social_hub.core import fleet as fleet_mod
    from social_hub.core.fleet import ChromeFleet
    from social_hub.db import get_session_factory
    from social_hub.vault.service import get_account

    class RedirectLoginPage(FakePage):
        def goto(self, url, timeout=0, wait_until=None):
            super().goto(url, timeout=timeout, wait_until=wait_until)
            self.url = "https://creator.xiaohongshu.com/login"  # 模拟未登录跳转

    class RedirectBrowser(FakeBrowser):
        def __init__(self, html):
            super().__init__(html)
            self.contexts[0]._page_cls = RedirectLoginPage

    fleet_mod.reset_fleet()
    fleet_mod._fleet = ChromeFleet(
        connector=lambda url: RedirectBrowser(XHS_REAL_HTML), prober=lambda port: {"ok": 1})
    try:
        make_task(platform="xhs", alias="redir", cover=True, cdp_port=9320)
        with get_session_factory()() as s:
            acc = get_account(s, "xhs", "redir")
            ctx = SimpleNamespace(account=acc)
        state = get_adapter("xhs").check_login(ctx)
        assert state == "expired"
    finally:
        fleet_mod.reset_fleet()


class LateConfirmPage(FakePage):
    """前 2 次 button 扫描隐藏确认按钮，之后出现（模拟快手异步弹窗）。"""

    def query_selector_all(self, sel):
        els = super().query_selector_all(sel)
        if "button" in sel:
            self._scan = getattr(self, "_scan", 0) + 1
            if self._scan <= 2:
                return [e for e in els if "确" not in (e.inner_text() or "")]
        return els


def test_kuaishou_confirm_modal_waited_and_clicked(make_task, cdp_env):
    adapter = get_adapter("kuaishou")
    cdp_env.calls["html"] = KS_HTML
    cdp_env.calls["page_cls"] = LateConfirmPage  # 前 2 次扫描隐藏确认按钮 → 必须轮询等待
    ctx = _cdp_ctx(make_task, "kuaishou", KS_HTML, alias="ks-ok", cover=True,
                   cover_kind="video", port_offset=94)
    result = adapter.publish(ctx)
    assert result.submitted
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any("确" in (a[2] if len(a) > 2 else "") for a in clicks), clicks  # 确认弹窗被二次点击


def test_kuaishou_confirm_timeout_is_transient(make_task, cdp_env):
    from social_hub.adapters.base import TransientError

    adapter = get_adapter("kuaishou")
    old_wait, old_poll = adapter.confirm_wait_seconds, adapter.confirm_poll_interval
    adapter.confirm_wait_seconds = 0.3
    adapter.confirm_poll_interval = 0.1
    try:
        cdp_env.calls["html"] = KS_HTML.replace("<button>确 认</button>", "")  # 弹窗永不出现
        ctx = _cdp_ctx(make_task, "kuaishou", cdp_env.calls["html"], alias="ks-timeout",
                       cover=True, cover_kind="video", port_offset=95)
        with pytest.raises(TransientError, match="confirm"):
            adapter.publish(ctx)
        evidence = json.loads(ctx.task.evidence) if ctx.task.evidence else {}
        assert evidence.get("publish_receipt") is None  # 回执未落盘，重跑安全
    finally:
        adapter.confirm_wait_seconds = old_wait
        adapter.confirm_poll_interval = old_poll


def test_open_closes_handle_when_page_raises(make_task):
    """_open 中 goto/守护抛错 → 句柄必须 disconnect（资源对称释放）。"""
    from social_hub.adapters.registry import get_adapter
    from social_hub.config import get_settings
    from social_hub.core import fleet as fleet_mod
    from social_hub.core.fleet import ChromeFleet
    from social_hub.db import get_session_factory
    from social_hub.vault.service import get_account

    class ExplodingPage(FakePage):
        def goto(self, url, timeout=0, wait_until=None):
            raise RuntimeError("navigation timeout")

    class ExplodingBrowser(FakeBrowser):
        def __init__(self, html):
            super().__init__(html)
            self.contexts[0]._page_cls = ExplodingPage

    fleet_mod.reset_fleet()
    browser = ExplodingBrowser(XHS_REAL_HTML)
    fleet_mod._fleet = ChromeFleet(connector=lambda url: browser, prober=lambda port: {"ok": 1})
    try:
        make_task(platform="xhs", alias="boom", cover=True, cdp_port=9321)
        with get_session_factory()() as s:
            acc = get_account(s, "xhs", "boom")
            ctx = SimpleNamespace(account=acc)
        with pytest.raises(RuntimeError):
            get_adapter("xhs")._open(ctx, get_adapter("xhs").publish_url)
        assert browser.disconnected
    finally:
        fleet_mod.reset_fleet()


def test_login_interactive_saves_qr(make_task, cdp_env, monkeypatch):
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = LOGIN_HTML
    ctx = _cdp_ctx(make_task, "xhs", LOGIN_HTML, alias="qracc", cover=True, port_offset=96)
    out = adapter.login_interactive(ctx)
    from social_hub.config import get_settings

    qr = get_settings().data_dir / "qr" / "xhs-qracc.png"
    assert out["qr_path"] == str(qr)
    assert qr.read_bytes() == b"png-bytes"


def test_cli_doctor_skips_text_keys(make_task, cdp_env):
    """doctor 只探测 CSS 选择器键，`*_text` 文案目标不得进入探测（真机 playwright 会炸）。"""
    from cli.shub import app
    from typer.testing import CliRunner

    cdp_env.calls["html"] = XHS_REAL_HTML
    _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, alias="docx", cover=True, port_offset=97)
    r = CliRunner().invoke(app, ["doctor", "--platform", "xhs"])
    assert r.exit_code == 0, r.output
    assert "image_tab_text" not in r.output
    assert "summary:" in r.output


def test_shared_cdp_handle_creates_isolated_pages():
    """P2-2 回归：共享端口（9222）调用 page() 时强制 new_page 且 close 时安全回收自身页面，

    不抢占公共 about:blank tab。
    """
    from social_hub.core.fleet import CdpBrowserHandle, SHARED_CDP_PORT

    class FakeCtx:
        def __init__(self):
            self.pages = [FakePage("<html>blank</html>")]
            self.pages[0].url = "about:blank"
            self.created = []

        def new_page(self):
            p = FakePage("<html>new</html>")
            self.created.append(p)
            self.pages.append(p)
            return p

    class FakeB:
        def __init__(self):
            self.contexts = [FakeCtx()]
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True

    b = FakeB()
    h1 = CdpBrowserHandle(b, SHARED_CDP_PORT)
    h2 = CdpBrowserHandle(b, SHARED_CDP_PORT)

    p1 = h1.page("https://creator.xiaohongshu.com")
    p2 = h2.page("https://www.zhihu.com/creator")

    # 两者分别获得了不同的新页面实例，未复用关于 about:blank 的第一个页面
    assert p1 is not p2
    assert p1 in b.contexts[0].created
    assert p2 in b.contexts[0].created

    # 关闭句柄只关闭该句柄创建的页面，不干扰其它句柄
    h1.close()
    assert h1.browser.disconnected
    assert p1.closed
    assert not p2.closed

    h2.close()
    assert p2.closed


def test_click_exact_text_fallback_priority_order():
    """P3-1 回归：视口内元素优先点击；若视口内元素无法点击，应平滑回退到视口外但合法的元素。"""
    ad = get_adapter("xhs")
    # offscreen 属于视口外候选，但 click 能够滚动到位成功
    offscreen = _BoxEl("确定", {"x": 100, "y": -500, "width": 80, "height": 30})
    # inview 属于视口内候选，但点击抛异常
    inview = _BoxEl("确定", {"x": 100, "y": 100, "width": 80, "height": 30})
    inview.click = lambda timeout=None: (_ for _ in ()).throw(RuntimeError("intercepted"))

    assert ad.click_exact_text(_StubPage([offscreen, inview]), "确定") is True
    assert offscreen.clicked


def test_check_login_ok_closes_handle(make_task, cdp_env):
    """P1：check_login 成功路径必须 close handle（此前 9222 标签泄漏）。"""
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, alias="login-ok", cover=True, port_offset=80)
    state = adapter.check_login(ctx)
    assert state == "ok"
    assert cdp_env.calls["connected"][-1].disconnected


def test_preview_skips_publish_and_receipt(make_task, cdp_env):
    """preview=True：填表但不点发布、不写 publish_receipt（收据是防重发闸门）。"""
    adapter = get_adapter("xhs")
    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, alias="preview", cover=True, port_offset=81)
    ctx.preview = True
    result = adapter.publish(ctx)
    assert result.submitted is False
    ev = json.loads(ctx.task.evidence or "{}")
    assert "publish_receipt" not in ev
    assert ev["preview"]["preview"] is True
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    clicks = [a for a in page.actions if a[0] == "click"]
    assert not any("publish-page-publish-btn" in str(a) for a in clicks)


def test_orchestrator_cdp_keeps_receipt_and_resume_skips_publish(make_task, cdp_env):
    """P0：编排器合并证据不得抹掉 publish_receipt；续跑只核验、不再点发布。"""
    from social_hub.core.orchestrator import Orchestrator
    from social_hub.db import session
    from social_hub.models import Account, AutomationTask
    from social_hub.vault.service import touch_login_check

    cdp_env.calls["html"] = XHS_REAL_HTML
    ctx = _cdp_ctx(make_task, "xhs", XHS_REAL_HTML, alias="orch", cover=True, port_offset=82)
    with session() as s:
        acc = s.get(Account, ctx.account.id)
        touch_login_check(s, acc, "ok")
        s.commit()
        task_id = ctx.task.id

    Orchestrator(worker_prefix="cdp").run_once()
    with session() as s:
        t = s.get(AutomationTask, task_id)
        assert t.status == "done"
        ev = json.loads(t.evidence or "{}")
        assert ev["publish_receipt"]["platform"] == "xhs"
        assert "verify" in ev

    connected_after_first = len(cdp_env.calls["connected"])
    with session() as s:
        t = s.get(AutomationTask, task_id)
        t.status = "queued"
        t.finished_at = None
        t.claimed_by = None
        t.lease_expires_at = None
        t.error = None
        s.commit()
    Orchestrator(worker_prefix="cdp-r").run_once()
    assert len(cdp_env.calls["connected"]) == connected_after_first  # 续跑不碰浏览器
    with session() as s:
        t = s.get(AutomationTask, task_id)
        assert t.status == "done"

