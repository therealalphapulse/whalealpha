"""
app_platform/middleware/premium_middleware.py

NEW in v4. Same rationale and pattern as rbac_middleware.py, applied to
Premium-gated features instead of admin permissions:

    @router.message(Command("premium_signals"), flags={"require_premium": "premium_signals"})
    async def cmd_premium_signals(message: Message, ...): ...

If the flag is set and the user isn't premium, they see the existing
`premium_upsell_text()` copy (unchanged, moved from services/premium_service.py
to domain/payments/premium_service.py) instead of the handler running.
Existing per-handler `is_premium()` checks in premium.py/real_wallet.py
continue to work unchanged; opting a handler into this flag is incremental,
per the same phased-adoption reasoning as RBACMiddleware.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject, Message, CallbackQuery

from domain.payments.premium_service import is_premium, premium_upsell_text


class PremiumMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        feature_key = get_flag(data, "require_premium")

        if feature_key is None:
            return await handler(event, data)

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return None

        if await is_premium(tg_user.id):
            return await handler(event, data)

        upsell = premium_upsell_text(feature_key)
        if isinstance(event, Message):
            await event.answer(upsell)
        elif isinstance(event, CallbackQuery):
            await event.answer("Premium required.", show_alert=True)
        return None
