"""B站适配器（API 通道，M2）：biliup-rs 协议投稿。

两段式幂等：evidence 记 bvid 后重试绝不重传（B站重复视频不可撤回）。
凭据 = cookies.json（biliup-rs 自管理）；登录 = 人工终端扫码（login_hint）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..base import (
    ActionContext,
    Capabilities,
    Evidence,
    NeedsLoginError,
    PermanentError,
    PlatformAdapter,
    PublishResult,
)
from ..registry import register
from .client import BiliClient


class BiliAdapter(PlatformAdapter):
    platform = "bili"
    lane = "api"
    capabilities = Capabilities(image_text=False, video=True, markdown=False, max_title=80, verifiable=True)

    def _client(self) -> BiliClient:
        from ...config import get_settings

        settings = get_settings()
        return BiliClient(settings.data_dir / "bili", binary=settings.biliup_bin)

    def _validated_snapshot(self, ctx: ActionContext) -> dict:
        from ..base import load_variant_snapshot

        return load_variant_snapshot(ctx, max_title=self.capabilities.max_title)

    def check_login(self, ctx: ActionContext) -> str:
        state = self._client().check_login()
        return state if state in ("ok", "expired", "unknown") else "unknown"

    def login_interactive(self, ctx: ActionContext) -> dict:
        raise NeedsLoginError(json.dumps(self._client().login_hint(), ensure_ascii=False))

    def publish(self, ctx: ActionContext) -> PublishResult:
        snap = self._validated_snapshot(ctx)
        prior = json.loads(ctx.task.evidence or "{}")
        bvid = prior.get("bvid")
        if not bvid:
            video = prior.get("video_path")
            if not video:
                if not snap["cover_path"]:
                    raise PermanentError("bili 投稿需要视频文件（media kind=video）")
                if snap["cover_kind"] != "video":
                    raise PermanentError(
                        f"bili 需要 kind=video 的媒体，got kind={snap['cover_kind']}"
                    )
                video = snap["cover_path"]
                self._merge(ctx, {"video_path": video})  # 断点一：文件路径先落盘
            client = self._client()
            if client.check_login() != "ok":
                raise NeedsLoginError("bili cookies.json missing/expired; run `biliup-rs login` first")
            bvid = client.upload(Path(video), snap["title"], snap["body"],
                                 json.loads(snap["tags"] or "[]"))
            self._merge(ctx, {"bvid": bvid})  # 断点二：BV 落盘后重试绝不重传
        return PublishResult(submitted=True, detail={"bvid": bvid})

    def verify(self, ctx: ActionContext) -> Evidence:
        evidence = json.loads(ctx.task.evidence or "{}")
        bvid = evidence.get("bvid")
        if not bvid:
            raise PermanentError("verify: task evidence missing bvid")
        client = self._client()
        result = client.verify(bvid)
        return Evidence(ok=True, url=result["url"], raw=result["raw"])

    # ---- 证据落盘（与 gzh 同模式）----
    @staticmethod
    def _merge(ctx: ActionContext, patch: dict) -> None:
        data = json.loads(ctx.task.evidence or "{}")
        data.update(patch)
        ctx.task.evidence = json.dumps(data, ensure_ascii=False)
        if ctx.session is not None:
            ctx.session.commit()


register(BiliAdapter())
