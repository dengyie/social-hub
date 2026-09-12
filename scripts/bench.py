"""性能基准：队列认领 / 任务入队 / API 列表（优化前后各跑一次，数字留档）。

独立临时数据目录，不污染真实数据；只依赖 builtin mock 适配器。

    .venv/Scripts/python.exe scripts/bench.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SOCIAL_HUB_DATA_DIR", tempfile.mkdtemp(prefix="shub-bench-"))
os.environ.setdefault("SOCIAL_HUB_LEASE_SECONDS", "60")
sys.path.insert(0, str(_ROOT / "server"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from social_hub.core.queue import claim_next  # noqa: E402
from social_hub.core.state import utcnow  # noqa: E402
from social_hub.core.taskops import enqueue_publish  # noqa: E402
from social_hub.db import get_claim_engine, get_session_factory, init_db  # noqa: E402
from social_hub.models import Account, AutomationTask, make_engine  # noqa: E402


def _bulk_tasks(session, account_id: int, count: int, status: str, key_prefix: str) -> None:
    now = utcnow()
    rows = [
        {
            "action_type": "publish", "payload": "{}",
            "idem_key": f"{key_prefix}{i}", "account_id": account_id,
            "platform": "mock", "lane": "api", "status": status,
            "retries": 0, "created_at": now - timedelta(minutes=count - i),
            "finished_at": now - timedelta(minutes=count - i) if status == "done" else None,
        }
        for i in range(count)
    ]
    session.execute(AutomationTask.__table__.insert(), rows)


def _mark_done(claim_engine, task_id: int) -> None:
    with claim_engine.connect() as conn:
        conn.execute(
            text("UPDATE automation_tasks SET status='done', finished_at=:now WHERE id=:tid"),
            {"now": utcnow(), "tid": task_id},
        )
        conn.commit()


def _avg_ms(fn, n: int, warmup: int = 5) -> float:
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n * 1000


def main() -> None:
    from social_hub.adapters.registry import load_builtin_adapters
    from social_hub.content.draft_service import create_draft
    from social_hub.web.app import create_app

    init_db()
    load_builtin_adapters()
    sf = get_session_factory()

    # 账号：限频全关（per_day=0/min_interval=0），否则基准测的是限频短路而非认领成本
    with sf() as s:
        acc = Account(platform="mock", alias="bench", lane="api",
                      rate_limit='{"per_day": 0, "min_interval_min": 0}')
        s.add(acc)
        s.flush()
        aid = acc.id
        s.commit()

        _bulk_tasks(s, aid, 2000, "done", "seed-done-")
        _bulk_tasks(s, aid, 200, "done", "seed-extra-")
        s.commit()

        draft_ids = []
        for i in range(100):
            d = create_draft(s, title=f"bench {i}", platform="mock")
            draft_ids.append(d.id)
        s.commit()

    results: dict[str, float] = {}

    # ---- API 层（TestClient 不进 with = 不跑 lifespan，无 worker 抢任务）----
    from social_hub.web.app import app as fastapi_app  # noqa: PLC0415

    client = TestClient(fastapi_app)
    results["api_tasks_ms(avg of 50, 2200 rows)"] = round(_avg_ms(
        lambda: client.get("/api/v1/tasks").json(), 50), 3)
    results["api_drafts_ms(avg of 50, 100 drafts N+1)"] = round(_avg_ms(
        lambda: client.get("/api/v1/drafts").json(), 50), 3)

    # ---- 入队（幂等查询随同键 done 任务增长；每轮把上一单置 done 以便继续入队）----
    with sf() as s:
        s.add(AutomationTask(status="done", finished_at=utcnow(), account_id=aid,
                             platform="mock", lane="api", idem_key="warm-1"))
        s.commit()

    def _enqueue_cycle() -> None:
        with sf() as s:
            t = enqueue_publish(s, draft_ids[0], "mock", "bench")
            tid = t.id
            s.commit()
        with sf() as s:
            row = s.get(AutomationTask, tid)
            row.status = "done"
            row.finished_at = utcnow()
            s.commit()

    results["enqueue_ms(avg of 300)"] = round(_avg_ms(_enqueue_cycle, 300, warmup=10), 3)

    # ---- 认领热路径：300 次认领（含限频聚合查询），2000+ 行历史 ----
    with sf() as s:
        _bulk_tasks(s, aid, 300, "queued", "seed-queued-")
        s.commit()
    ce = get_claim_engine()
    t0 = time.perf_counter()
    claimed = 0
    for _ in range(300):
        tid = claim_next(ce, sf, "bench-worker", 60)
        if tid is not None:
            _mark_done(ce, tid)
            claimed += 1
    dt = time.perf_counter() - t0
    results["claim_next_ms(avg of 300 claims)"] = round(dt / claimed * 1000, 3) if claimed else -1
    results["claims_claimed"] = claimed

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
