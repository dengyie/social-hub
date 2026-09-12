"""适配器契约（设计文档 §6.2）。

所有平台适配器实现本基类；核心代码零 `if platform ==` 分支。
错误分类决定状态机走向：Transient→退避重排 / NeedsLogin→needs_login /
CaptchaWait→captcha_wait / Permanent→failed。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AdapterError(Exception):
    """适配器错误基类。state = 触发后任务应迁往的状态。"""

    state = "failed"


class TransientError(AdapterError):
    """瞬时错误（限流/网络/上游 5xx）：退避重试。"""

    state = "queued"


class CredentialsError(AdapterError):
    """凭据失效/缺失（appid+secret 错误、token 无法恢复）：转 needs_login 等人工修复配置。"""

    state = "needs_login"


class NeedsLoginError(AdapterError):
    """登录态失效：转 needs_login 等人工/交互登录后重排。"""

    state = "needs_login"


class CaptchaWaitError(AdapterError):
    """遇到验证码/滑块：冻结等人处理（CDP 通道，M1+）。"""

    state = "captcha_wait"


class PermanentError(AdapterError):
    """永久错误（参数非法/内容不合规）：终态失败。"""

    state = "failed"


class UnsupportedActionError(PermanentError):
    """适配器未实现该动作（如 engage 在 M5 前的适配器上）。"""


@dataclass
class Capabilities:
    image_text: bool = False
    video: bool = False
    markdown: bool = False
    max_title: int = 64
    max_tags: int = 0
    platform_scheduling: bool = False  # 平台侧原生定时
    verifiable: bool = False  # 发布后能拿到凭证链接


@dataclass
class PublishResult:
    submitted: bool
    detail: dict = field(default_factory=dict)


@dataclass
class Evidence:
    ok: bool
    url: str | None = None
    raw: dict = field(default_factory=dict)


class ActionContext:
    """单次动作执行上下文：任务 + 账号 + 会话（编排器持有）+ 会话工厂。"""

    def __init__(self, task, account, session_factory, media_dir, session=None):
        self.task = task
        self.account = account
        self.session_factory = session_factory
        self.media_dir = media_dir
        self.session = session  # 编排器事务内会话；适配器可增量落证据后 commit

    def db(self):
        return self.session_factory()


class PlatformAdapter(ABC):
    platform: str = ""
    lane: str = "api"  # api | cdp
    capabilities: Capabilities = Capabilities()

    @abstractmethod
    def check_login(self, ctx: ActionContext) -> str:
        """返回 'ok'|'expired'|'unknown'；调用方缓存 >=12h（设计文档 §6.2）。"""

    def login_interactive(self, ctx: ActionContext) -> dict:
        raise UnsupportedActionError(f"{self.platform} 无交互登录（API 通道请用 account add 配置凭据）")

    def publish(self, ctx: ActionContext) -> PublishResult:
        raise UnsupportedActionError(f"{self.platform} 未实现 publish")

    def verify(self, ctx: ActionContext) -> Evidence:
        raise UnsupportedActionError(f"{self.platform} 未实现 verify")

    def engage(self, ctx: ActionContext) -> dict:
        """互动动作（comment/like/favorite/reply）：M5 互动引擎。"""
        raise UnsupportedActionError(f"{self.platform} 未实现 engage（M5）")

    def collect(self, ctx: ActionContext) -> dict:
        """创作者中心数据回收：M5。"""
        raise UnsupportedActionError(f"{self.platform} 未实现 collect（M5）")
