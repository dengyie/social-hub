"""daemon API：三探针 / 认证 / 全链路（建账号→草稿→发布→done+回执）+ SSE。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from social_hub import config as cfg
from social_hub.web.app import create_app


def _wait_terminal(client, task_id: int, want: str = "done", timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    t = {}
    while time.time() < deadline:
        t = client.get(f"/api/v1/tasks/{task_id}").json()
        if t["status"] in ("done", "failed", "canceled", "needs_login", "captcha_wait"):
            break
        time.sleep(0.3)
    return t


def test_healthz_readyz_metrics_and_e2e(env):
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200

        deadline = time.time() + 10
        rr = None
        while time.time() < deadline:
            rr = client.get("/readyz")
            if rr.status_code == 200:
                break
            time.sleep(0.3)
        assert rr is not None and rr.status_code == 200, rr.text if rr else "no response"
        assert rr.json()["checks"]["db_writable"] is True

        assert "shub_queue_depth" in client.get("/metrics").text

        # ---- 全链路 ----
        r = client.post("/api/v1/accounts", json={"platform": "mock", "alias": "demo", "vars": {"token": "x"}})
        assert r.status_code == 201, r.text
        assert client.post("/api/v1/accounts", json={"platform": "mock", "alias": "demo"}).status_code == 400
        assert client.post("/api/v1/accounts", json={"platform": "nosuch", "alias": "x"}).status_code == 400

        platforms = client.get("/api/v1/platforms").json()
        assert "mock" in platforms and "gzh" in platforms
        assert platforms["gzh"]["lane"] == "api"

        r = client.post("/api/v1/drafts", json={"title": "api 文章", "body": "<p>x</p>", "platform": "mock"})
        assert r.status_code == 201
        did = r.json()["id"]

        # 幂等：用「定时在未来」的任务验证（worker 不会抢先跑完，消除竞态）
        future = "2099-01-01T00:00:00"
        r = client.post("/api/v1/tasks/publish",
                        json={"draft_id": did, "platform": "mock", "account_alias": "demo",
                              "scheduled_at": future})
        assert r.status_code == 201
        tid = r.json()["task_id"]
        r2 = client.post("/api/v1/tasks/publish",
                         json={"draft_id": did, "platform": "mock", "account_alias": "demo",
                               "scheduled_at": future})
        assert r2.json()["task_id"] == tid
        assert r2.json()["status"] == "queued"

        # 立即执行链路：另一篇草稿
        did2 = client.post("/api/v1/drafts", json={"title": "api 文章2", "platform": "mock"}).json()["id"]
        r3 = client.post("/api/v1/tasks/publish",
                         json={"draft_id": did2, "platform": "mock", "account_alias": "demo"})
        tid2 = r3.json()["task_id"]
        assert tid2 != tid

        t = _wait_terminal(client, tid2)
        assert t["status"] == "done", t
        assert t["result_ref"].startswith("https://mock.example/")
        assert any(e["kind"] == "verified" for e in t["events"])

        assert "shub_tasks_total" in client.get("/metrics").text


def test_sse_stream(env):
    with TestClient(create_app()) as client:
        client.post("/api/v1/accounts", json={"platform": "mock", "alias": "demo", "vars": {"token": "x"}})
        r = client.post("/api/v1/drafts", json={"title": "sse", "platform": "mock"})
        did = r.json()["id"]
        tid = client.post("/api/v1/tasks/publish",
                          json={"draft_id": did, "platform": "mock", "account_alias": "demo"}).json()["task_id"]
        # 等到终态再连 SSE：应立刻回放事件并收到 end
        deadline = time.time() + 15
        while time.time() < deadline:
            t = client.get(f"/api/v1/tasks/{tid}").json()
            if t["status"] in ("done", "failed"):
                break
            time.sleep(0.3)
        events: list[dict] = []
        end = None
        with client.stream("GET", f"/api/v1/tasks/{tid}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("event: end"):
                    end = line
                    break
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        assert end is not None
        assert any(e["kind"] == "queued" for e in events)


def test_auth_token_required(env, monkeypatch):
    monkeypatch.setenv("SOCIAL_HUB_API_TOKEN", "tok123")
    cfg.reset_settings()
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/accounts").status_code == 401
        assert client.get("/api/v1/accounts",
                          headers={"Authorization": "Bearer tok123"}).status_code == 200
        # 探针免认证
        assert client.get("/healthz").status_code == 200
