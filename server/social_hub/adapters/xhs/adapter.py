"""小红书适配器（CDP 通道，M1）。

创作平台 creator.xiaohongshu.com；图文笔记：图片必需、标题 ≤20 字、正文 QL 编辑器。
登录 = 扫码（login_interactive 截图二维码）；风控滑块 → captcha_wait。
选择器标注 [calibrate] 的项需要真机校准（shub doctor）。
"""

from __future__ import annotations

import json

from ..base import Capabilities, PermanentError
from ..registry import register
from ..cdp.base import CdpAdapterBase


class XhsAdapter(CdpAdapterBase):
    platform = "xhs"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, markdown=False, max_title=20, verifiable=False)
    login_url = "https://creator.xiaohongshu.com/login"
    publish_url = "https://creator.xiaohongshu.com/publish/publish?source=official"
    publish_button_text = "发布"

    selectors = {
        # [calibrate] 小红书发布页选择器，2026-09 依据公开资料预置，上线前用 shub doctor 校准
        "title_input": "#title-textarea input",
        "body_editor": ".ql-editor[contenteditable='true']",
        "image_input": "input[type='file']",
        "image_text_tab": ".channel-tab",  # [calibrate] 图文 tab
    }
    login_markers = ('input[placeholder*="扫码"]', ".login-container")  # [calibrate]
    captcha_markers = (".captcha-slider", "#captcha-container")  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"]:
            raise PermanentError("xhs 图文笔记需要至少 1 张图片（draft cover_media_id）")
        if self.has(page, "image_text_tab"):
            self.click(page, "image_text_tab")
        self.upload(page, "image_input", [snap["cover_path"]])
        self.fill(page, "title_input", snap["title"])
        self.type_text(page, "body_editor", snap["body"])
        return {"images": 1, "note_url": None}


register(XhsAdapter())
