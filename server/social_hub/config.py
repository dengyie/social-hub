"""配置：环境变量优先、默认值兜底；数据目录约定 ~/.social-hub/。"""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOCIAL_HUB_", env_file=".env", extra="ignore")

    data_dir: Path = Path.home() / ".social-hub"
    db_url: str = ""  # 空则用 data_dir/social-hub.db
    api_token: str = ""  # 空 = 仅 loopback 放行
    lease_seconds: int = 120
    workers: int = 1
    port: int = 8767
    max_retries: int = 3
    chrome_bin: str = ""  # 空 = 自动探测（CDP 通道）
    biliup_bin: str = "biliup-rs"  # B站通道外部二进制（仅个人使用，禁商用，见设计文档 §2）
    cdp_connect_timeout: int = 15

    @property
    def db_path(self) -> Path:
        return self.data_dir / "social-hub.db"

    @property
    def database_url(self) -> str:
        return self.db_url or f"sqlite:///{self.db_path.as_posix()}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def chrome_profiles_dir(self) -> Path:
        return self.data_dir / "chrome-profiles"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.logs_dir, self.media_dir, self.data_dir / "backups"):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings() -> Settings:  # 测试用
    global _settings
    _settings = None
    return get_settings()


def generate_token() -> str:
    return f"shub_{secrets.token_urlsafe(24)}_{__version__}"


MIN_CDP_PORT: int = 9300  # 按账号独立实例的独占端口段下限
SHARED_CDP_PORT: int = 9222  # daily-checkin 共享浏览器（mac 方案）：attach-only，绝不代启/代关
