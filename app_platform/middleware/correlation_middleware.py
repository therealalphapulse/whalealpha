"""
app_platform/middleware/correlation_middleware.py

NEW in v4 (Bible §10 — Observability Stack Design). The audit found no
way to trace a user's command through to the provider calls it triggered
— every log line was independent, with no shared ID linking "user sent
/token X" to "DexScreener call for X" to "DB write for X".

This middleware generates one correlation ID per incoming Telegram
update, attaches it to the aiogram handler `data` dict (so any handler or
service can read it via the `correlation_id` kwarg aiogram injects), and
binds it into `infra.observability.logging_config`'s context so every log
line emitted while handling this update carries the same ID — with zero
call-site changes required in existing handlers/services.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from infra.observability.logging_config import correlation_id_var


class CorrelationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        correlation_id = uuid.uuid4().hex[:16]
        data["correlation_id"] = correlation_id

        token = correlation_id_var.set(correlation_id)
        try:
            return await handler(event, data)
        finally:
            correlation_id_var.reset(token)
