"""凭据加密：Fernet 主钥落在 data_dir/key.bin（POSIX 0600），明文不入库。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def get_or_create_fernet(data_dir: Path) -> Fernet:
    key_path = data_dir / "key.bin"
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows 无 POSIX 权限位
    return Fernet(key_path.read_bytes())


def encrypt_dict(fernet: Fernet, data: dict) -> str:
    return fernet.encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()


def decrypt_dict(fernet: Fernet, token: str) -> dict:
    try:
        return json.loads(fernet.decrypt(token.encode()))
    except InvalidToken as e:  # 主钥不匹配：数据目录换过
        raise RuntimeError("credential decrypt failed: key.bin 与数据库不匹配") from e
