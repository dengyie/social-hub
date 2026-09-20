"""微博适配器（CDP 通道，M3）：weibo.com 发布页，图文/视频。

参考 [[Note/AI/经验/国内社媒自动化发布工具对比]]：微博是国内社媒矩阵必备一档，
social-auto-upload 有微博上传器。本适配器为**预置待校准**（[calibrate]）——
未校准平台刻意只锚定结构性选择器（`input[type=file]` / `textarea[placeholder*=…]` /
可见文案按钮），不猜混淆 class；上线前用 `shub doctor --platform weibo --account A` 校准。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..cdp.base import CdpAdapterBase
from ..registry import register


class WeiboAdapter(CdpAdapterBase):
    platform = "weibo"
    lane = "cdp"
    calibrate_only = True  # 选择器未真机校准：fan-out 跳过，显式 shub publish 仍可试
    capabilities = Capabilities(image_text=True, video=True, markdown=False,
                                max_title=140, verifiable=False)
    login_url = "https://weibo.com/login.php"
    publish_url = "https://weibo.com/upload/channel"  # [calibrate] 发布页（图文/视频）
    publish_button_text = "发布"
    login_redirect_marker = "passport.weibo.com"  # [calibrate] 未登录跳微博统一 passport
    # 微博支持纯文字；带媒体时图文/视频均可
    media_kinds = ("image", "video")
    media_required = False

    selectors = {
        # 未校准平台只用结构锚点（改版存活率高），[calibrate] 待 doctor 收窄
        "media_input": "input[type='file']",
        "title_input": "textarea[placeholder*='新鲜事']",  # [calibrate] 微博正文框
        "body_editor": "div[contenteditable='true']",  # [calibrate] 富文本兜底
    }
    login_markers = ()  # 登录判定走 passport 重定向（页内"登录"文案误报率过高）
    captcha_markers = ("text:验证码",)  # [calibrate] 微博风控以验证码/滑动为主

    def _flow(self, page, snap: dict, ctx) -> dict:
        text = snap["title"] if not snap["body"] else f"{snap['title']}\n{snap['body']}"
        sel = self.first_has(page, "title_input", "body_editor")
        if not sel:
            raise PermanentError("weibo: 正文输入框未命中（选择器待校准）")
        if sel == "title_input":
            self.fill(page, "title_input", text)
        else:
            self.type_text(page, "body_editor", text)
        if snap["cover_path"]:
            self.upload(page, "media_input", [snap["cover_path"]])
        return {"content_url": None}


register(WeiboAdapter())