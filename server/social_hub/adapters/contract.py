"""适配器契约测试套件（设计文档 §6.9）。

所有平台适配器必须全绿——新平台接入 = 契约测试通过。
元数据检查是静态的；行为检查通过注入的 ctx + 断言钩子完成，具体平台测试负责构造 ctx。
"""

from __future__ import annotations

import pytest

from .base import ActionContext, Capabilities, Evidence, PermanentError, PlatformAdapter, PublishResult, UnsupportedActionError

VALID_LANES = {"api", "cdp"}

# CDP 平台声明媒体能力（image_text/video）时，必须注册的媒体输入选择器键——
# review P2：防止像 zhihu/csdn 曾声明 image_text 却无任何媒体上传路径的"声明失真"。
MEDIA_SELECTOR_KEYS = {"media_input", "media_input_alt", "image_input",
                       "image_input_alt", "image_input_alt2",
                       "video_input", "video_input_alt"}


def check_metadata(adapter: PlatformAdapter) -> None:
    assert adapter.platform and adapter.platform.replace("_", "").isalnum(), "platform 名非法"
    assert adapter.lane in VALID_LANES, f"lane 必须是 {VALID_LANES}"
    assert isinstance(adapter.capabilities, Capabilities), "capabilities 必须是 Capabilities 实例"
    assert adapter.capabilities.max_title > 0


def check_capability_truthfulness(adapter: PlatformAdapter) -> None:
    """能力声明真实性（设计 §6.9）：声明了内容形态，flow 就得有对应承载。

    - 至少声明一种内容形态（text/image_text/video/markdown 全 False = 配置错误）。
    - CDP 通道声明 image_text/video → 必须注册媒体输入选择器（有可执行的上传路径）。
    - media_kinds（媒体 kind 约束）必须与声明的能力一致。
    """
    caps = adapter.capabilities
    assert any((caps.text, caps.image_text, caps.video, caps.markdown)), \
        f"{adapter.platform}: capabilities 至少声明一种内容形态"
    if adapter.lane == "cdp" and (caps.image_text or caps.video):
        keys = set(getattr(adapter, "selectors", {}) or {})
        assert keys & MEDIA_SELECTOR_KEYS, \
            f"{adapter.platform}: 声明了媒体能力（image_text/video）但未注册媒体输入选择器"
    kinds = tuple(getattr(adapter, "media_kinds", ()) or ())
    if kinds:
        assert caps.video or caps.image_text, \
            f"{adapter.platform}: media_kinds={kinds} 声明了媒体输入但 capabilities 未声明媒体能力"
        if "video" in kinds:
            assert caps.video, f"{adapter.platform}: media_kinds 含 video 但 capabilities.video=False"
        if "image" in kinds and not caps.image_text:
            assert caps.video, f"{adapter.platform}: media_kinds 含 image 但 capabilities 未声明 image_text/video"


def check_unsupported_actions(adapter: PlatformAdapter, ctx: ActionContext) -> None:
    with pytest.raises(UnsupportedActionError):
        adapter.engage(ctx)
    with pytest.raises(UnsupportedActionError):
        adapter.collect(ctx)


def check_publish_contract(adapter: PlatformAdapter, ctx: ActionContext) -> None:
    """happy path：publish 提交成功 → verify 拿到凭证（若声明 verifiable）。"""
    result = adapter.publish(ctx)
    assert isinstance(result, PublishResult) and result.submitted
    if adapter.capabilities.verifiable:
        evidence = adapter.verify(ctx)
        assert isinstance(evidence, Evidence) and evidence.ok
        assert evidence.url, "verifiable 平台必须返回发布凭证 url"


def check_title_limit_enforced(adapter: PlatformAdapter, ctx_factory) -> None:
    """超长标题必须在提交前被拒绝（不产生平台侧副作用）。"""
    caps = adapter.capabilities
    ctx = ctx_factory(title="标题" * (caps.max_title // 2 + 5))
    with pytest.raises(PermanentError):
        adapter.publish(ctx)


def run_contract_suite(adapter: PlatformAdapter, ctx: ActionContext, ctx_factory) -> None:
    check_metadata(adapter)
    check_capability_truthfulness(adapter)
    check_unsupported_actions(adapter, ctx)
    check_publish_contract(adapter, ctx)
    check_title_limit_enforced(adapter, ctx_factory)
