"""掘金客户端：创作者 Web API（Cookie 鉴权，Markdown 正文）。

- 草稿：POST /content_api/v1/article/draft/create {title, mark_content}
- 发布：POST /content_api/v1/article/publish {draft_id, ...}
- 明细：GET  /content_api/v1/article/detail?article_id=
- 应答统一 {"err_no": 0, "err_msg": ""}；err_no 非零按语义分类。
"""

from __future__ import annotations

import httpx

from ..base import CredentialsError, PermanentError, TransientError

BASE_URL = "https://api.juejin.cn"
LOGIN_HINT_CODES = {"010161", "010162"}  # jwt 失效/未登录 [calibrate]


class JuejinClient:
    def __init__(self, cookie: str, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        if not cookie:
            raise CredentialsError("juejin account missing cookie")
        self.cookie = cookie
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json=payload)

    def _request(self, method: str, path: str, **kw) -> dict:
        headers = {"Cookie": self.cookie, "Content-Type": "application/json",
                   "User-Agent": "Mozilla/5.0 (social-hub)"}
        try:
            r = self._http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise TransientError(f"juejin network error: {e}") from e
        if r.status_code >= 500:
            raise TransientError(f"juejin upstream {r.status_code}")
        if r.status_code in (401, 403):
            raise CredentialsError(f"juejin auth failed (http {r.status_code}) — cookie 过期")
        try:
            data = r.json()
        except ValueError as e:
            raise PermanentError(f"juejin non-json response: {r.text[:120]!r}") from e
        err_no = str(data.get("err_no", "0"))
        if err_no == "0":
            return data.get("data") or {}
        if err_no in LOGIN_HINT_CODES or "token" in str(data.get("err_msg", "")).lower():
            raise CredentialsError(f"juejin err_no={err_no} err_msg={data.get('err_msg')}")
        raise PermanentError(f"juejin err_no={err_no} err_msg={data.get('err_msg')}")

    def create_draft(self, title: str, markdown: str) -> str:
        data = self._post("/content_api/v1/article/draft/create", {"title": title, "mark_content": markdown})
        draft_id = data.get("draft_id")
        if not draft_id:
            raise PermanentError("juejin draft/create missing draft_id")
        return str(draft_id)

    def publish(self, draft_id: str) -> str:
        data = self._post("/content_api/v1/article/publish",
                          {"draft_id": draft_id, "column_ids": [], "tags": [], "cover_image": ""})
        article_id = data.get("article_id")
        if not article_id:
            raise PermanentError("juejin publish missing article_id")
        return str(article_id)

    def detail(self, article_id: str) -> dict:
        return self._request("GET", "/content_api/v1/article/detail", params={"article_id": article_id})
