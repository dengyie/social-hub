"""FastAPI daemon：可观测性三探针 + /api/v1（accounts/drafts/tasks/media + SSE）。

安全（设计文档 §9.3）：配置了 SOCIAL_HUB_API_TOKEN 则强制 Bearer；未配置仅放行 loopback。
/healthz /readyz /metrics 免认证（探活/抓取需要）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..adapters.registry import load_builtin_adapters, registered_platforms, get_adapter
from ..config import get_settings
from ..core.orchestrator import Orchestrator
from ..core.scheduler import HubScheduler
from ..core.state import TERMINAL, utcnow
from ..core.taskops import enqueue_publish, requeue_task
from ..content.draft_service import create_draft
from ..db import get_session_factory, init_db
from ..media.service import ingest
from ..models import Account, AutomationTask, Draft, Media, TaskEvent, WorkerHeartbeat
from ..vault.service import create_account, list_accounts

log = logging.getLogger(__name__)
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from ..core.logging_setup import setup_logging

    settings = get_settings()
    setup_logging(settings.logs_dir)
    if not settings.api_token:
        # 无 token 时 require_auth 只放行 loopback——但请求经同机隧道/反代转发后
        # client.host 恰是 127.0.0.1，等于对公网开放；必须留痕提醒运维
        log.warning("SOCIAL_HUB_API_TOKEN not set: API allows loopback only. "
                    "If this service is exposed via a tunnel/reverse proxy on the same host, "
                    "remote requests will appear as 127.0.0.1 and be ACCEPTED — set a token!")
    init_db()
    load_builtin_adapters()
    orch = Orchestrator()
    orch.start()
    sched = HubScheduler(settings.max_retries)
    sched.start()
    from ..core.scheduler import gauges_job

    gauges_job()  # 启动即产出指标（不等调度器首个周期），/metrics 探针确定性可用
    _state["orch"] = orch
    _state["sched"] = sched
    log.info("social-hub daemon up", extra={"event": "start"})
    yield
    sched.shutdown()
    orch.stop()


# ---------- 认证 ----------
async def require_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if settings.api_token:
        if authorization != f"Bearer {settings.api_token}":
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1", "testserver", "testclient") or host == "":
        return
    raise HTTPException(status_code=401, detail="loopback only (set SOCIAL_HUB_API_TOKEN for remote)")


# ---------- 模型 ----------
class AccountIn(BaseModel):
    platform: str
    alias: str
    vars: dict[str, str] = {}
    rate_limit: dict | None = None
    cdp_port: int | None = None
    proxy: str | None = None


class DraftIn(BaseModel):
    title: str
    body: str = ""
    author: str | None = None
    digest: str | None = None
    tags: list[str] = []
    platform: str | None = None
    cover_media_id: int | None = None


class PublishIn(BaseModel):
    draft_id: int
    platform: str
    account_alias: str
    scheduled_at: str | None = None  # ISO8601（naive UTC）


class MediaIn(BaseModel):
    path: str


# ---------- 工厂 ----------
def create_app() -> FastAPI:
    app = FastAPI(title="social-hub", version="0.1.0", lifespan=lifespan)

    # ---- 探针（免认证）----
    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": app.version}

    @app.get("/readyz")
    def readyz():
        settings = get_settings()
        now = utcnow()
        checks: dict[str, bool] = {}
        try:
            with get_session_factory()() as session:
                row = session.get(WorkerHeartbeat, "readyz-probe")
                if row is None:
                    session.add(WorkerHeartbeat(name="readyz-probe", ok=True, detail="probe"))
                else:
                    row.at = now
                session.commit()
            checks["db_writable"] = True
        except Exception:
            checks["db_writable"] = False
        with get_session_factory()() as session:
            worker_fresh = session.execute(
                text(
                    "SELECT COUNT(*) FROM worker_heartbeats "
                    "WHERE name LIKE 'worker-%' AND ok=1 AND at >= :fresh"
                ),
                {"fresh": now - timedelta(seconds=settings.lease_seconds)},
            ).scalar()
            sched_fresh = session.execute(
                text("SELECT COUNT(*) FROM worker_heartbeats WHERE name='scheduler' AND ok=1 AND at >= :fresh"),
                {"fresh": now - timedelta(seconds=60)},
            ).scalar()
        checks["worker_alive"] = bool(worker_fresh)
        checks["scheduler_alive"] = bool(sched_fresh)
        ok = all(checks.values())
        return (JSONStatus({"status": "ok" if ok else "degraded", "checks": checks}, ok))

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint():
        from ..core import metrics

        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    # ---- /api/v1 ----
    @app.get("/api/v1/platforms", dependencies=[Depends(require_auth)])
    def platforms():
        out = {}
        for name in registered_platforms():
            a = get_adapter(name)
            out[name] = {"lane": a.lane, "capabilities": a.capabilities.__dict__}
        return out

    @app.post("/api/v1/accounts", status_code=201, dependencies=[Depends(require_auth)])
    def add_account(body: AccountIn):
        with get_session_factory()() as session:
            try:
                acc = create_account(session, body.platform, body.alias, body.vars,
                                     rate_limit=body.rate_limit, cdp_port=body.cdp_port, proxy=body.proxy)
                aid, lane = acc.id, acc.lane
                session.commit()
            except (ValueError, KeyError) as e:
                raise HTTPException(status_code=400, detail=str(e))
        return {"id": aid, "platform": body.platform, "alias": body.alias, "lane": lane}

    @app.get("/api/v1/accounts", dependencies=[Depends(require_auth)])
    def get_accounts():
        with get_session_factory()() as session:
            return [
                {"id": a.id, "platform": a.platform, "alias": a.alias, "lane": a.lane,
                 "login_state": a.login_state, "status": a.status, "cdp_port": a.cdp_port}
                for a in list_accounts(session)
            ]

    @app.post("/api/v1/media", status_code=201, dependencies=[Depends(require_auth)])
    def add_media(body: MediaIn):
        with get_session_factory()() as session:
            try:
                m = ingest(session, get_settings().media_dir, __import__("pathlib").Path(body.path))
                mid = m.id
                session.commit()
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return {"id": mid}

    @app.post("/api/v1/drafts", status_code=201, dependencies=[Depends(require_auth)])
    def add_draft(body: DraftIn):
        with get_session_factory()() as session:
            try:
                d = create_draft(session, title=body.title, body=body.body, author=body.author,
                                 digest=body.digest, tags=body.tags, platform=body.platform,
                                 cover_media_id=body.cover_media_id)
                did = d.id
                session.commit()
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return {"id": did}

    @app.get("/api/v1/drafts", dependencies=[Depends(require_auth)])
    def get_drafts(limit: int = Query(default=100, le=500)):
        with get_session_factory()() as session:
            # selectinload 一次性取全部 variants：lazy 逐条加载是 N+1（100 草稿 = 101 查询）
            q = (
                select(Draft)
                .options(selectinload(Draft.variants))
                .order_by(Draft.id.desc())
                .limit(limit)
            )
            return [
                {"id": d.id, "title": d.title,
                 "created_at": d.created_at.isoformat() if d.created_at else None,
                 "variants": [{"id": v.id, "platform": v.platform, "status": v.status} for v in d.variants]}
                for d in session.execute(q).scalars()
            ]

    @app.post("/api/v1/tasks/publish", status_code=201, dependencies=[Depends(require_auth)])
    def publish_now(body: PublishIn, request: Request):
        scheduled = None
        if body.scheduled_at:
            try:
                scheduled = datetime.fromisoformat(body.scheduled_at)
            except ValueError:
                raise HTTPException(status_code=400, detail="scheduled_at must be ISO8601")
        with get_session_factory()() as session:
            try:
                task = enqueue_publish(session, body.draft_id, body.platform, body.account_alias, scheduled)
                tid, status = task.id, task.status
                session.commit()
            except IntegrityError:
                # 并发同 key 入队撞 ux_active_idem：回滚后重查——此时活跃任务已存在，幂等命中
                session.rollback()
                task = enqueue_publish(session, body.draft_id, body.platform, body.account_alias, scheduled)
                tid, status = task.id, task.status
                session.commit()
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return {"task_id": tid, "status": status}

    @app.get("/api/v1/tasks", dependencies=[Depends(require_auth)])
    def get_tasks(status: str | None = Query(default=None), limit: int = Query(default=100, le=500)):
        with get_session_factory()() as session:
            q = select(AutomationTask).order_by(AutomationTask.id.desc()).limit(limit)
            if status:
                q = q.where(AutomationTask.status == status)
            return [_task_dto(t) for t in session.execute(q).scalars()]

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(require_auth)])
    def get_task(task_id: int):
        with get_session_factory()() as session:
            t = session.get(AutomationTask, task_id)
            if t is None:
                raise HTTPException(status_code=404, detail="task not found")
            dto = _task_dto(t)
            dto["events"] = [
                {"id": e.id, "kind": e.kind, "at": e.at.isoformat() if e.at else None,
                 "data": json.loads(e.data or "{}")}
                for e in session.execute(
                    select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
                ).scalars()
            ]
            return dto

    @app.post("/api/v1/tasks/{task_id}/requeue", dependencies=[Depends(require_auth)])
    def requeue(task_id: int):
        with get_session_factory()() as session:
            try:
                t = requeue_task(session, task_id)
                tid, status = t.id, t.status
                session.commit()
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return {"task_id": tid, "status": status}

    @app.get("/api/v1/tasks/{task_id}/events", dependencies=[Depends(require_auth)])
    async def task_events(task_id: int):
        async def gen():
            last = 0
            while True:
                with get_session_factory()() as session:
                    rows = list(session.execute(
                        select(TaskEvent)
                        .where(TaskEvent.task_id == task_id, TaskEvent.id > last)
                        .order_by(TaskEvent.id)
                    ).scalars())
                    task = session.get(AutomationTask, task_id)
                for ev in rows:
                    last = ev.id
                    payload = json.dumps(
                        {"kind": ev.kind, "data": json.loads(ev.data or "{}"),
                         "at": ev.at.isoformat() if ev.at else None},
                        ensure_ascii=False,
                    )
                    yield f"id: {ev.id}\ndata: {payload}\n\n"
                if task and task.status in TERMINAL and not rows:
                    yield f'event: end\ndata: {json.dumps({"status": task.status})}\n\n'
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


class JSONStatus(dict):
    """带状态码的 JSON 响应（/readyz 用）。"""

    def __new__(cls, data: dict, ok: bool):
        from fastapi.responses import JSONResponse

        return JSONResponse(content=data, status_code=200 if ok else 503)


def _task_dto(t: AutomationTask) -> dict:
    return {
        "id": t.id,
        "action_type": t.action_type,
        "platform": t.platform,
        "lane": t.lane,
        "account_id": t.account_id,
        "status": t.status,
        "result_ref": t.result_ref,
        "error": t.error,
        "error_class": t.error_class,
        "retries": t.retries,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
    }


app = create_app()
