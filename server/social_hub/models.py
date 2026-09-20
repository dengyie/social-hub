"""SQLAlchemy 模型：账号/草稿/变体/自动化任务/任务事件/媒体。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .core.state import utcnow

DEFAULT_RATE_LIMIT = {"per_day": 3, "min_interval_min": 120}


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "alias", name="ux_account_platform_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    alias: Mapped[str] = mapped_column(String(64))
    lane: Mapped[str] = mapped_column(String(8))  # api | cdp
    credential_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet 密文
    chrome_profile: Mapped[str | None] = mapped_column(Text, nullable=True)  # CDP 通道专用
    cdp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 9222 共享(attach-only) 或 9300+ 独立，见 core/fleet.py
    proxy: Mapped[str | None] = mapped_column(Text, nullable=True)
    rate_limit: Mapped[str] = mapped_column(Text, default=lambda: __import__("json").dumps(DEFAULT_RATE_LIMIT))
    login_state: Mapped[str] = mapped_column(String(16), default="unknown")  # ok|expired|unknown
    last_login_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|paused|banned_suspect
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text)  # 相对 media_dir 的内容寻址路径
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="image")  # image|video
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual|ai|hotlist|url_import
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    variants: Mapped[list["DraftVariant"]] = relationship(back_populates="draft", cascade="all,delete-orphan")


class DraftVariant(Base):
    __tablename__ = "draft_variants"
    __table_args__ = (UniqueConstraint("draft_id", "platform", name="ux_variant_draft_platform"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    platform: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="[]")
    cover_media_id: Mapped[int | None] = mapped_column(ForeignKey("media.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ready")  # draft|ready
    generated_by: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    draft: Mapped[Draft] = relationship(back_populates="variants")


class AutomationTask(Base):
    """动作泛化的任务（action_type 区分 publish/comment/like/...，M0 实现 publish）。"""

    __tablename__ = "automation_tasks"
    __table_args__ = (
        # 幂等：同一 idem_key 只允许一个「活跃」任务存在（终态可重排）
        Index(
            "ux_active_idem",
            "idem_key",
            unique=True,
            sqlite_where=text(
                "status IN ('queued','running','verifying','needs_login','captcha_wait')"
            ),
        ),
        # 认领候选（status='queued' ORDER BY created_at）与按状态筛选/计数共用
        Index("ix_task_status_created", "status", "created_at"),
        # 限频聚合：MAX/COUNT(finished_at) WHERE account_id+status —— 热路径必须走索引
        Index("ix_task_account_status_finished", "account_id", "status", "finished_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(24), default="publish")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    idem_key: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    platform: Mapped[str] = mapped_column(String(32))
    lane: Mapped[str] = mapped_column(String(8))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 到点前不可认领
    status: Mapped[str] = mapped_column(String(16), default="queued")  # 索引见 ix_task_status_created
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)  # 动作凭证（发布 URL/评论 ID…）
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TaskEvent(Base):
    """任务时间线（追加写）：排障/审计/回放的唯一事实源。"""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("automation_tasks.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))  # queued|claimed|running|verifying|done|failed|...
    data: Mapped[str] = mapped_column(Text, default="{}")


def make_engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        # WAL + synchronous=NORMAL：提交不再逐次 fsync（掉电最多丢最后一个 checkpoint，
        # 对发布队列可接受——崩溃恢复语义由租约回收保证，不靠 fsync）
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA cache_size=-20000")  # 20MB 页缓存（默认 2MB）
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA mmap_size=268435456")  # 256MB 读路径免拷贝
        cur.close()

    return engine


def make_claim_engine(url: str) -> Engine:
    """队列专用引擎：pysqlite autocommit 模式，显式 BEGIN IMMEDIATE 抢占。"""
    engine = create_engine(url, connect_args={"check_same_thread": False, "isolation_level": None, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _set_claim_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # 首连接兜底（WAL 虽持久，防建库顺序漂移）
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA cache_size=-20000")
        cur.close()

    return engine


class WorkerHeartbeat(Base):
    """worker/调度器心跳（/readyz 判活）。"""

    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str] = mapped_column(Text, default="")
