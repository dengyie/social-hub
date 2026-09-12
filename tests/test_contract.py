"""适配器契约套件：mock 参考实现与公众号适配器都必须全绿。"""

from __future__ import annotations

from httpx import MockTransport

from conftest import MockAdapter
from social_hub.adapters.contract import run_contract_suite
from social_hub.adapters.gzh.adapter import GzhAdapter
from social_hub.adapters.gzh.client import GzhClient
from test_gzh import _handler


def test_contract_mock_adapter(make_task):
    out = make_task(platform="mock", alias="demo", title="契约测试")
    run_contract_suite(MockAdapter(), out["ctx"],
                       ctx_factory=lambda title: make_task(platform="mock", alias="demo2", title=title)["ctx"])


def test_contract_gzh_adapter(make_task, monkeypatch):
    out = make_task(platform="gzh", alias="main", title="契约测试文章", cover=True,
                    creds={"app_id": "wx1", "app_secret": "s"})
    calls: list = []
    a = GzhAdapter()
    a.poll_interval = 0

    def _client(self, ctx):
        return GzhClient("wx1", "s", transport=MockTransport(_handler(calls)))

    monkeypatch.setattr(GzhAdapter, "_client", _client)
    run_contract_suite(a, out["ctx"],
                       ctx_factory=lambda title: make_task(platform="gzh", alias=f"u{abs(hash(title)) % 9999}",
                                                           title=title, cover=True,
                                                           creds={"app_id": "wx1", "app_secret": "s"})["ctx"])
