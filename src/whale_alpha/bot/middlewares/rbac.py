"""RBAC middleware — port of src/bot/middlewares/rbac.ts (aiogram v3 version).

Attaches `is_admin` to the aiogram event's `data` dict based on the configured
admin Telegram ID allowlist. This is a bootstrap mechanism — production
deployments should back this with the User.role column (see db/models.py)
once an admin console exists for role changes, so admin rights can be
granted/revoked without redeploying environment variables. Carried over
verbatim from the original TS comment.

Note on library-driven difference (see PORTING_NOTES.md): grammY's middleware
signature is `(ctx, next) => Promise<void>`, mutating `ctx` directly. aiogram
v3 middleware is `(handler, event, data) => Awaitable`, and handlers read
context via the `data` dict (or, for aiogram's DI-style handlers, via a typed
kwarg). We mirror the same *behavior* — every downstream handler can read
`is_admin` — via `data["is_admin"]`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from whale_alpha.config import Env


class RbacMiddleware(BaseMiddleware):
    def __init__(self, env: Env) -> None:
        self._admin_ids = env.admin_telegram_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat_id = None
        if isinstance(event, Update):
            if event.message and event.message.from_user:
                chat_id = event.message.from_user.id
            elif event.callback_query and event.callback_query.from_user:
                chat_id = event.callback_query.from_user.id
        else:
            from_user = getattr(event, "from_user", None)
            if from_user is not None:
                chat_id = from_user.id

        data["is_admin"] = chat_id is not None and str(chat_id) in self._admin_ids
        return await handler(event, data)


async def require_admin(message_reply: Callable[[str], Awaitable[Any]], is_admin: bool) -> bool:
    """Helper used by command handlers: replies + returns False if not admin.

    Equivalent of the TS `requireAdmin` middleware, expressed as a guard
    function since aiogram command handlers are more naturally written as
    "check and return" rather than a middleware chain per-command (grammY
    registers `requireAdmin` per-command; aiogram filters would work too, but
    a guard keeps the 1:1 mapping to the TS command bodies clearer).
    """
    if not is_admin:
        await message_reply("⛔ This command is restricted to administrators.")
        return False
    return True
