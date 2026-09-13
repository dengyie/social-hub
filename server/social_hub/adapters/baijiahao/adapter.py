"""百家号适配器（CDP 通道，M2）：baijiahao.baidu.com 视频投稿。

流程参考 social-auto-upload baijiahao_uploader/main.py：视频文件选择（多级回退）、
标题 contentEditable；未登录以「注册/登录百家号」文案判定。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..cdp.base import CdpAdapterBase
from ..registry import register


class BaijiahaoAdapter(CdpAdapterBase):
    platform = "baijiahao"
    lane = "cdp"
    capabilities = Capabilities(image_text=False, video=True, markdown=False, max_title=40, verifiable=False)
    login_url = "https://baijiahao.baidu.com/builder/theme/bjh/login"
    publish_url = "https://baijiahao.baidu.com/builder/rc/edit?type=videoV2"
    publish_button_text = "发布"

    selectors = {
        # social-auto-upload baijiahao_uploader（真机维护）
        "media_input": "input[type='file'][accept*='video']",
        "media_input_alt": "input[type='file'][accept*='mp4']",
        "media_input_alt2": "input[type='file']",
        "title_input": "div[class*='contentEditable']",
    }
    login_markers = ("text:注册/登录百家号",)
    captcha_markers = ()  # 百家号风控以百度账号安全中心弹窗为主 [calibrate]

    def _first_has(self, page, *names: str) -> str | None:
        for name in names:
            if self.selectors.get(name) and self.has(page, name):
                return name
        return None

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"]:
            raise PermanentError("baijiahao 视频投稿需要视频文件（media kind=video）")
        media_sel = self._first_has(page, "media_input", "media_input_alt", "media_input_alt2")
        if not media_sel:
            raise PermanentError("baijiahao: file input not found（选择器待校准）")
        self.upload(page, media_sel, [snap["cover_path"]])
        if self.has(page, "title_input"):
            self.type_text(page, "title_input", snap["title"])
        return {"content_url": None}


register(BaijiahaoAdapter())
