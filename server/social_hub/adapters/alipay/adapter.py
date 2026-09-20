"""支付宝生活号适配器（CDP 通道，M3）：c.alipay.com 内容创作平台。

参考 social-auto-upload alipay_uploader/main.py：入口必须带 `_appScene=CONTENT`
（否则跳生活号开通页）；点「发布视频…」卡片进入 short-video 表单。
本适配器为**预置待校准**（calibrate-only）——URL/占位符已对齐 SAU，
上线前用 `shub doctor --platform alipay --account A` 校准。
"""

from __future__ import annotations

from ..base import Capabilities
from ..cdp.base import CdpAdapterBase
from ..registry import register


class AlipayAdapter(CdpAdapterBase):
    platform = "alipay"
    lane = "cdp"
    calibrate_only = True  # 选择器未真机校准：fan-out 跳过，显式 shub publish 仍可试
    capabilities = Capabilities(image_text=True, video=True, markdown=False,
                                max_title=30, verifiable=False)
    login_url = "https://c.alipay.com/"
    # SAU：内容创作平台入口；缺 _appScene=CONTENT 会跳 signup
    publish_url = ("https://c.alipay.com/page/life-account/index"
                   "?_appScene=CONTENT&appId=2030022469359777")
    publish_button_text = "发布"  # SAU 主按钮「确认发布」含子串
    login_redirect_marker = "auth.alipay.com"
    media_kinds = ("video", "image")
    media_required = True

    selectors = {
        "media_input": "input[type='file']",
        "title_input": "input[placeholder*='一个好的标题']",
        "title_input_alt": "input[placeholder*='标题']",
        "body_editor": "textarea[placeholder*='填写作品描述']",
        "body_editor_alt": "div[contenteditable='true']",
        "video_entry_text": "发布视频推荐分辨率720p及以上，建议1080p",
    }
    login_markers = ()  # 登录判定走 auth.alipay.com 重定向；页内「登录」误报率过高
    captcha_markers = ("text:验证码",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        self.click_exact_text(page, self.selectors["video_entry_text"])
        self.upload(page, "media_input", [snap["cover_path"]])
        title_sel = self.require_has(page, "title_input", "title_input_alt", "body_editor",
                                    "body_editor_alt")
        if title_sel in ("title_input", "title_input_alt"):
            self.fill(page, title_sel, snap["title"])
        else:
            self.type_text(page, title_sel, snap["title"])
        return {"content_url": None}


register(AlipayAdapter())
