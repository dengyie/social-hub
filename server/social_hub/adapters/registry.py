"""适配器注册表：核心代码取用适配器的唯一入口（设计文档 §6.2）。"""

from __future__ import annotations

from .base import PlatformAdapter

PLATFORMS: dict[str, PlatformAdapter] = {}


def register(adapter: PlatformAdapter) -> PlatformAdapter:
    if not adapter.platform:
        raise ValueError("adapter.platform is empty")
    PLATFORMS[adapter.platform] = adapter
    return adapter


def get_adapter(platform: str) -> PlatformAdapter:
    """未知平台统一抛 ValueError（调用方 400 语义），不泄漏 dict 访问语义的 KeyError。"""
    try:
        return PLATFORMS[platform]
    except KeyError:
        raise ValueError(f"unknown platform: {platform} (registered: {sorted(PLATFORMS)})") from None


def registered_platforms() -> list[str]:
    return sorted(PLATFORMS)


def unregister(platform: str) -> None:  # 测试用
    PLATFORMS.pop(platform, None)


def load_builtin_adapters() -> None:
    from .alipay.adapter import AlipayAdapter  # noqa: F401
    from .baijiahao.adapter import BaijiahaoAdapter  # noqa: F401
    from .bili.adapter import BiliAdapter  # noqa: F401
    from .channels.adapter import ChannelsAdapter  # noqa: F401
    from .csdn.adapter import CsdnAdapter  # noqa: F401
    from .douyin.adapter import DouyinAdapter  # noqa: F401
    from .gzh.adapter import GzhAdapter  # noqa: F401
    from .juejin.adapter import JuejinAdapter  # noqa: F401
    from .kuaishou.adapter import KuaishouAdapter  # noqa: F401
    from .mock import MockAdapter  # noqa: F401  参考/冒烟平台（模块导入即注册）
    from .toutiao.adapter import ToutiaoAdapter  # noqa: F401
    from .weibo.adapter import WeiboAdapter  # noqa: F401
    from .xhs.adapter import XhsAdapter  # noqa: F401
    from .zhihu.adapter import ZhihuAdapter  # noqa: F401  CDP 平台延迟导入 playwright（attach 时）

    for adapter in (GzhAdapter(), BiliAdapter(), JuejinAdapter(),
                    XhsAdapter(), ZhihuAdapter(), DouyinAdapter(), ChannelsAdapter(),
                    KuaishouAdapter(), BaijiahaoAdapter(), ToutiaoAdapter(), CsdnAdapter(),
                    WeiboAdapter(), AlipayAdapter(),
                    MockAdapter()):
        register(adapter)
