"""Sliding-window rate limiter â port of src/bot/middlewares/rateLimit.ts.

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
from redis.exceptions import RedisError

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
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, WINDOW_SECONDS)
        except RedisError as err:
            # Redis must never become a single point of failure for Telegram.
            # Postgres remains the durable state store; on Redis failure we fail
            # open for this update and log the condition at the dispatcher level.
            return await handler(event, data)

        if count > MAX_REQUESTS:
            if message is not None:
                await message.answer("â³ You're sending commands too fast. Please slow down.")
            return None

        return await handler(event, data)
