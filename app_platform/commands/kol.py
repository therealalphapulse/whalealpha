import logging

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from domain.admin.user_service import get_or_create_user
from domain.intelligence.kol_tracker import (
    get_active_kol_wallets,
    format_kol_wallets_list,
    set_kol_alert_subscription,
    get_kol_alert_status,
    sync_kol_wallets_from_provider,
)

router = Router()
logger = logging.getLogger("AlphaPulse.KOLCommands")


@router.message(Command("kol_wallets"))
async def cmd_kol_wallets(message: Message):
    try:
        wallets = await get_active_kol_wallets()
        report = format_kol_wallets_list(wallets)
        await message.answer(report)
    except Exception as e:
        logger.error(f"/kol_wallets error: {e}")
        await message.answer("⚠️ Could not load KOL wallets right now. Try again shortly.")


@router.message(Command("kol_alerts_on"))
async def cmd_kol_alerts_on(message: Message):
    try:
        await get_or_create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )

        await set_kol_alert_subscription(
            user_id=message.from_user.id,
            enabled=True,
        )

        await message.answer(
            "✅ <b>KOL Alerts Enabled</b>\n\n"
            "AlphaPulse will alert you when the synced KOL provider reports new wallet activity.\n\n"
            "Use /kol_wallets to view synced wallets.\n"
            "Use /kol_alerts_off to disable alerts."
        )
    except Exception as e:
        logger.error(f"/kol_alerts_on error: {e}")
        await message.answer("⚠️ Could not enable KOL alerts right now. Try again shortly.")


@router.message(Command("kol_alerts_off"))
async def cmd_kol_alerts_off(message: Message):
    try:
        await set_kol_alert_subscription(
            user_id=message.from_user.id,
            enabled=False,
        )

        await message.answer(
            "🔕 <b>KOL Alerts Disabled</b>\n\n"
            "You will no longer receive KOL wallet activity alerts."
        )
    except Exception as e:
        logger.error(f"/kol_alerts_off error: {e}")
        await message.answer("⚠️ Could not disable KOL alerts right now. Try again shortly.")


@router.message(Command("kol_status"))
async def cmd_kol_status(message: Message):
    try:
        enabled = await get_kol_alert_status(message.from_user.id)
        wallets = await get_active_kol_wallets()

        status = "✅ Enabled" if enabled else "🔕 Disabled"

        await message.answer(
            "🧠 <b>KOL Provider Status</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Alerts: <b>{status}</b>\n"
            f"Synced Active Wallets: <b>{len(wallets)}</b>\n\n"
            "Commands:\n"
            "/kol_alerts_on — Enable alerts\n"
            "/kol_alerts_off — Disable alerts\n"
            "/kol_wallets — View synced KOL wallets\n"
            "/kol_sync — Manually trigger provider sync"
        )
    except Exception as e:
        logger.error(f"/kol_status error: {e}")
        await message.answer("⚠️ Could not load KOL status right now. Try again shortly.")


@router.message(Command("kol_sync"))
async def cmd_kol_sync(message: Message):
    try:
        await message.answer("🧠 Syncing KOL provider wallets...")

        result = await sync_kol_wallets_from_provider(bot=None)

        if not result.get("ok"):
            await message.answer(
                "⚠️ <b>KOL Provider Sync Failed</b>\n\n"
                f"Reason: <code>{result.get('reason')}</code>\n\n"
                "Check Railway Variables:\n"
                "<code>KOL_PROVIDER_URL</code>\n"
                "<code>KOL_PROVIDER_API_KEY</code>"
            )
            return

        await message.answer(
            "✅ <b>KOL Provider Sync Complete</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Imported/Updated: <b>{result.get('imported')}</b>\n"
            f"Active Wallets: <b>{result.get('active')}</b>\n"
            f"Alerts Triggered: <b>{result.get('alerts')}</b>"
        )
    except Exception as e:
        logger.error(f"/kol_sync error: {e}")
        await message.answer("⚠️ KOL sync failed unexpectedly. Try again shortly.")
