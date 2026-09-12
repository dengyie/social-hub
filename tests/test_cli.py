"""CLI 端到端：账号→草稿→publish --wait→task show 全留痕；CDP 端口红线校验。"""

from __future__ import annotations

from typer.testing import CliRunner

from cli.shub import app
from social_hub.adapters.base import Capabilities, PlatformAdapter
from social_hub.adapters.registry import register

runner = CliRunner()


def _output(r) -> str:
    """兼容 click 版本差异：stdout + stderr 合并断言。"""
    return (r.output or "") + (getattr(r, "stderr", "") or "")


class FakeCdpAdapter(PlatformAdapter):
    platform = "fakecdp"
    lane = "cdp"
    capabilities = Capabilities(image_text=True)

    def check_login(self, ctx) -> str:
        return "unknown"


def test_cli_end_to_end(env, tmp_path):
    content = tmp_path / "a.html"
    content.write_text("<p>hi social-hub</p>", encoding="utf-8")

    r = runner.invoke(app, ["account", "add", "mock", "demo", "--var", "token=demo"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["account", "list"])
    assert "mock:demo" in r.output

    r = runner.invoke(app, ["draft", "create", "CLI 文章", "--platform", "mock", "--account", "demo",
                            "--content-file", str(content)])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["publish", "--draft", "1", "--platform", "mock", "--account", "demo", "--wait"])
    assert r.exit_code == 0, r.output
    assert "-> done" in r.output
    assert "https://mock.example" in r.output

    r = runner.invoke(app, ["task", "show", "1"])
    assert "status=done" in r.output
    assert "queued" in r.output and "verified" in r.output  # 时间线留痕


def test_cli_duplicate_publish_is_idempotent(env):
    # min_interval=0：同一账号重复发布不受限频窗干扰（首单 done 后再发为新任务）
    runner.invoke(app, ["account", "add", "mock", "demo", "--var", "token=demo",
                        "--min-interval-min", "0"])
    runner.invoke(app, ["draft", "create", "dup", "--platform", "mock"])
    r1 = runner.invoke(app, ["publish", "--draft", "1", "--platform", "mock", "--account", "demo", "--wait"])
    r2 = runner.invoke(app, ["publish", "--draft", "1", "--platform", "mock", "--account", "demo", "--wait"])
    assert r1.exit_code == 0, _output(r1)
    assert r2.exit_code == 0, _output(r2)
    assert "task #1" in _output(r1)          # 首次入队
    assert "-> done" in _output(r2)          # 重发：终态任务允许重新排队 → 新任务执行成功
    assert "task #1 enqueued" in _output(r2) or "task #2 enqueued" in _output(r2)


def test_cli_cdp_port_redline(env):
    """CDP 通道账号必须 9300+ 端口；Chrome 默认端口 9222 直接拒绝。"""
    register(FakeCdpAdapter())
    r = runner.invoke(app, ["account", "add", "fakecdp", "a1"])
    assert r.exit_code == 1
    assert "9300" in _output(r)
    r = runner.invoke(app, ["account", "add", "fakecdp", "a2", "--cdp-port", "9222"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["account", "add", "fakecdp", "a3", "--cdp-port", "9301"])
    assert r.exit_code == 0, _output(r)


def test_serve_deny_by_default(env, monkeypatch):
    """非 loopback 绑定 + 无 token → 拒绝启动；设 token 或 loopback 才放行。"""
    from social_hub import config as cfg

    monkeypatch.delenv("SOCIAL_HUB_API_TOKEN", raising=False)
    cfg.reset_settings()
    r = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert r.exit_code == 1
    assert "SOCIAL_HUB_API_TOKEN" in _output(r)

    monkeypatch.setenv("SOCIAL_HUB_API_TOKEN", "tok")
    cfg.reset_settings()
    called: dict = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: called.setdefault("ok", True))
    r = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert r.exit_code == 0, _output(r)
    assert called.get("ok")

    r = runner.invoke(app, ["serve"])  # loopback 无 token 允许（本机调试默认）
    assert r.exit_code == 0, _output(r)
