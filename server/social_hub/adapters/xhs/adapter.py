"""小红书适配器（CDP 通道，M1）。

创作平台 creator.xiaohongshu.com；图文笔记：图片必需、标题 ≤20 字。
选择器来自 XiaohongshuSkills（2026-03 真机校准，GitHub white0dew/XiaohongshuSkills
scripts/cdp_publish.py SELECTORS），含多级回退；未登录以 URL 重定向 /login 判定。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..cdp.base import CdpAdapterBase
from ..registry import register


class XhsAdapter(CdpAdapterBase):
    platform = "xhs"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, markdown=False, max_title=20, verifiable=False)
    login_url = "https://creator.xiaohongshu.com/login"
    publish_url = "https://creator.xiaohongshu.com/publish/publish?source=official"
    login_redirect_marker = "/login"  # XiaohongshuSkills：未登录跳 /login

    selectors = {
        # XiaohongshuSkills SELECTORS（2026-03 真机验证）
        "title_input": "div.d-input input",
        "title_input_alt": "input[placeholder*='标题']",
        "content_editor": "div.tiptap.ProseMirror",
        "content_editor_alt": "div.ProseMirror[contenteditable='true']",
        "content_editor_alt2": "div.ql-editor",
        "image_input": ".upload-input",
        "image_input_alt": "input[type='file']",
        "image_tab_text": "上传图文",
        "publish_button": ".publish-page-publish-btn button.bg-red",
        "image_preview_items": ".img-preview-area .pr",
    }
    login_markers = (".login-container",)  # [calibrate] 兜底
    captcha_markers = (".captcha-slider", "#captcha-container")  # [calibrate]

    def _first_has(self, page, *names: str) -> str | None:
        for name in names:
            if self.selectors.get(name) and self.has(page, name):
                return name
        return None

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"]:
            raise PermanentError("xhs 图文笔记需要至少 1 张图片（draft cover_media_id）")
        self.click_exact_text(page, self.selectors["image_tab_text"])  # 单 tab 态容错（点不到忽略）
        self.upload(page, "image_input" if self.has(page, "image_input") else "image_input_alt",
                    [snap["cover_path"]])
        title_sel = self._first_has(page, "title_input", "title_input_alt")
        if not title_sel:
            raise PermanentError("xhs: title input not found（选择器待校准）")
        self.fill(page, title_sel, snap["title"])
        editor_sel = self._first_has(page, "content_editor", "content_editor_alt", "content_editor_alt2")
        if not editor_sel:
            raise PermanentError("xhs: content editor not found（选择器待校准）")
        self.type_text(page, editor_sel, snap["body"])
        return {"images": 1, "note_url": None}


register(XhsAdapter())
