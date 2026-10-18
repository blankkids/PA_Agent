"""交易执行留痕：trade_records/executions.jsonl 追加写入（永不写 Secret）."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# 锚定项目根（pa_agent/trade/executions.py → 项目根/trade_records），不依赖进程 CWD
_EXECUTIONS_PATH = Path(__file__).resolve().parents[2] / "trade_records" / "executions.jsonl"
_write_lock = threading.Lock()

_REDACT_KEYS = {"api_secret", "apisecret", "secret", "api_key", "apikey"}


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if any(r in k.lower() for r in _REDACT_KEYS) else _sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    return obj


def append_execution_event(event: dict[str, Any], path: Path | None = None) -> None:
    """追加一条执行事件（自动加时间戳并脱敏）。"""
    target = path or _EXECUTIONS_PATH
    record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **_sanitize(event)}
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
