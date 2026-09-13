"""微信视频号适配器（CDP 通道，M1）：channels.weixin.qq.com/platform/post。

登录 = 微信扫码（login_interactive 截二维码推 WebUI）；描述上限 30 字左右口径。
视频/图片上传 input[type=file]；发表按钮文案「发表」。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..registry import register
from ..cdp.base import CdpAdapterBase


class ChannelsAdapter(CdpAdapterBase):
    platform = "channels"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, video=True, markdown=False, max_title=30, verifiable=False)
    login_url = "https://channels.weixin.qq.com/platform/login"
    publish_url = "https://channels.weixin.qq.com/platform/post/content"
    publish_button_text = "发表"

    selectors = {
        # [calibrate] 视频号助手选择器，上线前用 shub doctor 校准
        "media_input": "input[type='file']",
        "desc_editor": "#post-content textarea, .ql-editor[contenteditable='true']",  # [calibrate]
    }
    login_markers = (".login__type__container__scan",)  # [calibrate] 扫码登录容器
    captcha_markers = ()  # 视频号无滑块，扫码即风控

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"]:
            raise PermanentError("视频号发布需要视频/图片文件（draft cover_media_id 指向媒体）")
        self.upload(page, "media_input", [snap["cover_path"]])
        self.type_text(page, "desc_editor", snap["title"])
        return {"content_url": None}


register(ChannelsAdapter())
