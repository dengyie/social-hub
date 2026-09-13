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


def test_fleet_playwright_start_race_single_driver(env, monkeypatch):
    """R2 回归：多 worker 线程并发首连，双检锁保证只 start 一个 driver。"""
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
        time.sleep(0.2)  # 放大竞态窗口：无锁时第二线程必然也进入 start
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
            results.append("ok")
        except Exception as e:  # pragma: no cover  仅在实现退化时触发
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert results == ["ok", "ok"]
    assert starts["n"] == 1


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
    ctx = _cdp_ctx(make_task, "kuaishou", KS_HTML, alias="ks-ok", cover=True, port_offset=94)
    result = adapter.publish(ctx)
    assert result.submitted
    page = cdp_env.calls["connected"][-1].contexts[0].pages[-1]
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any("确" in (a[2] if len(a) > 2 else "") for a in clicks), clicks  # 确认弹窗被二次点击


def test_kuaishou_confirm_timeout_is_transient(make_task, cdp_env):
    from social_hub.adapters.base import TransientError

    adapter = get_adapter("kuaishou")
    adapter.confirm_wait_seconds = 0.3
    adapter.confirm_poll_interval = 0.1
    cdp_env.calls["html"] = KS_HTML.replace("<button>确 认</button>", "")  # 弹窗永不出现
    ctx = _cdp_ctx(make_task, "kuaishou", cdp_env.calls["html"], alias="ks-timeout",
                   cover=True, port_offset=95)
    with pytest.raises(TransientError, match="confirm"):
        adapter.publish(ctx)
    evidence = json.loads(ctx.task.evidence) if ctx.task.evidence else {}
    assert evidence.get("publish_receipt") is None  # 回执未落盘，重跑安全


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
