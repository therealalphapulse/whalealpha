"""Sliding-window rate limiter — port of src/bot/middlewares/rateLimit.ts.

Per-Telegram-user, backed by Redis, so a spam script hammering the bot can't
exhaust RPC/API quota or DB connections. Same window/threshold as the
original: 15 requests per 10-second window.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from redis.asyncio import Redis

WINDOW_SECONDS = 10
MAX_REQUESTS = 15


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = None
        message = None
        if isinstance(event, Update):
            if event.message and event.message.from_user:
                chat_id = event.message.from_user.id
                message = event.message
            elif event.callback_query and event.callback_query.from_user:
                chat_id = event.callback_query.from_user.id

        if chat_id is None:
            return await handler(event, data)

        key = f"ratelimit:{chat_id}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, WINDOW_SECONDS)

        if count > MAX_REQUESTS:
            if message is not None:
                await message.answer("⏳ You're sending commands too fast. Please slow down.")
            return None

        return await handler(event, data)
