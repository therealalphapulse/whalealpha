"""
app_platform/middleware/auth_middleware.py

NEW in v4. Previously, `get_or_create_user()` (moved to
domain/admin/user_service.py in v4) and role lookups were called
ad hoc inside individual command handlers wherever they happened to be
needed. This middleware resolves them once per update and attaches the
result to aiogram's handler `data` dict, so handlers can request `user`
and `role` as ordinary keyword arguments instead of each re-implementing
the lookup — and so `RBACMiddleware`/`PremiumMiddleware` (which run after
this one) don't need a second DB round-trip to know who's asking.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from domain.admin.admin_rbac import get_role
from domain.admin.user_service import get_or_create_user


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")

        if tg_user is not None:
            user = await get_or_create_user(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
            )
            role = await get_role(tg_user.id)

            data["user"] = user
            data["role"] = role
            data["is_admin"] = role is not None

        return await handler(event, data)
