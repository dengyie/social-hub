"""媒体仓库：内容寻址 media/yyyy-mm/<sha256>.<ext>，秒传去重（设计文档 §5）。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.state import utcnow
from ..models import Media

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".flv", ".mkv", ".webm"}


def ingest(session: Session, media_dir: Path, src: Path) -> Media:
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"media file not found: {src}")
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    existing = session.execute(select(Media).where(Media.sha256 == sha)).scalar_one_or_none()
    if existing:
        return existing
    dest_dir = media_dir / utcnow().strftime("%Y-%m")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sha}{src.suffix.lower()}"
    if not dest.exists():
        shutil.copyfile(src, dest)
    row = Media(
        path=str(dest.relative_to(media_dir)),
        sha256=sha,
        kind="video" if src.suffix.lower() in VIDEO_EXTS else "image",
        bytes=len(data),
    )
    session.add(row)
    session.flush()
    return row


def resolve(media_dir: Path, media: Media) -> Path:
    return Path(media_dir) / media.path
