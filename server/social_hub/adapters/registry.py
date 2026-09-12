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
    try:
        return PLATFORMS[platform]
    except KeyError:
        raise KeyError(f"unknown platform: {platform} (registered: {sorted(PLATFORMS)})") from None


def registered_platforms() -> list[str]:
    return sorted(PLATFORMS)


def unregister(platform: str) -> None:  # 测试用
    PLATFORMS.pop(platform, None)


def load_builtin_adapters() -> None:
    from .gzh.adapter import GzhAdapter  # noqa: F401  延迟导入避免环
    from .mock import MockAdapter  # noqa: F401  参考/冒烟平台（模块导入即注册）

    register(GzhAdapter())
    register(MockAdapter())
