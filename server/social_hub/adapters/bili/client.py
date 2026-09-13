"""B站投稿客户端：外部二进制 biliup-rs（仅个人使用能力，禁止商用——上游许可约束）。

- 登录：`biliup-rs login`（终端扫码，cookies.json 落在 cwd）——人工一次性操作
- 投稿：`biliup-rs upload <file> --title --desc --tag`，stdout 解析 BV 号
- 客户端只读 cookies.json 判登录态；**绝不修改/清除 cookie**（域过滤红线同级）
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx

from ..base import CredentialsError, PermanentError, TransientError

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
UPLOAD_TIMEOUT = 6 * 3600  # 大视频上传：子进程级超时交给任务租约体系管理，这里兜底 6h


class BiliClient:
    def __init__(self, work_dir: Path, binary: str = "biliup-rs", runner=None, transport=None):
        self.work_dir = Path(work_dir)
        self.binary = binary
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner or self._subprocess_run
        self._http = httpx.Client(base_url="https://www.bilibili.com", timeout=20.0,
                                  transport=transport) if transport else httpx.Client(timeout=20.0)

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _subprocess_run(argv: list[str], timeout: int) -> str:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (proc.stdout or "") + (proc.stderr or "")

    # ---- 登录态 ----
    @property
    def cookies_path(self) -> Path:
        return self.work_dir / "cookies.json"

    def check_login(self) -> str:
        """cookies.json 存在且非空 → ok；缺失 → unknown（登录走人工 `biliup-rs login`）。"""
        if not self.cookies_path.exists():
            return "unknown"
        try:
            data = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "unknown"
        return "ok" if data else "unknown"

    def login_hint(self) -> dict:
        return {"command": f"{self.binary} login", "cwd": str(self.work_dir),
                "note": "终端扫码完成登录，cookies.json 会落在 cwd；完成后 shub task requeue"}

    # ---- 投稿 ----
    def upload(self, file_path: Path, title: str, desc: str, tags: list[str]) -> str:
        """上传视频，返回 BV 号。输出不含 BV 视为失败（脚本改版探测点）。"""
        if not Path(file_path).is_file():
            raise PermanentError(f"video file not found: {file_path}")
        argv = [
            self.binary, "upload", str(file_path),
            "--title", title, "--desc", desc,
            "--tag", ",".join(tags) if tags else "自制",
        ]
        out = self._runner(argv, UPLOAD_TIMEOUT)
        m = BV_RE.search(out)
        if not m:
            raise TransientError(f"biliup upload did not yield BV (输出片段: {out[-200:]!r})")
        return m.group(0)

    def verify(self, bvid: str) -> dict:
        """BV 链接可访问（200）即视为核验通过。"""
        url = f"https://www.bilibili.com/video/{bvid}"
        try:
            r = self._http.get(url)
        except httpx.HTTPError as e:
            raise TransientError(f"bili verify network error: {e}") from e
        if r.status_code == 200:
            return {"url": url, "raw": {"bvid": bvid}}
        if r.status_code in (403, 412):  # 风控页，稍后复查
            raise TransientError(f"bili verify hit {r.status_code} (风控/限频), retry later")
        if r.status_code == 404:
            raise PermanentError(f"bili video {bvid} not found (404)")
        raise TransientError(f"bili verify unexpected status {r.status_code}")
