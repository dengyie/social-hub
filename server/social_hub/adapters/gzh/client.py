"""公众号 API 客户端（cgi-bin）。

httpx 传输层可注入（tests 用 MockTransport）。errcode 统一映射为适配器错误分类：
凭据类(40001/42001...)→CredentialsError(needs_login)、配额类(45009)→Transient、其余→Permanent。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from ..base import CredentialsError, PermanentError, TransientError

log = logging.getLogger(__name__)

BASE_URL = "https://api.weixin.qq.com"

CREDENTIAL_CODES = {40001, 40002, 40003, 40013, 41001, 41002, 41004, 42001, 43002}
RATE_CODES = {45009, 45011}
RETRYABLE_CODES = {-1}

TOKEN_TTL_MARGIN = 300  # 提前 5 分钟刷新


class GzhClient:
    def __init__(self, app_id: str, app_secret: str, transport: httpx.BaseTransport | None = None, timeout: float = 15.0):
        if not app_id or not app_secret:
            raise CredentialsError("gzh account missing app_id/app_secret")
        self.app_id = app_id
        self.app_secret = app_secret
        self._http = httpx.Client(base_url=BASE_URL, transport=transport, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def close(self) -> None:
        self._http.close()

    # ---- token ----
    def fetch_token(self, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        r = self._http.get(
            "/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": self.app_id, "secret": self.app_secret},
        )
        data = self._data(r)
        self._raise_for_errcode(data)
        self._token = data["access_token"]
        self._token_expires_at = time.monotonic() + int(data.get("expires_in", 7200)) - TOKEN_TTL_MARGIN
        return self._token

    def _request(self, method: str, path: str, *, params: dict | None = None, **kw) -> dict:
        token = self.fetch_token()
        r = self._http.request(method, path, params={**(params or {}), "access_token": token}, **kw)
        data = self._data(r)
        if data.get("errcode") in CREDENTIAL_CODES:  # token 中途失效：强刷一次重试
            token = self.fetch_token(force=True)
            r = self._http.request(method, path, params={**(params or {}), "access_token": token}, **kw)
            data = self._data(r)
        self._raise_for_errcode(data)
        return data

    @staticmethod
    def _data(r: httpx.Response) -> dict:
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise PermanentError(f"unexpected gzh response: {data!r}")
        return data

    @staticmethod
    def _raise_for_errcode(data: dict) -> None:
        code = data.get("errcode", 0)
        if not code:
            return
        msg = f"gzh errcode={code} errmsg={data.get('errmsg', '')}"
        if code in CREDENTIAL_CODES:
            raise CredentialsError(msg)
        if code in RATE_CODES or code in RETRYABLE_CODES:
            raise TransientError(msg)
        raise PermanentError(msg)

    # ---- 业务接口 ----
    def add_draft(self, article: dict) -> str:
        """草稿箱新建，返回 media_id。"""
        data = self._request("POST", "/cgi-bin/draft/add", json={"articles": [article]})
        return data["media_id"]

    def add_material_image(self, file_path: Path) -> str:
        """永久素材图片上传，返回 media_id（重复上传会产生重复素材，M0 不做去重）。"""
        mime = "image/png" if file_path.suffix.lower() == ".png" else "image/jpeg"
        return self._request(
            "POST",
            "/cgi-bin/material/add_material",
            params={"type": "image"},
            files={"media": (file_path.name, file_path.read_bytes(), mime)},
        )["media_id"]

    def freepublish_submit(self, media_id: str) -> str:
        data = self._request("POST", "/cgi-bin/freepublish/submit", json={"media_id": media_id})
        return data["publish_id"]

    def freepublish_get(self, publish_id: str) -> dict:
        return self._request("POST", "/cgi-bin/freepublish/get", json={"publish_id": publish_id})
