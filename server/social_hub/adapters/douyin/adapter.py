"""抖音适配器（CDP 通道，M1）：创作者中心图文/视频上传 creator.douyin.com。

选择器来自 social-auto-upload douyin_uploader/main.py（真机维护）：
标题 `input[placeholder*="填写作品标题"]`、正文 `div.zone-container[contenteditable]`、
登录面板 .web-login / 文案「扫码登录」「手机号登录」。
"""

from __future__ import annotations

import json as _json

from ..base import Capabilities, PermanentError
from ..cdp.base import CdpAdapterBase
from ..registry import register


class DouyinAdapter(CdpAdapterBase):
    platform = "douyin"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, video=True, markdown=False, max_title=55, verifiable=False)
    login_url = "https://creator.douyin.com/login"
    publish_url = "https://creator.douyin.com/creator-micro/content/upload"
    publish_button_text = "发布"

    selectors = {
        # social-auto-upload douyin_uploader（真机维护）
        "title_input": "input[placeholder*='填写作品标题']",
        "body_editor": "div.zone-container[contenteditable='true']",
        "media_input": "input[type='file']",
    }
    login_markers = (".web-login", "text:扫码登录", "text:手机号登录")
    captcha_markers = (".captcha-verify-container",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"] and not snap["body"]:
            raise PermanentError("douyin 需要图片（图文）或正文文案")
        if snap["cover_path"]:
            self.upload(page, "media_input", [snap["cover_path"]])
        if self.has(page, "title_input"):
            self.fill(page, "title_input", snap["title"])
        if self.has(page, "body_editor"):
            self.type_text(page, "body_editor", snap["body"])
        return {"content_url": None, "variant_tags": _json.loads(snap["tags"] or "[]")}


register(DouyinAdapter())
