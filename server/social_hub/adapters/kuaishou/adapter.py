"""快手适配器（CDP 通道，M2）：创作者中心 cp.kuaishou.com。

流程参考 social-auto-upload ks_uploader/main.py：
- 视频：上传按钮 → file input → 有界等待「描述」编辑区（上传/转码就绪）→ 发布 → ant-modal 确认
- 图文：切 role=tab「图文」→ 「上传图片」按钮 → 同样填描述 → 发布 → 「确认」/「确认发布」
登录态以「立即登录」文案判定；cookie 失效标记 `div.names …:text('机构服务')` 为
playwright 伪类，此处以 URL 跳转兜底。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..cdp.base import CdpAdapterBase
from ..registry import register


class KuaishouAdapter(CdpAdapterBase):
    platform = "kuaishou"
    lane = "cdp"
    capabilities = Capabilities(text=False, image_text=True, video=True, markdown=False,
                                max_title=20, verifiable=False)
    login_url = ("https://passport.kuaishou.com/pc/account/login/"
                 "?sid=kuaishou.web.cp.api&callback=https%3A%2F%2Fcp.kuaishou.com")
    publish_url = "https://cp.kuaishou.com/article/publish/video"
    publish_button_text = "发布"
    confirm_button_text = "确认"  # ant-modal 主按钮；图文「确认发布」含子串
    login_redirect_marker = "passport.kuaishou.com"
    media_kinds = ("video", "image")
    media_required = True
    video_ready_timeout = 120.0  # SAU 上传中轮询上限 480s；标题/描述挂载即可填

    selectors = {
        # social-auto-upload ks_uploader（真机维护 + [calibrate] 兜底）
        "media_input": "input[type='file']",
        "desc_editor": "div[contenteditable='true']",  # [calibrate] 真实结构锚定「描述」label
        "upload_btn": "button[class^='_upload-btn']",  # [calibrate]
        "image_tab_text": "图文",  # SAU KSNote：div[role=tab]:has-text("图文")
    }
    login_markers = ("text:立即登录",)
    captcha_markers = ("text:验证码",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        is_image = snap.get("cover_kind") == "image"
        if is_image:
            self.click_exact_text(page, self.selectors["image_tab_text"])
        if self.has(page, "upload_btn"):
            self.click(page, "upload_btn")
        self.upload(page, "media_input", [snap["cover_path"]])
        wait = 15.0 if is_image else self.video_ready_timeout
        if not self.first_has(page, "desc_editor", timeout=wait):
            raise PermanentError(
                "kuaishou: desc editor not found（选择器待校准，SAU 锚定「描述」label）")
        self.type_text(page, "desc_editor", snap["title"])
        return {"content_url": None, "note_kind": "image" if is_image else "video"}


register(KuaishouAdapter())
