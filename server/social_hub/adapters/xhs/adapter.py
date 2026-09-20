"""小红书适配器（CDP 通道，M1）。

创作平台 creator.xiaohongshu.com；图文 + 视频笔记。
图文：切「上传图文」+ accept=jpg；视频：URL `target=video`（SAU）+ 切「上传视频」
（XiaohongshuSkills）+ accept=mp4，上传后有界等待标题框
（Skills VIDEO_PROCESS_TIMEOUT=120s，不 sleep）。
选择器来自 XiaohongshuSkills（2026-03 真机校准）与 SAU xiaohongshu_uploader。
未登录以 URL 重定向 /login 判定。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..base import Capabilities
from ..cdp.base import CdpAdapterBase
from ..registry import register


class XhsAdapter(CdpAdapterBase):
    platform = "xhs"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, video=True, markdown=False,
                                max_title=20, verifiable=False)
    login_url = "https://creator.xiaohongshu.com/login"
    publish_url = "https://creator.xiaohongshu.com/publish/publish?source=official"
    login_redirect_marker = "/login"  # XiaohongshuSkills：未登录跳 /login
    media_kinds = ("image", "video")
    media_required = True
    # XiaohongshuSkills VIDEO_PROCESS_TIMEOUT：视频转码后标题框才挂载
    video_ready_timeout = 120.0

    selectors = {
        # XiaohongshuSkills SELECTORS（2026-03 真机验证）+ SAU 回退
        "title_input": "div.d-input input",
        "title_input_alt": "input[placeholder*='标题']",
        "content_editor": "div.tiptap.ProseMirror",
        "content_editor_alt": "div.ProseMirror[contenteditable='true']",
        "content_editor_alt2": "div.ql-editor",
        "image_input": ".upload-input[accept*='.jpg']",
        "image_input_alt": ".upload-input",
        "image_input_alt2": "input[type='file']",
        "video_input": ".upload-input[accept*='.mp4']",
        "video_input_alt": "div[class^='upload-content'] input.upload-input",
        "image_tab_text": "上传图文",
        "video_tab_text": "上传视频",
        "publish_button": ".publish-page-publish-btn button.bg-red",
        "image_preview_items": ".img-preview-area .pr",
    }
    login_markers = (".login-container",)  # [calibrate] 兜底
    captcha_markers = (".captcha-slider", "#captcha-container")  # [calibrate]

    def _resolve_publish_url(self, snap: dict) -> str:
        # SAU：同一发布页用 target=image|video 分流，再叠加 Skills 的 tab 点击
        target = "video" if snap.get("cover_kind") == "video" else "image"
        parts = urlsplit(self.publish_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["target"] = target
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(query), parts.fragment))

    def _flow(self, page, snap: dict, ctx) -> dict:
        is_video = snap.get("cover_kind") == "video"
        tab_key = "video_tab_text" if is_video else "image_tab_text"
        self.click_exact_text(page, self.selectors[tab_key])  # 单 tab 态容错
        if is_video:
            media_sel = self.require_has(page, "video_input", "video_input_alt",
                                         "image_input_alt2")
        else:
            media_sel = self.require_has(page, "image_input", "image_input_alt")
        self.upload(page, media_sel, [snap["cover_path"]])
        # 切 tab/上传后表单异步渲染；视频另等转码（标题框出现 = 就绪信号，同 SAU）
        wait = self.video_ready_timeout if is_video else 15.0
        title_sel = self.require_has(page, "title_input", "title_input_alt",
                                     timeout=wait)
        self.fill(page, title_sel, snap["title"])
        editor_sel = self.require_has(
            page, "content_editor", "content_editor_alt", "content_editor_alt2")
        self.type_text(page, editor_sel, snap["body"])
        return {"images": 0 if is_video else 1,
                "note_kind": "video" if is_video else "image",
                "note_url": None}


register(XhsAdapter())
