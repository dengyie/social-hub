"""账号保险库服务：创建/查询/解密凭据。平台合法性由适配器注册表校验。"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.registry import get_adapter
from ..config import MIN_CDP_PORT, SHARED_CDP_PORT, get_settings
from ..core.state import utcnow
from ..models import Account, DEFAULT_RATE_LIMIT
from .crypto import decrypt_dict, encrypt_dict, get_or_create_fernet

# alias 会进入文件系统路径（Chrome Profile、QR 截图）与日志——只允许安全字符
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LOGIN_CHECK_TTL = timedelta(hours=12)  # 设计 §6.2 / XiaohongshuSkills：登录检测缓存 ≥12h


def create_account(
    session: Session, platform: str, alias: str, creds: dict | None = None,
    rate_limit: dict | None = None, cdp_port: int | None = None, proxy: str | None = None,
) -> Account:
    adapter = get_adapter(platform)  # 未知平台直接抛错
    if not ALIAS_RE.match(alias or ""):
        # 防路径穿越/日志注入：alias 进 Chrome Profile 目录名与 QR 文件名
        raise ValueError(f"alias 仅限字母数字与 _ . -（≤64 字符，字母开头），got: {alias!r}")
    exists = session.execute(
        select(Account).where(Account.platform == platform, Account.alias == alias)
    ).scalar_one_or_none()
    if exists:
        raise ValueError(f"account {platform}:{alias} already exists")
    acc = Account(
        platform=platform,
        alias=alias,
        lane=adapter.lane,
        credential_enc=encrypt_dict(get_or_create_fernet(get_settings().data_dir), creds or {}),
        rate_limit=json.dumps(rate_limit or DEFAULT_RATE_LIMIT, ensure_ascii=False),
        cdp_port=cdp_port,
        proxy=proxy,
    )
    if adapter.lane == "cdp":
        # CDP 端口两种形态（mac 浏览器共享方案，2026-09-13 起）：
        # - SHARED_CDP_PORT(9222) = daily-checkin 共享浏览器：attach-only，fleet 绝不代启；
        #   Profile 由外部管理，故不写 chrome_profile
        # - 9300+ = 按账号独立 Profile，缺失时 fleet 可代启
        # 其余 <9300 端口拒绝（避免与其它自动化工具的 Chrome 默认 CDP 端口串号）
        if cdp_port is None or (cdp_port != SHARED_CDP_PORT and cdp_port < MIN_CDP_PORT):
            raise ValueError(
                f"cdp lane account requires cdp_port >= {MIN_CDP_PORT} "
                f"or the shared browser port {SHARED_CDP_PORT} (attach-only)"
            )
        if cdp_port != SHARED_CDP_PORT:
            acc.chrome_profile = str(get_settings().chrome_profiles_dir / f"{platform}-{alias}")
    session.add(acc)
    session.flush()
    return acc


def get_credentials(account: Account) -> dict:
    return decrypt_dict(get_or_create_fernet(get_settings().data_dir), account.credential_enc or "")


def touch_login_check(session: Session, account: Account, state: str) -> None:
    account.login_state = state
    account.last_login_check = utcnow()


def login_check_is_fresh(account: Account, now=None) -> bool:
    """True = last_login_check 仍在 12h TTL 内（编排器可跳过再探 / 信任 expired 缓存）。"""
    if account.last_login_check is None:
        return False
    stamp = now or utcnow()
    return stamp - account.last_login_check < LOGIN_CHECK_TTL


def list_accounts(session: Session) -> list[Account]:
    return list(session.execute(select(Account).order_by(Account.id)).scalars())


def get_account(session: Session, platform: str, alias: str) -> Account | None:
    return session.execute(
        select(Account).where(Account.platform == platform, Account.alias == alias)
    ).scalar_one_or_none()
