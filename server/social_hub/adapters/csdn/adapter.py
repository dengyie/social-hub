"""CSDN 适配器（CDP 通道，M2）：mp.csdn.net 博客创作（StackEdit 系 Markdown 编辑器）。

未登录跳转 passport.csdn.net（URL 重定向判定）；编辑器为 StackEdit 系
pre.editor__inner contenteditable，fill 无效 → click + 键盘注入；发布按钮文案「发布博客」[calibrate]。
"""

from __future__ import annotations

from ..base import Capabilities
from ..cdp.base import CdpAdapterBase
from ..registry import register


class CsdnAdapter(CdpAdapterBase):
    platform = "csdn"
    lane = "cdp"
    capabilities = Capabilities(image_text=False, markdown=True, max_title=100, verifiable=False)
    login_url = "https://passport.csdn.net/login"
    publish_url = "https://mp.csdn.net/mp_blog/creation/editor/new"
    publish_button_text = "发布博客"
    login_redirect_marker = "passport.csdn.net"
    # 博客为纯 Markdown 文本写作：flow 无媒体上传路径，媒体能力不声明
    media_kinds = ()
    media_required = False

    selectors = {
        # 2026-09-13 home-win 真机校准：编辑器实为 StackEdit 系（发布页 302 到
        # editor.csdn.net/md/?not_checkout=1），contenteditable 容器是 pre.editor__inner；
        # 标题为 input.article-bar__title（placeholder 含「标题」）
        "title_input": "input[placeholder*='标题']",
        "body_editor": "pre.editor__inner",
        "body_editor_alt": ".cm-content[contenteditable='true']",
    }
    login_markers = ()  # 登录判定走 login_redirect_marker（passport.csdn.net）；页内"登录"文案误报率过高
    captcha_markers = ()

    def _flow(self, page, snap: dict, ctx) -> dict:
        title_sel = self.require_has(page, "title_input")
        self.fill(page, title_sel, snap["title"])
        editor_sel = self.require_has(page, "body_editor", "body_editor_alt")
        self.type_text(page, editor_sel, snap["body"])
        return {"content_url": None}


register(CsdnAdapter())
