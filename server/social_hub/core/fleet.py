"""CDP 舰队管理（设计文档 §6.3）：按账号独立 Chrome Profile + 9300+ 端口。

红线（继承 daily-checkin P0 契约，违反即事故）：
- **绝不终止/重启任何 Chrome 进程**——启动一次后交给操作系统；本模块只负责拉起缺失的实例。
- 端口从 9300 起独占分配；9222 是 daily-checkin 共享 Profile，本模块永生不碰（account add 已拦，此处再拦一道）。
- 断开只 `browser.disconnect()`（Chrome 继续运行），**绝不调用 `browser.close()`**。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass

from ..config import get_settings

MIN_CDP_PORT = 9300  # 9222 属于 daily-checkin，禁碰


def _default_chrome_bin() -> str:
    import sys

    if sys.platform == "win32":
        for p in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ):
            if shutil.which(p) or __import__("pathlib").Path(p).exists():
                return p
        return ""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def probe_cdp(port: int, timeout: float = 2.0) -> dict | None:
    """探 CDP 端口是否已有 Chrome 在服务（/json/version）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


@dataclass
class ChromeLaunch:
    pid: int
    port: int
    profile: str


_playwright_cm = None  # playwright driver 生命周期 = daemon 生命周期（与 Chrome 同级，不主动 stop）
_pw_lock = threading.Lock()  # 多 worker 线程并发首连时防止双 driver（review R2）


def _get_playwright():
    """playwright driver 单例：每进程只 start 一次。

    此前在 _connect 内逐任务 start()——node driver 子进程从不回收，daemon 长跑
    每发一篇泄漏一个进程（review P1）。driver 与被附着的 Chrome 同为常驻资源。
    """
    global _playwright_cm
    if _playwright_cm is None:  # 快路径
        with _pw_lock:
            if _playwright_cm is None:  # 双检：workers>1 时并发首连
                from playwright.sync_api import sync_playwright  # 延迟导入：API 通道零浏览器依赖

                _playwright_cm = sync_playwright().start()
    return _playwright_cm


def reset_playwright() -> None:  # 测试用
    global _playwright_cm
    if _playwright_cm is not None:
        try:
            _playwright_cm.stop()
        except Exception:
            pass
    _playwright_cm = None


class ChromeFleet:
    """舰队：ensure(account) → 已连接的 Browser 句柄。可注入 connector/prober 便于测试。"""

    LAUNCH_WAIT_SECONDS = 30

    def __init__(self, connector=None, prober=None, launcher=None):
        self._connector = connector  # (url) -> browser（playwright connect_over_cdp 包装）
        self._prober = prober or probe_cdp
        self._launcher = launcher  # (argv) -> Popen（测试注入）

    # ---- 生命周期 ----
    def ensure(self, account) -> "CdpBrowserHandle":
        settings = get_settings()
        port = account.cdp_port
        if port is None or port < MIN_CDP_PORT:
            # 红线双保险：9222 及一切 <9300 端口一律拒绝
            raise ValueError(f"cdp port must be >= {MIN_CDP_PORT} (got {port}; 9222 belongs to daily-checkin)")
        profile = account.chrome_profile or str(settings.chrome_profiles_dir / f"{account.platform}-{account.alias}")
        if not self._prober(port):
            self._launch(port, profile)
        return self._connect(port)

    def _launch(self, port: int, profile: str) -> None:
        settings = get_settings()
        chrome = settings.chrome_bin or _default_chrome_bin()
        if not chrome:
            raise RuntimeError("chrome binary not found; set SOCIAL_HUB_CHROME_BIN")
        argv = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--window-size=1440,960",
        ]
        # 只 Popen，不等待、不杀：进程退出/清理由人管理（红线）
        if self._launcher is not None:
            self._launcher(argv)
        else:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + self.LAUNCH_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._prober(port):
                return
            time.sleep(0.5)
        raise RuntimeError(f"chrome CDP on port {port} did not come up within {self.LAUNCH_WAIT_SECONDS}s")

    def _connect(self, port: int) -> "CdpBrowserHandle":
        if self._connector is not None:
            browser = self._connector(f"http://127.0.0.1:{port}")
        else:
            browser = _get_playwright().chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=get_settings().cdp_connect_timeout * 1000
            )
        return CdpBrowserHandle(browser, port)


class CdpBrowserHandle:
    """已连接的浏览器句柄。close() 只 disconnect，Chrome 进程继续跑（红线）。"""

    def __init__(self, browser, port: int):
        self.browser = browser
        self.port = port

    def page(self, url: str, timeout_ms: int = 30000):
        """取（或新建）一个页面并打开 url。优先复用已有空白页，避免越开越多 tab。"""
        ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        page = next((p for p in ctx.pages if p.url in ("about:blank", "")), None) or ctx.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return page

    def close(self) -> None:
        # 红线：绝不 browser.close()（那会杀掉用户的 Chrome 实例）
        try:
            self.browser.disconnect()
        except AttributeError:  # 测试假件可能只有 close 语义
            pass


_fleet: ChromeFleet | None = None


def get_fleet() -> ChromeFleet:
    global _fleet
    if _fleet is None:
        _fleet = ChromeFleet()
    return _fleet


def reset_fleet() -> None:  # 测试用
    global _fleet
    _fleet = None
