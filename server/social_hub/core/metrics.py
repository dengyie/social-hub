"""极简指标注册表：Prometheus 文本暴露格式，零依赖（ADR-002）。"""

from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_gauges: dict[str, float] = {}
_help: dict[str, str] = {}


def inc_counter(name: str, labels: dict[str, str] | None = None, value: float = 1.0, help_: str = "") -> None:
    key = (name, tuple(sorted((labels or {}).items())))
    with _lock:
        if help_:
            _help.setdefault(name, help_)
        _counters[key] += value


def set_gauge(name: str, value: float, help_: str = "") -> None:
    with _lock:
        if help_:
            _help.setdefault(name, help_)
        _gauges[name] = value


def _labels_str(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in pairs)
    return "{" + inner + "}"


def render() -> str:
    lines: list[str] = []
    with _lock:
        counter_names = {name for (name, _labels) in _counters}
        for name, text in _help.items():
            if name in counter_names:
                lines.append(f"# HELP {name} {text}")
                lines.append(f"# TYPE {name} counter")
        for (name, labels), v in sorted(_counters.items()):
            lines.append(f"{name}{_labels_str(labels)} {v}")
        for name, v in sorted(_gauges.items()):
            lines.append(f"# HELP {name} {_help.get(name, name)}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {v}")
    return "\n".join(lines) + "\n"


def reset() -> None:  # 测试用
    with _lock:
        _counters.clear()
        _gauges.clear()
        _help.clear()
