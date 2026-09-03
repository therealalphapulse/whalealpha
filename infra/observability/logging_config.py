"""
infra/observability/logging_config.py

NEW in v4 (Bible §10). The audit found logging limited to a single
`logging.basicConfig(level=INFO, format="%(asctime)s | %(levelname)s | %(message)s")`
call in `main.py`, plain-text only, no correlation IDs, plus 39 stray
`print()` calls bypassing the logging framework entirely (grep-verified in
the audit).

This module provides:
  * `correlation_id_var` — a `contextvars.ContextVar` set by
    `CorrelationMiddleware` per update, read by the formatter below so
    every log line emitted while handling a given command carries the
    same ID without any call-site changes.
  * `configure_logging()` — call once at process startup. Emits structured
    JSON when `LOG_FORMAT=json` (the production default in
    docker-compose.yml), or the old human-readable text format when unset
    (kept as the default so local/dev output — including in this sandbox
    — stays exactly as readable as v3's was).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get() or "-"
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())

    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | [%(correlation_id)s] | %(name)s | %(message)s"
            )
        )

    root.handlers.clear()
    root.addHandler(handler)
