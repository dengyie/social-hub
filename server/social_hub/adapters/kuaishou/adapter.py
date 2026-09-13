"""快手适配器（CDP 通道，M2）：创作者中心视频上传 cp.kuaishou.com。

流程参考 social-auto-upload ks_uploader/main.py：视频上传 → 「描述」富文本 → 发布 →
ant-modal 确认弹窗（confirm_button_text）。登录态以「立即登录」文案判定；
cookie 失效标记 `div.names …:text('机构服务')` 为 playwright 伪类，此处以 URL 跳转兜底。
"""

from __future__ import annotations

from ..base import Capabilities, PermanentError
from ..cdp.base import CdpAdapterBase
from ..registry import register


class KuaishouAdapter(CdpAdapterBase):
    platform = "kuaishou"
    lane = "cdp"
    capabilities = Capabilities(image_text=False, video=True, markdown=False, max_title=20, verifiable=False)
    login_url = ("https://passport.kuaishou.com/pc/account/login/"
                 "?sid=kuaishou.web.cp.api&callback=https%3A%2F%2Fcp.kuaishou.com")
    publish_url = "https://cp.kuaishou.com/article/publish/video"
    publish_button_text = "发布"
    confirm_button_text = "确认"  # ant-modal-confirm-centered 主按钮

    selectors = {
        # social-auto-upload ks_uploader（真机维护 + [calibrate] 兜底）
        "media_input": "input[type='file']",
        "desc_editor": "div[contenteditable='true']",  # [calibrate] 真实结构锚定「描述」label 多级回退
        "upload_btn": "button[class^='_upload-btn']",  # [calibrate]
    }
    login_markers = ("text:立即登录",)
    captcha_markers = ("text:验证码",)  # [calibrate]

    def _flow(self, page, snap: dict, ctx) -> dict:
        if not snap["cover_path"]:
            raise PermanentError("kuaishou 视频投稿需要视频文件（media kind=video）")
        if self.has(page, "upload_btn"):
            self.click(page, "upload_btn")
        self.upload(page, "media_input", [snap["cover_path"]])
        if not self.has(page, "desc_editor"):
            raise PermanentError("kuaishou: desc editor not found（选择器待校准，SAU 锚定「描述」label）")
        self.type_text(page, "desc_editor", snap["title"])
        return {"content_url": None}


register(KuaishouAdapter())
