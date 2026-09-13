"""掘金适配器（API 通道，M1.5）：Cookie + 创作 API，Markdown 正文。

三段式幂等：draft_id → article_id → verify URL；凭据 = 浏览器 Cookie（credential 变量 cookie=...）。
"""

from __future__ import annotations

import json

from ..base import (
    ActionContext,
    Capabilities,
    CredentialsError,
    Evidence,
    PermanentError,
    PlatformAdapter,
    PublishResult,
)
from ..registry import register
from .client import JuejinClient


class JuejinAdapter(PlatformAdapter):
    platform = "juejin"
    lane = "api"
    capabilities = Capabilities(image_text=True, markdown=True, max_title=100, verifiable=True)

    def _client(self, ctx: ActionContext) -> JuejinClient:
        from ...vault.service import get_credentials

        creds = get_credentials(ctx.account)
        return JuejinClient(creds.get("cookie", ""))

    def _validated_snapshot(self, ctx: ActionContext) -> dict:
        from ..base import load_variant_snapshot

        return load_variant_snapshot(ctx, max_title=self.capabilities.max_title)

    def check_login(self, ctx: ActionContext) -> str:
        client = self._client(ctx)
        try:
            client._request("GET", "/content_api/v1/article/query_list",
                            params={"cursor": "0", "sort_type": 2}, json={})
            return "ok"
        except CredentialsError:
            return "expired"
        finally:
            client.close()

    def publish(self, ctx: ActionContext) -> PublishResult:
        snap = self._validated_snapshot(ctx)
        prior = json.loads(ctx.task.evidence or "{}")
        client = self._client(ctx)
        try:
            draft_id = prior.get("draft_id")
            if not draft_id:
                draft_id = client.create_draft(snap["title"], snap["body"])
                self._merge(ctx, {"draft_id": draft_id})  # 断点一
            article_id = prior.get("article_id")
            if not article_id:
                article_id = client.publish(draft_id)
                self._merge(ctx, {"article_id": article_id})  # 断点二
        finally:
            client.close()
        return PublishResult(submitted=True,
                             detail={"draft_id": draft_id, "article_id": article_id,
                                     "note_url": f"https://juejin.cn/post/{article_id}"})

    def verify(self, ctx: ActionContext) -> Evidence:
        evidence = json.loads(ctx.task.evidence or "{}")
        article_id = evidence.get("article_id")
        if not article_id:
            raise PermanentError("verify: task evidence missing article_id")
        client = self._client(ctx)
        try:
            client.detail(article_id)
        finally:
            client.close()
        url = f"https://juejin.cn/post/{article_id}"
        return Evidence(ok=True, url=url, raw={"article_id": article_id})

    @staticmethod
    def _merge(ctx: ActionContext, patch: dict) -> None:
        data = json.loads(ctx.task.evidence or "{}")
        data.update(patch)
        ctx.task.evidence = json.dumps(data, ensure_ascii=False)
        if ctx.session is not None:
            ctx.session.commit()


register(JuejinAdapter())
