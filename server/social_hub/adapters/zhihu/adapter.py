"""知乎适配器（CDP 通道，M1）：专栏文章 zhuanlan.zhihu.com/write。

正文为 DraftJS 富文本（contenteditable），fill 无效 → type_text 键盘注入。
标题上限 100 字；发布后默认回执核验（页面级核验待真机校准后覆写）。
"""

from __future__ import annotations

from ..base import Capabilities
from ..registry import register
from ..cdp.base import CdpAdapterBase


class ZhihuAdapter(CdpAdapterBase):
    platform = "zhihu"
    lane = "cdp"
    capabilities = Capabilities(image_text=False, markdown=False, max_title=100, verifiable=False)
    login_url = "https://www.zhihu.com/signin"
    publish_url = "https://zhuanlan.zhihu.com/write"
    publish_button_text = "发布"
    # 专栏文章为纯文字写作页：flow 无媒体上传路径，故不声明媒体能力，
    # 也不接受封面（media_kinds 空 = 忽略封面，避免误导用户以为会带图）
    media_kinds = ()
    media_required = False

    selectors = {
        # [calibrate] 知乎写作页选择器，上线前用 shub doctor 校准
        "title_input": "textarea[placeholder*='标题']",
        "body_editor": ".public-DraftEditor-content[contenteditable='true']",
    }
    login_markers = (".SignFlowHomepage",)  # [calibrate] 登录浮层容器
    captcha_markers = (".Captcha", ".captcha-container")  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        title_sel = self.require_has(page, "title_input")
        self.fill(page, title_sel, snap["title"])
        editor_sel = self.require_has(page, "body_editor")
        self.type_text(page, editor_sel, snap["body"])
        return {"article_url": None}


register(ZhihuAdapter())
