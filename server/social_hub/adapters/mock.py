"""mock 平台适配器：契约参考实现 + 无凭据冒烟/验收用。

发布"结果"为确定性回执（https://mock.example/note/<task_id>），全程留痕走真实 DB/队列链路。
"""

from __future__ import annotations

import json

from .base import (
    ActionContext,
    Capabilities,
    Evidence,
    PermanentError,
    PlatformAdapter,
    PublishResult,
)
from .registry import register


class MockAdapter(PlatformAdapter):
    platform = "mock"
    lane = "api"
    capabilities = Capabilities(image_text=True, verifiable=True, max_title=64)

    def __init__(self):
        self.calls: list[str] = []

    def check_login(self, ctx: ActionContext) -> str:
        return "ok"

    def publish(self, ctx: ActionContext) -> PublishResult:
        title = self._variant_title(ctx)
        if len(title) > self.capabilities.max_title:
            raise PermanentError(f"title too long ({len(title)} > {self.capabilities.max_title})")
        prior = json.loads(ctx.task.evidence or "{}")
        if not prior.get("draft_media_id"):
            self.calls.append("draft")
            prior["draft_media_id"] = f"dm-{ctx.task.id}"
            self._merge(ctx, prior)
        if not prior.get("publish_id"):
            self.calls.append("submit")
            prior["publish_id"] = f"pub-{ctx.task.id}"
            self._merge(ctx, prior)
        return PublishResult(submitted=True, detail={"publish_id": prior["publish_id"]})

    def verify(self, ctx: ActionContext) -> Evidence:
        self.calls.append("verify")
        return Evidence(ok=True, url=f"https://mock.example/note/{ctx.task.id}")

    @staticmethod
    def _merge(ctx: ActionContext, data: dict) -> None:
        ctx.task.evidence = json.dumps(data, ensure_ascii=False)
        if ctx.session is not None:
            ctx.session.commit()

    @staticmethod
    def _variant_title(ctx: ActionContext) -> str:
        from sqlalchemy.orm import Session

        from ..models import DraftVariant

        payload = json.loads(ctx.task.payload or "{}")
        session: Session = ctx.session if ctx.session is not None else ctx.db()
        try:
            v = session.get(DraftVariant, payload["variant_id"])
            return v.title if v else ""
        finally:
            if ctx.session is None:
                session.close()


register(MockAdapter())
