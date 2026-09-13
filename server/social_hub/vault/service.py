"""账号保险库服务：创建/查询/解密凭据。平台合法性由适配器注册表校验。"""

from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.registry import get_adapter
from ..config import get_settings
from ..core.state import utcnow
from ..models import Account, DEFAULT_RATE_LIMIT
from .crypto import decrypt_dict, encrypt_dict, get_or_create_fernet

# alias 会进入文件系统路径（Chrome Profile、QR 截图）与日志——只允许安全字符
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


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
        # CDP 通道每账号独立 Profile + 独占端口段（9300+）；9222 是 Chrome 默认 CDP 端口，
        # 常被其它自动化工具占用，为避免串号直接禁用
        if cdp_port is None or cdp_port < 9300:
            raise ValueError(
                "cdp lane account requires cdp_port >= 9300 "
                "(9222 is the default Chrome CDP debug port, commonly occupied by other tools)"
            )
        acc.chrome_profile = str(get_settings().chrome_profiles_dir / f"{platform}-{alias}")
    session.add(acc)
    session.flush()
    return acc


def get_credentials(account: Account) -> dict:
    return decrypt_dict(get_or_create_fernet(get_settings().data_dir), account.credential_enc or "")


def touch_login_check(session: Session, account: Account, state: str) -> None:
    account.login_state = state
    account.last_login_check = utcnow()


def list_accounts(session: Session) -> list[Account]:
    return list(session.execute(select(Account).order_by(Account.id)).scalars())


def get_account(session: Session, platform: str, alias: str) -> Account | None:
    return session.execute(
        select(Account).where(Account.platform == platform, Account.alias == alias)
    ).scalar_one_or_none()
