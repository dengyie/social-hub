"""抖音适配器（CDP 通道，M1）：创作者中心图文/视频上传 creator.douyin.com。

视频上传走 biliup 式两段？否——抖音无开放上传 API，CDP 表单流。
标题（文案）上限 55 字（图文备注口径）；登录 = 扫码（抖音 App）。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..registry import register
from ..cdp.base import CdpAdapterBase


class DouyinAdapter(CdpAdapterBase):
    platform = "douyin"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, video=True, markdown=False, max_title=55, verifiable=False)
    login_url = "https://creator.douyin.com/login"
    publish_url = "https://creator.douyin.com/creator-micro/content/upload"
    publish_button_text = "发布"

    selectors = {
        # [calibrate] 抖音创作者中心选择器，上线前用 shub doctor 校准
        "media_input": "input[type='file']",
        "title_editor": ".notranslate[contenteditable='true']",
        "body_editor": ".zone-container[contenteditable='true']",
    }
    login_markers = (".web-login", 'div[class*="login"] [class*="qrcode"]')  # [calibrate]
    captcha_markers = (".captcha-verify-container",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        import json as _json

        if not snap["cover_path"] and not snap["body"]:
            raise PermanentError("douyin 需要图片（图文）或正文文案")
        if snap["cover_path"]:
            self.upload(page, "media_input", [snap["cover_path"]])
        self.type_text(page, "title_editor", snap["title"])
        if self.has(page, "body_editor"):
            self.type_text(page, "body_editor", snap["body"])
        return {"content_url": None, "variant_tags": _json.loads(snap["tags"] or "[]")}


register(DouyinAdapter())
