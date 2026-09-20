"""公众号适配器：素材上传 → 草稿箱 → freepublish → 轮询拿发布链接。

平台侧两段式天然幂等：draft/add 进草稿箱，freepublish 发布；verify 复查发布状态拿 article url。
注意：公众号草稿要求 thumb_media_id（封面必需）；content 为 HTML（markdown=False）。
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

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
from .client import GzhClient

log = logging.getLogger(__name__)

PUBLISH_STATUS_OK = 0
PUBLISH_STATUS_RUNNING = 1


class GzhAdapter(PlatformAdapter):
    platform = "gzh"
    lane = "api"
    capabilities = Capabilities(
        image_text=True,
        video=False,
        markdown=False,
        max_title=64,
        max_tags=0,
        platform_scheduling=False,
        verifiable=True,
    )

    poll_interval = 2.0
    poll_attempts = 15

    def _client(self, ctx: ActionContext) -> GzhClient:
        from ...vault.service import get_credentials

        creds = get_credentials(ctx.account)
        return GzhClient(creds.get("app_id", ""), creds.get("app_secret", ""))

    def _load_variant(self, ctx: ActionContext):
        from ..base import load_variant_snapshot, require_media_kind

        snap = load_variant_snapshot(ctx, max_title=self.capabilities.max_title)
        # 公众号 thumb_media_id 必须是图片（API add_material_image 只收图）。
        # review P1 根因：媒体 kind 校验统一收口唯一实现，双通道共用。
        require_media_kind(snap, platform="gzh", kinds=("image",), required=True)
        return snap

    def check_login(self, ctx: ActionContext) -> str:
        client = self._client(ctx)
        try:
            client.fetch_token(force=True)
            return "ok"
        except CredentialsError:
            return "expired"
        finally:
            client.close()

    def publish(self, ctx: ActionContext) -> PublishResult:
        """两段式幂等：draft_media_id / publish_id 逐阶段落 task.evidence，
        退避重试或进程崩溃恢复后从断点继续，绝不重复建草稿/重复提交发布。"""
        snap = self._load_variant(ctx)
        if len(snap["title"]) > self.capabilities.max_title:
            raise PermanentError(
                f"title too long for gzh ({len(snap['title'])} > {self.capabilities.max_title})"
            )

        prior = json.loads(ctx.task.evidence or "{}")
        client = self._client(ctx)
        try:
            draft_media_id = prior.get("draft_media_id")
            if not draft_media_id:
                thumb_media_id = client.add_material_image(_path(snap["cover_path"]))
                draft_media_id = client.add_draft(
                    {
                        "title": snap["title"],
                        "author": snap.get("author") or "",
                        "digest": snap.get("digest") or "",
                        "content": snap["body"],
                        "thumb_media_id": thumb_media_id,
                        "need_open_comment": 0,
                        "only_fans_can_comment": 0,
                    }
                )
                self._merge_evidence(ctx, {"draft_media_id": draft_media_id})

            publish_id = prior.get("publish_id")
            if not publish_id:
                publish_id = client.freepublish_submit(draft_media_id)
                self._merge_evidence(ctx, {"publish_id": publish_id})

            status = self._poll_status(client, publish_id, attempts=self.poll_attempts)
            return PublishResult(submitted=True,
                                 detail={"draft_media_id": draft_media_id, "publish_id": publish_id,
                                         "publish_status": status})
        finally:
            client.close()

    @staticmethod
    def _merge_evidence(ctx: ActionContext, patch: dict) -> None:
        data = json.loads(ctx.task.evidence or "{}")
        data.update(patch)
        ctx.task.evidence = json.dumps(data, ensure_ascii=False)
        if ctx.session is not None:
            ctx.session.commit()

    def _poll_status(self, client: GzhClient, publish_id: str, attempts: int) -> int:
        for i in range(attempts):
            data = client.freepublish_get(publish_id)
            status = int(data.get("publish_status", -1))
            if status == PUBLISH_STATUS_OK:
                return status
            if status != PUBLISH_STATUS_RUNNING:
                raise PermanentError(
                    f"gzh freepublish failed: publish_status={status} errmsg={data.get('errmsg', '')}"
                )
            if i < attempts - 1:
                time.sleep(self.poll_interval)
        return PUBLISH_STATUS_RUNNING  # 仍在发布中，交给 verify 复查

    def verify(self, ctx: ActionContext) -> Evidence:
        import json

        evidence = json.loads(ctx.task.evidence or "{}")
        publish_id = evidence.get("publish_id")
        if not publish_id:
            raise PermanentError("verify: task evidence missing publish_id")
        client = self._client(ctx)
        try:
            data = client.freepublish_get(publish_id)
            status = int(data.get("publish_status", -1))
            if status != PUBLISH_STATUS_OK:
                raise _transient_if_running(status, data)
            url = _first_article_url(data)
            return Evidence(ok=bool(url), url=url, raw={"publish_status": status})
        finally:
            client.close()


def _transient_if_running(status: int, data: dict):
    from ..base import TransientError

    if status == PUBLISH_STATUS_RUNNING:
        return TransientError("gzh freepublish still running")
    return PermanentError(f"gzh freepublish failed: publish_status={status} errmsg={data.get('errmsg', '')}")


def _first_article_url(data: dict) -> str | None:
    detail = data.get("article_detail") or {}
    items = detail.get("item") or []
    for item in items:
        if item.get("url"):
            return item["url"]
    return None


def _path(p: str):
    from pathlib import Path

    return Path(p)


register(GzhAdapter())
