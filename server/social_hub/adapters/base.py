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
    # 内容形态语义（review P2）：`text`=纯文字正文；`image_text`=正文中需上传图片；
    # `video`=视频投稿；`markdown`=Markdown 正文。至少声明一种，且要与 flow 实际承载一致——
    # 契约测试 check_capability_truthfulness 校验声明的媒体能力有对应媒体输入。
    text: bool = True
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

    def __init__(self, task, account, session_factory, media_dir, session=None, preview: bool = False):
        self.task = task
        self.account = account
        self.session_factory = session_factory
        self.media_dir = media_dir
        self.session = session  # 编排器事务内会话；适配器可增量落证据后 commit
        self.preview = preview  # True = 只填表不点发布（XiaohongshuSkills --preview）

    def db(self):
        return self.session_factory()


def load_variant_snapshot(ctx: ActionContext, *, max_title: int | None = None) -> dict:
    """变体快照唯一实现（此前 gzh/bili/juejin/cdp-base 各持一份拷贝）。

    payload→variant→字段快照，脱离 session 返回；标题上限可选（适配器能力驱动）。
    返回字段：id/title/body/tags/author/digest/cover_path/cover_kind。
    """
    import json

    from ..models import DraftVariant, Media

    payload = json.loads(ctx.task.payload or "{}")
    variant_id = payload.get("variant_id")
    if not variant_id:
        raise PermanentError("task payload missing variant_id")
    with ctx.db() as session:
        variant = session.get(DraftVariant, variant_id)
        if variant is None:
            raise PermanentError(f"variant #{variant_id} not found")
        media = session.get(Media, variant.cover_media_id) if variant.cover_media_id else None
        draft = variant.draft
        snap = {
            "id": variant.id,
            "title": variant.title,
            "body": variant.body,
            "tags": variant.tags,
            "author": draft.author if draft else None,
            "digest": draft.digest if draft else None,
            "cover_path": str(ctx.media_dir / media.path) if media else None,
            "cover_kind": media.kind if media else None,
        }
    if max_title is not None and len(snap["title"]) > max_title:
        raise PermanentError(f"title too long ({len(snap['title'])} > {max_title})")
    return snap


def require_media_kind(snap: dict, *, platform: str, kinds: tuple[str, ...] = (),
                       required: bool = False) -> None:
    """媒体 kind 校验的**唯一实现**（review P1 根因：双通道曾各写一份，易漏/易漂移）。

    - `kinds` 非空且已带媒体 → 媒体 kind 必须命中，否则拒绝（图片不会误投视频平台，反之亦然）。
    - `required=True` 且未带媒体 → 拒绝。
    - `kinds=()` 表示该平台不消费媒体（纯文本平台），此时忽略封面。

    调用点一律在触碰任何外部副作用（浏览器 / 平台 API）之前——fail-fast、零副作用。
    CDP 通道经 `CdpAdapterBase._validate_media` 调用；API 通道（gzh 图文封面 / bili 视频）
    直接调用。
    """
    cover, kind = snap.get("cover_path"), snap.get("cover_kind")
    if kinds and cover and kind not in kinds:
        raise PermanentError(f"{platform} 需要 kind={'/'.join(kinds)} 的媒体，got kind={kind}")
    if required and not cover:
        raise PermanentError(f"{platform} 需要媒体文件（draft cover_media_id）")


class PlatformAdapter(ABC):
    platform: str = ""
    lane: str = "api"  # api | cdp
    capabilities: Capabilities = Capabilities()
    # True = 选择器/URL 未真机校准：fan-out 跳过；显式 shub publish 仍可试
    calibrate_only: bool = False

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
