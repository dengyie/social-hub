"""草稿与变体（M0：变体=直接复制，`status=ready`；AI 裁剪 M3 接入 content 工坊）。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..models import Draft, DraftVariant


def create_draft(
    session: Session,
    *,
    title: str,
    body: str = "",
    author: str | None = None,
    digest: str | None = None,
    tags: list[str] | None = None,
    platform: str | None = None,
    cover_media_id: int | None = None,
    variant_status: str = "ready",
) -> Draft:
    if not (title or "").strip():
        raise ValueError("title is required")
    d = Draft(
        title=title.strip(),
        body=body or "",
        author=author,
        digest=digest,
        tags=json.dumps(tags or [], ensure_ascii=False),
    )
    session.add(d)
    session.flush()
    if platform:
        session.add(
            DraftVariant(
                draft_id=d.id,
                platform=platform,
                title=title.strip(),
                body=body or "",
                tags=d.tags,
                cover_media_id=cover_media_id,
                status=variant_status,
            )
        )
        session.flush()
    return d


def variant_of(session: Session, draft_id: int, platform: str) -> DraftVariant | None:
    from sqlalchemy import select

    return session.execute(
        select(DraftVariant).where(DraftVariant.draft_id == draft_id, DraftVariant.platform == platform)
    ).scalar_one_or_none()
