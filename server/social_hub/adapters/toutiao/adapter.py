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
    publish_url = "https://mp.toutiao.com/profile_v4/graphic/publish"  # 图文；视频切 type 参数 [calibrate]
    publish_button_text = "发布"

    selectors = {
        # [calibrate] 头条创作平台，参考 ProseMirror 系编辑器通用结构预置
        "title_input": "input[placeholder*='标题']",
        "body_editor": ".ProseMirror[contenteditable='true']",
        "media_input": "input[type='file']",
    }
    login_markers = ("text:扫码登录",)  # [calibrate]
    captcha_markers = (".captcha_verify_container",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        self.fill(page, "title_input", snap["title"])
        self.type_text(page, "body_editor", snap["body"])
        if snap["cover_path"]:
            self.upload(page, "media_input", [snap["cover_path"]])
        return {"content_url": None}


register(ToutiaoAdapter())
