"""
app_platform/middleware/rbac_middleware.py

NEW in v4 (Bible §8 — Security Architecture). The audit's finding:
admin/premium checks were called *inside* individual handler bodies,
meaning correctness depended entirely on every handler author remembering
to add the check — nothing structurally prevented a new admin-only
handler from shipping without one.

This middleware makes that structurally impossible for any handler that
opts in via an aiogram flag:

    @router.message(Command("ban"), flags={"required_permission": "manage_admins"})
    async def cmd_ban(message: Message, ...): ...

If `required_permission` is set and the resolved user (see
AuthMiddleware, which must run before this one) lacks it, the handler
never executes — RBACMiddleware replies with a denial and returns.

v3's existing per-handler `has_permission()` calls inside `admin_panel.py`
and elsewhere are NOT removed by this change — they keep working
unchanged, belt-and-suspenders, until each handler is incrementally
migrated to declare its permission via the flag instead (tracked as a
v4.1 follow-up per the Bible's phased roadmap, not a blind mass edit of
already-working admin code).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject, Message, CallbackQuery

from domain.admin.admin_rbac import has_permission


class RBACMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        required_permission = get_flag(data, "required_permission")

        if required_permission is None:
            return await handler(event, data)

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return None

        allowed = await has_permission(tg_user.id, required_permission)
        if allowed:
            return await handler(event, data)

        denial_text = (
            f"⛔ This action requires the <b>{required_permission}</b> "
            "permission, which your account doesn't have."
        )
        if isinstance(event, Message):
            await event.answer(denial_text)
        elif isinstance(event, CallbackQuery):
            await event.answer("Permission denied.", show_alert=True)
        return None
