"""High-precision early Solana token opportunity detector."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReplyParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from whale_alpha.config import Env
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot, enrich_tokens
from whale_alpha.integrations.token_age import resolve_token_ages
from whale_alpha.utils.logger import child_logger

from whale_alpha.db.models import TokenOpportunity, TokenSnapshot, User, WalletEvent, WalletStatus, WhaleWallet
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate, discover_token_candidates

log = child_logger("tokenHunter")


def alert_recipient_ids(admin_ids: set[str], subscriber_ids: list[str]) -> list[str]:
    """Return admin recipients when configured, otherwise subscribed users."""
    if admin_ids:
        return sorted(admin_ids)
    return list(dict.fromkeys(str(chat_id) for chat_id in subscriber_ids if str(chat_id).strip()))


def quote_milestones_for_gain(gain_pct: float) -> list[int]:
    """Return crossed quote milestones: +25/+50/+75/+100%, then each whole X."""
    if gain_pct < 25:
        return []
    milestones = [m for m in range(25, 101, 25) if gain_pct >= m]
    if gain_pct >= 200:
        milestones.extend(range(200, int(gain_pct // 100) * 100 + 1, 100))
    return milestones


def format_quote_alert(o: TokenOpportunity, gain_pct: float, milestone_pct: int, price_usd: float) -> str:
    multiple = 1 + (gain_pct / 100)
    milestone_multiple = 1 + (milestone_pct / 100)
    milestone_label = f"{milestone_multiple:.0f}x" if milestone_pct >= 200 else f"+{milestone_pct}%"
    return (
        f"📈 <b>WHALE ALPHA • QUOTE ALERT</b>\n"
        f"<i>{escape(o.symbol or o.name or o.mint[:8])} hit {escape(milestone_label)}</i>\n\n"
        f"🪙 <b>${escape(o.symbol or 'TOKEN')}</b>\n"
        f"🚀 <b>Gain:</b> +{gain_pct:.1f}%  <b>({multiple:.2f}x)</b>\n"
        f"🎯 <b>Milestone:</b> {escape(milestone_label)}\n"
        f"💵 <b>Price:</b> ${price_usd:.10f}\n"
        f"📊 <b>From signal:</b> ${o.alert_reference_price_usd:.10f}\n\n"
        f"🔥 <b>Momentum confirmed.</b>"
    )


@dataclass(frozen=True)
class TokenScore:
    total: float
    components: dict[str, float]
    risk_level: str
    risk_flags: tuple[str, ...]
    reasons: tuple[str, ...]


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def age_score(minutes: float) -> float:
    if minutes <= 10:
        return 100
    if minutes <= 30:
        return 92
    if minutes <= 60:
        return 82
    if minutes <= 180:
        return 68
    if minutes <= 360:
        return 48
    if minutes <= 720:
        return 25
    return 0


def _outcomes(session: AsyncSession, client: Any, env: Env, now: datetime, bot: Bot) -> None:
    raise RuntimeError("placeholder")
