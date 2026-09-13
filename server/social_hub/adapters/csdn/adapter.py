"""CSDN 适配器（CDP 通道，M2）：mp.csdn.net 博客创作（Markdown 编辑器）。

未登录跳转 passport.csdn.net（URL 重定向判定）；编辑器 CodeMirror 6
（.cm-content），fill 无效 → click + 键盘注入；发布按钮文案「发布博客」[calibrate]。
"""

from __future__ import annotations

from ..base import Capabilities
from ..cdp.base import CdpAdapterBase
from ..registry import register


class CsdnAdapter(CdpAdapterBase):
    platform = "csdn"
    lane = "cdp"
    capabilities = Capabilities(image_text=True, markdown=True, max_title=100, verifiable=False)
    login_url = "https://passport.csdn.net/login"
    publish_url = "https://mp.csdn.net/mp_blog/creation/editor/new"
    publish_button_text = "发布博客"
    login_redirect_marker = "passport.csdn.net"

    selectors = {
        # [calibrate] CSDN 创作中心（CodeMirror 6 + 私有 UI 库）
        "title_input": "input[placeholder*='标题']",
        "body_editor": ".cm-content[contenteditable='true']",
    }
    login_markers = ("text:登录",)  # [calibrate] 过宽，配合 URL 重定向使用
    captcha_markers = ()

    def _flow(self, page, snap: dict, ctx) -> dict:
        self.fill(page, "title_input", snap["title"])
        self.type_text(page, "body_editor", snap["body"])
        return {"content_url": None}


register(CsdnAdapter())
