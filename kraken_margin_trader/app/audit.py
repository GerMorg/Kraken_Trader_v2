from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from typing import Any, TextIO

_SECRET_KEYS = {
    "api_key", "kraken_api_key", "api_secret", "kraken_api_secret", "secret",
    "token", "api_sign", "signature", "authorization", "headers",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(api[-_ ]?(?:key|sign|secret)|authorization|bearer|token)\s*[:=]\s*[^\s,;]+"
)

def _json_default(value: Any) -> str:
    return str(value)

def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(r"\1=[REDACTED]", value)
    return value

class AuditLogger:
    def __init__(self, path: str = "/data/logs/events.jsonl", *,
                 stream: TextIO | None = None, max_bytes: int = 5_000_000,
                 backup_count: int = 5) -> None:
        self.stream = stream or sys.stdout
        self.file_handler = None
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.file_handler = RotatingFileHandler(
                target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        except OSError:
            self.file_handler = None

    def emit(self, event: str, **payload: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "app_version": "0.1.0", "strategy_version": "tsm-0.1.0",
            "event": event, "payload": redact(payload),
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=_json_default)
        print(line, file=self.stream, flush=True)
        if self.file_handler is not None:
            log_record = logging.LogRecord("kraken_trader_v2", logging.INFO, "", 0, line, (), None)
            if self.file_handler.shouldRollover(log_record):
                self.file_handler.doRollover()
            self.file_handler.emit(log_record)
        return record

    def close(self) -> None:
        if self.file_handler is not None:
            self.file_handler.close()
            self.file_handler = None