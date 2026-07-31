"""Structured logging — port of src/utils/logger/index.ts (pino) to structlog.

Behavior preserved:
  * Fields that must never appear in logs, even accidentally (private keys,
    tokens, secrets), are redacted regardless of nesting depth.
  * Pretty, colorized console output in development; structured JSON otherwise
    (pino-pretty vs plain pino, mirrored with structlog's ConsoleRenderer vs
    JSONRenderer).
  * `child_logger(module)` binds a `module` field, exactly like pino's
    `logger.child({ module })`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# Keys redacted anywhere in the event dict (case-insensitive), mirroring the TS
# REDACT_PATHS list (`*.privateKey`, `*.encryptedWalletKey`, `*.password`,
# `*.secret`, `*.token`, `req.headers.authorization`).
_REDACT_KEYS = {
    "privatekey",
    "private_key",
    "encryptedwalletkey",
    "encrypted_wallet_key",
    "password",
    "secret",
    "token",
    "authorization",
}
_CENSOR = "[REDACTED]"


def _redact_processor(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: (_CENSOR if k.lower() in _REDACT_KEYS else scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    return scrub(event_dict)


def configure_logging(log_level: str, node_env: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_processor,
    ]

    if node_env == "development":
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def child_logger(module: str) -> Any:
    """Equivalent of the TS `childLogger(module)` — binds a `module` field."""
    return structlog.get_logger().bind(module=module)
