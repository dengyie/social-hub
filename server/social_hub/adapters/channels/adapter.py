"""微信视频号适配器（CDP 通道，M1）：channels.weixin.qq.com。

SAU tencent_uploader 真机结论：直接打开 /platform/post/create 会被跳回首页，
Vue 不挂载、页面上没有 input[type=file]。正确入口是先打开 /platform，再点可见的
「发表视频」按钮做客户端跳转。图文（TencentNote）上游仍是 NotImplemented，
image 媒体回退同一入口后走 file input。
登录 = 微信扫码（login_interactive 截二维码推 WebUI）；未登录跳 login.html。
"""

from __future__ import annotations

from ..base import Capabilities
from ..registry import register
from ..cdp.base import CdpAdapterBase


class ChannelsAdapter(CdpAdapterBase):
    platform = "channels"
    lane = "cdp"
    # SAU format_str_for_short_title：界面短标题 7~15 字；描述区另填，当前按描述口径 30
    capabilities = Capabilities(text=False, image_text=True, video=True, markdown=False,
                                max_title=30, verifiable=False)
    login_url = "https://channels.weixin.qq.com/platform/login"
    # SAU：先进首页再点「发表视频」；直开 /platform/post/create 表单不挂载
    publish_url = "https://channels.weixin.qq.com/platform"
    publish_button_text = "发表"
    login_redirect_marker = "login.html"  # SAU cookie_auth：失效后 JS 跳 **/login.html**
    media_kinds = ("video", "image")
    media_required = True

    selectors = {
        # SAU tencent_uploader + [calibrate] 回退
        "media_input": "input[type='file']",
        "desc_editor": "div.input-editor",  # SAU：视频描述
        "desc_editor_alt": "#post-content textarea, .ql-editor[contenteditable='true']",
        "short_title_input": "input[placeholder*='短标题']",
        "video_entry_text": "发表视频",
        "image_entry_text": "发表图文",  # 上游未实现；有则点，无则回退发表视频
    }
    login_markers = (".login__type__container__scan", "text:微信扫码登录")  # [calibrate]
    captcha_markers = ()  # 视频号无滑块，扫码即风控

    def _flow(self, page, snap: dict, ctx) -> dict:
        is_image = snap.get("cover_kind") == "image"
        entry = self.selectors["image_entry_text" if is_image else "video_entry_text"]
        if not self.click_exact_text(page, entry) and is_image:
            self.click_exact_text(page, self.selectors["video_entry_text"])
        media_sel = self.require_has(page, "media_input")
        self.upload(page, media_sel, [snap["cover_path"]])
        desc_sel = self.require_has(page, "desc_editor", "desc_editor_alt")
        self.type_text(page, desc_sel, snap["title"])
        if self.has(page, "short_title_input"):
            short = "".join(ch for ch in snap["title"] if ch.isalnum() or ch.isspace()).strip()
            short = (short or "精彩视频内容分享")[:15]
            if len(short) < 7:
                short = (short + "内容分享")[:15]
            self.fill(page, "short_title_input", short)
        return {"content_url": None, "note_kind": "image" if is_image else "video"}


register(ChannelsAdapter())
