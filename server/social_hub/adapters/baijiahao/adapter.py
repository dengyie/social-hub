"""百家号适配器（CDP 通道，M2）：baijiahao.baidu.com 视频投稿。

流程参考 social-auto-upload baijiahao_uploader/main.py：视频文件选择（多级回退）、
上传后等待 contentEditable 标题区（180s，转码/表单挂载），再填标题。
未登录以「注册/登录百家号」文案判定。发布按钮优先 data-testid=publish-btn。
"""

from __future__ import annotations

from ..base import Capabilities
from ..cdp.base import CdpAdapterBase
from ..registry import register


class BaijiahaoAdapter(CdpAdapterBase):
    platform = "baijiahao"
    lane = "cdp"
    capabilities = Capabilities(text=False, image_text=False, video=True, markdown=False,
                                max_title=30, verifiable=False)  # SAU max_title_length=30
    login_url = "https://baijiahao.baidu.com/builder/theme/bjh/login"
    publish_url = "https://baijiahao.baidu.com/builder/rc/edit?type=videoV2"
    publish_button_text = "发布"
    login_redirect_marker = "/login"
    media_kinds = ("video",)  # 视频投稿：图片封面在提交前拒绝
    media_required = True
    video_ready_timeout = 180.0  # SAU：contentEditable 标题区 wait visible 180s

    selectors = {
        # social-auto-upload baijiahao_uploader；2026-09-13 真机实测 accept 实际值
        # 是 ".mp4, .mov, ..."（不含 "video" 字样），故 mp4 匹配为主、video 反而 MISS
        "media_input": "input[type='file'][accept*='mp4']",
        "media_input_alt": "input[type='file']",
        "title_input": "div[class*='contentEditable']",
        "publish_button": "[data-testid='publish-btn']",
    }
    login_markers = ("text:注册/登录百家号",)
    captcha_markers = ("text:百度安全验证",)  # SAU：安全中心弹窗

    def _flow(self, page, snap: dict, ctx) -> dict:
        media_sel = self.require_has(page, "media_input", "media_input_alt")
        self.upload(page, media_sel, [snap["cover_path"]])
        # 标题栏在上传完成后才渲染（SAU wait visible 180s）；瞬时 has 会竞态
        title_sel = self.require_has(page, "title_input", timeout=self.video_ready_timeout)
        self.type_text(page, title_sel, snap["title"])
        return {"content_url": None, "note_kind": "video"}


register(BaijiahaoAdapter())
