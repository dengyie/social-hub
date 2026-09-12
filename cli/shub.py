"""shub CLI（typer）：本地直连 DB（local-first），远程机器走 /api/v1。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

app = typer.Typer(help="social-hub 控制台（M0）", no_args_is_help=True)
account_app = typer.Typer(help="账号保险库", no_args_is_help=True)
media_app = typer.Typer(help="媒体仓库", no_args_is_help=True)
draft_app = typer.Typer(help="草稿", no_args_is_help=True)
task_app = typer.Typer(help="任务", no_args_is_help=True)
app.add_typer(account_app, name="account")
app.add_typer(media_app, name="media")
app.add_typer(draft_app, name="draft")
app.add_typer(task_app, name="task")


def _init():
    from social_hub.adapters.registry import load_builtin_adapters
    from social_hub.config import get_settings
    from social_hub.db import init_db

    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    load_builtin_adapters()
    return settings


def _err(e: Exception) -> None:
    typer.secho(f"错误: {e}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _split_var(kv: str) -> tuple[str, str]:
    if "=" not in kv:
        raise ValueError(f"--var 需要 key=value，得到: {kv}")
    k, _, v = kv.partition("=")
    return k.strip(), v


def _parse_dt(s: str | None):
    return datetime.fromisoformat(s) if s else None


# ---------- serve ----------
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int | None = typer.Option(None, "--port", help="默认 8767"),
    log_level: str = typer.Option("info", "--log-level"),
):
    """启动 daemon（API + 编排器 worker + 调度器）。"""
    settings = _init()
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.api_token:
        # deny-by-default：非 loopback 绑定 + 无 token = 对外开放（隧道/端口映射场景同理）
        _err(ValueError(
            "refusing to serve: --host 非 loopback 且未设置 SOCIAL_HUB_API_TOKEN"
            "（API 会完全开放；请设置 token 或改用 127.0.0.1）"
        ))
    import uvicorn

    uvicorn.run(
        "social_hub.web.app:create_app",
        factory=True,
        host=host,
        port=port or settings.port,
        log_level=log_level,
    )


# ---------- account ----------
@account_app.command("add")
def account_add(
    platform: str,
    alias: str,
    var: list[str] = typer.Option([], "--var", help="凭据键值对 key=value，可重复"),
    cdp_port: int = typer.Option(None, "--cdp-port", help="CDP 调试端口（必须 >=9300；9222 为 Chrome 默认 CDP 端口，禁用）"),
    per_day: int = typer.Option(None, "--per-day"),
    min_interval_min: int = typer.Option(None, "--min-interval-min"),
):
    """新增平台账号（凭据 Fernet 加密入库）。"""
    try:
        creds = dict(_split_var(v) for v in var)
        rl: dict = {}
        if per_day is not None:
            rl["per_day"] = per_day
        if min_interval_min is not None:
            rl["min_interval_min"] = min_interval_min
        _init()
        from social_hub.db import session
        from social_hub.vault.service import create_account

        with session() as s:
            acc = create_account(s, platform, alias, creds, rate_limit=rl or None, cdp_port=cdp_port)
            typer.echo(f"account #{acc.id} {acc.platform}:{acc.alias} lane={acc.lane}")
    except Exception as e:
        _err(e)


@account_app.command("list")
def account_list():
    _init()
    from social_hub.db import session
    from social_hub.vault.service import list_accounts

    with session() as s:
        rows = list_accounts(s)
    if not rows:
        typer.echo("(no accounts)")
        return
    for a in rows:
        typer.echo(f"#{a.id:<4} {a.platform}:{a.alias:<16} lane={a.lane:<4} login={a.login_state:<8} status={a.status}")


# ---------- media ----------
@media_app.command("add")
def media_add(path: Path):
    """本地文件入库（内容寻址去重），返回 media id。"""
    try:
        settings = _init()
        from social_hub.db import session
        from social_hub.media.service import ingest

        with session() as s:
            m = ingest(s, settings.media_dir, path)
            typer.echo(f"media #{m.id} {m.kind} {m.bytes}B {m.path}")
    except Exception as e:
        _err(e)


# ---------- draft ----------
@draft_app.command("create")
def draft_create(
    title: str,
    platform: str = typer.Option(..., "--platform", help="生成该平台的变体"),
    content_file: Path = typer.Option(None, "--content-file", help="正文文件（gzh 为 HTML）"),
    body: str = typer.Option("", "--body"),
    author: str = typer.Option(None, "--author"),
    digest: str = typer.Option(None, "--digest"),
    tag: list[str] = typer.Option([], "--tag"),
    cover_media: int = typer.Option(None, "--cover-media", help="媒体 id（gzh 必需封面）"),
    account: str = typer.Option(None, "--account", help="顺带校验账号存在"),
):
    """创建草稿 + 平台变体（M0 变体=直接复制）。"""
    try:
        _init()
        from social_hub.content.draft_service import create_draft
        from social_hub.db import session
        from social_hub.vault.service import get_account

        body_text = Path(content_file).read_text(encoding="utf-8") if content_file else body
        with session() as s:
            if account and get_account(s, platform, account) is None:
                raise ValueError(f"account {platform}:{account} not found")
            d = create_draft(s, title=title, body=body_text, author=author, digest=digest,
                             tags=list(tag), platform=platform, cover_media_id=cover_media)
            typer.echo(f"draft #{d.id} '{d.title}' + variant({platform}, ready)")
    except Exception as e:
        _err(e)


@draft_app.command("list")
def draft_list():
    _init()
    from social_hub.db import session
    from sqlalchemy import select

    from social_hub.models import Draft

    with session() as s:
        for d in s.execute(select(Draft).order_by(Draft.id.desc())).scalars():
            vs = ",".join(f"{v.platform}:{v.status}" for v in d.variants) or "-"
            typer.echo(f"#{d.id:<4} {d.title[:40]:<42} [{vs}]")


# ---------- publish ----------
@app.command()
def publish(
    draft: int = typer.Option(..., "--draft", help="draft id"),
    platform: str = typer.Option(..., "--platform"),
    account: str = typer.Option(..., "--account", help="账号 alias"),
    wait: bool = typer.Option(False, "--wait", help="内联执行到终态（不走 daemon）"),
    at: str = typer.Option(None, "--at", help="ISO8601 定时（naive UTC）"),
):
    """入队发布任务；--wait 时本地内联执行（验收/调试用）。"""
    try:
        _init()
        from social_hub.core.taskops import enqueue_publish
        from social_hub.db import session
        from social_hub.models import AutomationTask

        with session() as s:
            t = enqueue_publish(s, draft, platform, account, _parse_dt(at))
            tid = t.id
            s.commit()
        typer.echo(f"task #{tid} enqueued")
        if wait:
            from social_hub.core.orchestrator import Orchestrator

            orch = Orchestrator(worker_prefix="cli")
            status = orch.drain_until_terminal(tid, timeout=300)
            with session() as s:
                t = s.get(AutomationTask, tid)
            typer.echo(f"task #{tid} -> {status}")
            if t.result_ref:
                typer.echo(f"result: {t.result_ref}")
            if t.error:
                typer.secho(f"error: {t.error}", fg=typer.colors.RED)
            if status != "done":
                raise typer.Exit(1)
    except Exception as e:
        _err(e)


# ---------- task ----------
@task_app.command("list")
def task_list(status: str = typer.Option(None, "--status")):
    _init()
    from sqlalchemy import select

    from social_hub.db import session
    from social_hub.models import AutomationTask

    with session() as s:
        q = select(AutomationTask).order_by(AutomationTask.id.desc()).limit(50)
        if status:
            q = q.where(AutomationTask.status == status)
        rows = s.execute(q).scalars().all()
    if not rows:
        typer.echo("(no tasks)")
        return
    for t in rows:
        extra = t.result_ref or t.error or ""
        typer.echo(f"#{t.id:<5} {t.platform:<6} {t.action_type:<8} {t.status:<12} retries={t.retries} {extra[:60]}")


@task_app.command("show")
def task_show(task_id: int):
    _init()
    import json as _json

    from sqlalchemy import select

    from social_hub.db import session
    from social_hub.models import AutomationTask, TaskEvent

    with session() as s:
        t = s.get(AutomationTask, task_id)
        if t is None:
            _err(ValueError(f"task #{task_id} not found"))
        typer.echo(f"task #{t.id} {t.platform}/{t.action_type} status={t.status} retries={t.retries}")
        if t.result_ref:
            typer.echo(f"result_ref: {t.result_ref}")
        if t.error:
            typer.echo(f"error: [{t.error_class}] {t.error}")
        for e in s.execute(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)).scalars():
            typer.echo(f"  [{e.at.isoformat() if e.at else '-'}] {e.kind} {_json.loads(e.data or '{}')}")


@task_app.command("requeue")
def task_requeue(task_id: int):
    """人工重排 needs_login/failed/captcha_wait 任务。"""
    try:
        _init()
        from social_hub.core.taskops import requeue_task
        from social_hub.db import session

        with session() as s:
            t = requeue_task(s, task_id)
            s.commit()
        typer.echo(f"task #{t.id} -> {t.status}")
    except Exception as e:
        _err(e)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
