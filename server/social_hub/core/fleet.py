"""CDP 舰队管理（设计文档 §6.3）：9222 共享浏览器（attach-only）+ 9300+ 按账号独立实例。

红线（继承 daily-checkin P0 契约，违反即事故）：
- **绝不终止/重启任何 Chrome 进程**——启动一次后交给操作系统；本模块只负责拉起缺失的实例。
- 端口两种形态（2026-09-13 起，mac 浏览器共享方案）：
  - **9222 = daily-checkin 共享浏览器**（专用 chrome-checkin-profile，登录态由用户手工登录）：
    **attach-only**——在跑就附着，不在跑报错并给出启动指引，**绝不代启、绝不代关**。
  - **9300+ = 按账号独立 Profile**，端口缺失时本模块可代启。
  - 其余 <9300 端口一律拒绝（account add 已拦，此处再拦一道）。
- 断开只 `browser.disconnect()`（Chrome 继续运行），**绝不调用 `browser.close()`**。
"""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass

from ..config import MIN_CDP_PORT, SHARED_CDP_PORT, get_settings


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


_local = threading.local()  # 每线程独立一个 driver（sync playwright 绑定创建线程，见 review P1）
_all_drivers: list = []  # 记录所有已创建的 driver，供 atexit 统一 stop
_drivers_lock = threading.Lock()
_pw_atexit_registered = False


def _get_playwright():
    """playwright driver：每线程一个单例。

    此前在进程级别做单例，但在 sync_playwright 架构下，driver 内部绑定了创建线程。
    当 workers>1 时，其它 worker 线程调用 connect_over_cdp 会抛出
    'Cannot switch to a different thread'（review P1）。
    改用 threading.local() 保障每线程独立持有 driver，数量 <= workers（有界）；
    同时在 atexit 中汇总 stop() 所有线程的 driver，消除进程退出时的 EPIPE 噪音。
    """
    global _pw_atexit_registered
    cm = getattr(_local, "cm", None)
    if cm is None:
        from playwright.sync_api import sync_playwright

        cm = sync_playwright().start()
        _local.cm = cm
        with _drivers_lock:
            _all_drivers.append(cm)
            if not _pw_atexit_registered:
                atexit.register(reset_playwright)
                _pw_atexit_registered = True
    return cm


def reset_playwright() -> None:  # 测试与退出收尾用
    global _all_drivers
    with _drivers_lock:
        drivers = list(_all_drivers)
        _all_drivers.clear()
    for cm in drivers:
        try:
            cm.stop()
        except Exception:
            pass
    if hasattr(_local, "cm"):
        _local.cm = None


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
        if port == SHARED_CDP_PORT:
            # mac 共享方案：附着已在运行的共享浏览器；不在跑时绝不代启
            # （该实例的生命周期归 daily-checkin，启动命令见运维手册）
            if not self._prober(port):
                raise RuntimeError(
                    f"shared CDP browser on port {SHARED_CDP_PORT} is not running; "
                    "start it per the ops manual (dedicated chrome-checkin-profile, "
                    "--remote-debugging-port=9222) — social-hub never auto-launches or kills it"
                )
            return self._connect(port)
        if port is None or port < MIN_CDP_PORT:
            # 其余 <9300 端口一律拒绝（红线双保险）
            raise ValueError(
                f"cdp port must be {SHARED_CDP_PORT} (shared, attach-only) or >= {MIN_CDP_PORT} (got {port})"
            )
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
        self._owned_pages: list = []  # 记录该客户端主动创建的页面，close 时仅关闭自己打开的页面

    def page(self, url: str, timeout_ms: int = 30000):
        """取（或新建）一个页面并打开 url。

        - 共享端口（9222）：强制 new_page() 并跟踪生命周期，避免多 worker/并发客户端
          互相抢占同一个 about:blank tab 并打乱导航流（review P2）。
        - 独立端口（>=9300）：优先复用已有空白页，避免单账号专属浏览器 tab 无休止堆叠。
        """
        ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        if self.port == SHARED_CDP_PORT:
            page = ctx.new_page()
            self._owned_pages.append(page)
        else:
            page = next((p for p in ctx.pages if p.url in ("about:blank", "")), None)
            if page is None:
                page = ctx.new_page()
                self._owned_pages.append(page)
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return page

    def close(self) -> None:
        # 红线：绝不 browser.close()（那会杀掉用户的 Chrome 实例）
        # 但主动创建的 tab 应在任务结束时关闭，避免泄漏大量孤儿 tab
        for p in self._owned_pages:
            try:
                p.close()
            except Exception:
                pass
        self._owned_pages.clear()
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
