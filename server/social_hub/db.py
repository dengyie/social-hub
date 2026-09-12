"""DB 会话与初始化。M0 用 create_all；schema 首次演化时引入 alembic（见 docs/ADR-001）。"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base, make_claim_engine, make_engine

_engine = None
_claim_engine = None
_session_factory: sessionmaker | None = None


def get_engine():
    global _engine, _claim_engine, _session_factory
    if _engine is None:
        url = get_settings().database_url
        _engine = make_engine(url)
        _claim_engine = make_claim_engine(url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_claim_engine():
    get_engine()
    return _claim_engine


def get_session_factory() -> sessionmaker:
    get_engine()
    return _session_factory


@contextmanager
def session() -> Session:
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    get_engine()
    Base.metadata.create_all(_engine)


def reset_db_cache() -> None:  # 测试隔离用
    global _engine, _claim_engine, _session_factory
    _engine = _claim_engine = None
    _session_factory = None
