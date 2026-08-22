"""Signal -> Telegram notification. Closes the first scheduler TODO:

    # TODO(integration): notify subscribed users (services/notification)
    # ... carried over verbatim from the original.

"Subscribed" = any User row with `notify_signals=True` (the default — see
db/models.py). Users opt out with /mute and back in with /unmute
(bot/commands/alerts.py). There's no separate per-token watchlist gate here:
every signal that clears the confidence bar goes to every subscribed user, by
design — narrowing by token/interest is a v2 feature, not wired up here.

Sends best-effort: a user who has blocked the bot or deleted their account
will make `bot.send_message` raise (aiogram wraps Telegram's 403), which we
log and skip rather than let one bad recipient abort notifying everyone else.
"""

from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.db.models import Signal, User
from whale_alpha.engines.signal import SignalCandidate
from whale_alpha.utils.logger import child_logger

log = child_logger("notification")


def _short(mint: str) -> str:
    return f"{mint[:4]}...{mint[-4:]}" if len(mint) > 8 else mint


def format_signal_message(signal: Signal, candidate: SignalCandidate) -> str:
    risk_icon = "🟢" if candidate.risk_level == "LOW" else "🟡" if candidate.risk_level == "MEDIUM" else "🔴"
    token = escape(_short(candidate.token_mint))
    mint = escape(candidate.token_mint)
    recommendation = escape(candidate.ai_recommendation) if candidate.ai_recommendation else None
    lines = [
        "🚨 <b>WHALE ALPHA • SIGNAL</b>",
        "<i>Smart-money activity cleared the signal engine</i>",
        "",
        f"🪙 <b>{token}</b>",
        f"🎯 <b>Confidence:</b> {candidate.confidence_score:.0f}/100  •  {risk_icon} <b>{escape(candidate.risk_level)} RISK</b>",
        "",
        "🐋 <b>SMART-MONEY FLOW</b>",
        f"• Accumulating wallets: <b>{candidate.wallet_count}</b>",
        f"• Capital deployed: <b>${candidate.total_capital_usd:,.0f}</b>",
    ]
    if signal.entry_zone_low is not None and signal.entry_zone_high is not None:
        lines += [
            "",
            "📍 <b>REFERENCE ENTRY ZONE</b>",
            f"<code>${signal.entry_zone_low:.6f} – ${signal.entry_zone_high:.6f}</code>",
        ]
    if recommendation:
        lines += ["", "🧠 <b>ALPHA READ</b>", f"<i>{recommendation}</i>"]
    lines += [
        "",
        "🔐 <b>CONTRACT</b>",
        f"<code>{mint}</code>",
        "",
        "⚡ <b>QUICK ACTIONS</b>",
        "<code>/buy &lt;mint&gt; &lt;usd_amount&gt;</code>  •  manual execution",
        "<code>/autotrading</code>  •  review automated risk rules",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "<i>Whale Alpha • Signal Intelligence</i>",
        "<i>Informational only — verify liquidity, contract and risk before trading.</i>",
    ]
    return "\n".join(lines)


async def notify_signal_subscribers(
    bot: Bot, session: AsyncSession, signal: Signal, candidate: SignalCandidate
) -> int:
    """Sends the signal to every subscribed user. Returns how many DMs went out."""
    result = await session.execute(select(User).where(User.notify_signals.is_(True)))
    users = list(result.scalars())

    if not users:
        return 0

    text = format_signal_message(signal, candidate)
    sent = 0
    for user in users:
        try:
            await bot.send_message(chat_id=int(user.telegram_id), text=text, parse_mode="HTML")
            sent += 1
        except (TelegramAPIError, ValueError) as err:
            # ValueError covers a non-numeric telegram_id, which shouldn't
            # happen but shouldn't take down the whole notify pass either.
            log.warning(
                "Failed to deliver signal notification",
                user_id=user.id,
                err=str(err),
            )

    log.info("Signal notification sent", signal_id=signal.id, recipients=sent, total_subscribers=len(users))
    return sent
