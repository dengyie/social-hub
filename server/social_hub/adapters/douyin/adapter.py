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
    # 图文视频均可，也允许纯文本作品（抖音支持无图纯文案）；给媒体时必须 kind 合法
    media_kinds = ("image", "video")
    media_required = False

    selectors = {
        # social-auto-upload douyin_uploader（真机维护）
        "title_input": "input[placeholder*='填写作品标题']",
        "body_editor": "div.zone-container[contenteditable='true']",
        "media_input": "input[type='file']",
        "image_input": "input[accept*='image']",
        "image_tab_text": "发布图文",  # SAU DouYinNote：图文走独立 tab
    }
    login_markers = (".web-login", "text:扫码登录", "text:手机号登录")
    captcha_markers = (".captcha-verify-container",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"] and not snap["body"]:
            raise PermanentError("douyin 需要图片（图文）或正文文案")
        is_image = snap.get("cover_kind") == "image"
        if is_image:
            self.click_exact_text(page, self.selectors["image_tab_text"])
        if snap["cover_path"]:
            media_sel = ("image_input" if is_image and self.has(page, "image_input")
                         else "media_input")
            self.upload(page, media_sel, [snap["cover_path"]])
        # 上传后表单由 SPA 异步挂载：瞬时 has() 会竞态（小红书同因，走 first_has）
        form_sel = self.require_has(page, "title_input", "body_editor")
        if form_sel == "title_input" or self.has(page, "title_input"):
            self.fill(page, "title_input", snap["title"])
        if self.has(page, "body_editor"):
            self.type_text(page, "body_editor", snap["body"])
        kind = ("image" if is_image
                else ("video" if snap.get("cover_kind") == "video" else "text"))
        return {"content_url": None, "variant_tags": _json.loads(snap["tags"] or "[]"),
                "note_kind": kind}


register(DouyinAdapter())
