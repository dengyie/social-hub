"""结构化 JSON 日志：按天滚动落 logs/YYYYMMDD.jsonl + 控制台人类可读。

敏感信息（token/cookie/secret/手机号）在序列化前统一脱敏（设计文档 §9.3）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

SENSITIVE_KEYS = re.compile(r"secret|token|cookie|password|authorization", re.I)
PHONE_RE = re.compile(r"1[3-9]\d{9}")
SECRET_RE = re.compile(r"(app_secret=|token=|secret=)[A-Za-z0-9_\-]+")

SENSITIVE_VALUE_KEYS = {"app_secret", "secret", "access_token", "cookie"}


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if SENSITIVE_KEYS.search(str(k)) and v not in (None, "", []) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str):
        s = SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", s := obj)
        return PHONE_RE.sub("1**********", s)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("task_id", "account_id", "platform", "worker", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        exc = record.exc_info
        if exc:
            payload["exc"] = self.formatException(exc)
        return json.dumps(_redact(payload), ensure_ascii=False)


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    fh = logging.FileHandler(logs_dir / f"social-hub-{today}.jsonl", encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(ch)

    logging.getLogger("uvicorn.access").handlers = [fh]
