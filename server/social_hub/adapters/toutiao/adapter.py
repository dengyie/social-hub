"""今日头条适配器（CDP 通道，M2）：mp.toutiao.com 图文/视频。

对比文档结论：头条无万星级开源方案（SAU 亦无），全部选择器 [calibrate]——
预置自头条创作平台公开结构（ProseMirror 系编辑器），上线前必须 shub doctor 校准。
"""

from __future__ import annotations

from ..base import Capabilities
from ..cdp.base import CdpAdapterBase
from ..registry import register


class ToutiaoAdapter(CdpAdapterBase):
    platform = "toutiao"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, video=True, markdown=False, max_title=30, verifiable=False)
    login_url = "https://mp.toutiao.com/auth/page/login"
    publish_url = "https://mp.toutiao.com/profile_v4/graphic/publish"  # 图文；视频走 video_publish_url
    _video_publish_url = "https://mp.toutiao.com/profile_v4/video/publish"  # [calibrate]
    publish_button_text = "发布"
    # 图文/视频两页；图文可纯文字（无封面），视频必须有视频媒体 → 按 kind 分流 URL
    media_kinds = ("image", "video")
    media_required = False

    selectors = {
        # 2026-09-13 home-win 真机校准：标题为 textarea（占位文案「请输入文章标题（2～30个字）」），
        # 正文 .ProseMirror[contenteditable=true] 已 HIT；media_input 在图文页不渲染（视频页才有）
        "title_input": "textarea[placeholder*='标题']",
        "title_input_alt": "input[placeholder*='标题']",
        "body_editor": ".ProseMirror[contenteditable='true']",
        "body_editor_video": "div[contenteditable='true']",  # [calibrate] 视频描述编辑器
        "media_input": "input[type='file']",
    }
    login_markers = ("text:扫码登录",)  # [calibrate]
    captcha_markers = (".captcha_verify_container",)  # [calibrate]

    def _resolve_publish_url(self, snap: dict) -> str:
        if snap.get("cover_kind") == "video":
            return self._video_publish_url
        return self.publish_url

    def _flow(self, page, snap: dict, ctx) -> dict:
        title_sel = self.require_has(page, "title_input", "title_input_alt")
        self.fill(page, title_sel, snap["title"])
        is_video = snap.get("cover_kind") == "video"
        editor = "body_editor_video" if is_video else "body_editor"
        editor_sel = self.require_has(page, editor)
        self.type_text(page, editor_sel, snap["body"])
        if snap["cover_path"]:
            media_sel = self.require_has(page, "media_input")
            self.upload(page, media_sel, [snap["cover_path"]])
            if is_video:
                self.first_has(page, editor, timeout=30.0)
        return {"content_url": None}


register(ToutiaoAdapter())
