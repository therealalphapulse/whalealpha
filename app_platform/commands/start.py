import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from domain.admin.user_service import get_or_create_user
from domain.signals.pump_radar import set_pump_subscription

router = Router()
logger = logging.getLogger("AlphaPulse.Start")


WELCOME_TEXT = (
    "⚡ <b>AlphaPulse</b>\n"
    "<i>AI-Powered Solana Signal &amp; Trading Intelligence</i>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "✅ Signal alerts: <b>enabled</b>\n"
    "📊 Paper trading account: <b>$100 created</b>\n\n"
    "Every high-quality signal is scored on narrative + momentum, security-checked, "
    "and can auto-trade in your paper account. Here's everything you can do:\n\n"

    "📡 <b>Signals &amp; Market</b>\n"
    "/signals — active tracked signals\n"
    "/winners — best-performing signals\n"
    "/top, /top10 — top performing leaderboard\n"
    "/signal_status — signal tracker status\n"
    "/trending — trending tokens\n"
    "/narratives — trending themes/sectors\n"
    "/market — market overview\n\n"

    "🔍 <b>Token Lookup</b>\n"
    "/token &lt;contract&gt; — full token card\n"
    "/security &lt;contract&gt; — rug/security check\n"
    "/score &lt;contract&gt; — token score\n\n"

    "📝 <b>Paper Trading</b>\n"
    "/paper — dashboard: positions, history, settings, PnL calendar\n"
    "Includes auto-buy on signals, TP/SL auto-sell, moonbag partial exits, "
    "custom $ amounts, and a daily trade limit — all configurable in /paper → Settings.\n\n"

    "💼 <b>Portfolio</b>\n"
    "/portfolio — your manual portfolio\n"
    "/portfolio_add, /portfolio_remove — manage it\n"
    "/wallet_portfolio — live on-chain wallet lookup\n\n"

    "🐋 <b>Whale &amp; KOL Tracking</b>\n"
    "/track, /untrack, /wallets, /activity — track any wallet\n"
    "/kol_wallets, /kol_status, /kol_sync — known KOL wallets\n"
    "/kol_alerts_on, /kol_alerts_off — KOL move alerts\n\n"

    "🔔 <b>Watchlist &amp; Alerts</b>\n"
    "/watchlist, /add, /remove — price watchlist\n"
    "/pump_alerts_on, /pump_alerts_off, /pump_status — signal alert subscription\n\n"

    "━━━━━━━━━━━━━━━━━━━━━\n"
    "Start with /signals to see what's live, or /paper to check your account.\n"
    "🔥 AlphaPulse v4.0"
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id

    try:
        await get_or_create_user(
            telegram_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )

        await set_pump_subscription(user_id, True)

    except Exception as e:
        logger.error(f"/start setup error: {e}")

    await message.answer(WELCOME_TEXT)
