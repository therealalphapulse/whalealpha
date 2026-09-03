"""
Premium membership service.

This is the single source of truth for Premium status. Every future
Premium-only feature should gate on `is_premium(user_id)` from this
module rather than querying PremiumMembership directly — that keeps the
permission check in exactly one place, so adding a new Premium-gated
command/feature later is just:

    from domain.payments.premium_service import is_premium, PREMIUM_REQUIRED_MESSAGE

    async def some_handler(...):
        if not await is_premium(user_id):
            await message.answer(PREMIUM_REQUIRED_MESSAGE)
            return
        ...

No schema or code changes are needed elsewhere to add more gated
features — just that one check at the top of the handler.
"""

"""
Premium membership service.

This is the single source of truth for Premium status. Every future
Premium-only feature should gate on `is_premium(user_id)` from this
module rather than querying PremiumMembership directly — that keeps the
permission check in exactly one place, so adding a new Premium-gated
command/feature later is just:

    from domain.payments.premium_service import is_premium, PREMIUM_REQUIRED_MESSAGE

    async def some_handler(...):
        if not await is_premium(user_id):
            await message.answer(PREMIUM_REQUIRED_MESSAGE)
            return
        ...

No schema or code changes are needed elsewhere to add more gated
features — just that one check at the top of the handler.

============================================================
Premium Intelligence Engine
============================================================
This module is also the single entry point that wires up the
autonomous Smart Wallet Intelligence Engine described in the Premium
rebuild: Discovery -> Scoring -> Monitoring/Consensus -> Maintenance.
See premium_master_loop() below, started once from main.py. Each stage
lives in its own service module so it can be reasoned about (and
rate-limited/tuned) independently:

    services/premium_wallet_discovery.py   — finds new candidate wallets
    services/premium_wallet_scorer.py      — reputation scoring
    services/premium_signal_engine.py      — monitoring + consensus + AI gate
    services/premium_wallet_maintenance.py — pruning / tiering / DB size
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from config.settings import (
    PREMIUM_DISCOVERY_INTERVAL_SECONDS,
    PREMIUM_MONITOR_INTERVAL_SECONDS,
    PREMIUM_SCORING_INTERVAL_SECONDS,
    PREMIUM_MAINTENANCE_INTERVAL_SECONDS,
)
from models.premium_membership import PremiumMembership

# Imported here (not just lazily inside start_premium_intelligence_engine)
# so these tables are registered with SQLAlchemy's Base.metadata before
# config.database.init_db() runs at startup — mirrors how every other
# model in this codebase gets registered via its owning module's import
# chain. Unused directly in this file; kept to guarantee create_all()
# sees them.
from models.premium_wallet import PremiumWallet as _PremiumWallet  # noqa: F401
from models.premium_wallet_trade import PremiumWalletTrade as _PremiumWalletTrade  # noqa: F401
from models.premium_signal import PremiumSignal as _PremiumSignal  # noqa: F401

logger = logging.getLogger("AlphaPulse.Premium")

PREMIUM_REQUIRED_MESSAGE = (
    "🔒 This feature is Premium-only.\n\n"
    "Use /premium to see your status and benefits."
)

# Central place to describe what Premium unlocks today. Extend this list
# as Premium-only features are added — the /premium command renders it
# directly, so no other file needs to change to update the pitch.
PREMIUM_BENEFITS = [
    "🧠 Premium Signals — AI + Smart Wallet consensus, highest-confidence only",
    "👛 Access to the live Smart Money wallet leaderboard (500-1,000+ wallets)",
    "⚡ Priority signal delivery",
    "🧬 DCA Auto-Buy strategy",
    "🧰 Unlimited saved Auto-Buy filter presets",
    "📊 Extended PnL history & analytics",
    "🎯 Early access to new AlphaPulse features",
]

# ============================================================
# Premium Trading Suite — cross-cutting Access Control + branding
# ============================================================
# Single source of truth for what the Advanced Trading Suite includes,
# rendered by /premium (bot/commands/premium.py) and referenced by the
# Upgrade Experience upsell shown from bot/commands/real_wallet.py.
# Every feature below is gated purely on is_premium(user_id) — see
# PREMIUM_GATED_TRADING_FEATURES for the upsell copy keyed by feature.

PREMIUM_TRADING_SUITE = [
    "🤖 Advanced Auto Buy & Auto Sell",
    "🎯 Take Profit / Stop Loss",
    "🎯 Partial Take Profit (multi-rung exits)",
    "📥 Limit Orders",
    "🧬 Fully Customizable DCA Engine (unlimited orders + price guards)",
    "📊 Advanced Position Management",
    "📈 Premium Trade Performance Analytics",
]

PREMIUM_INTELLIGENCE_FEATURES = [
    "🧠 Elite AI Signals",
    "🤝 Smart Wallet Consensus",
    "⭐ AlphaPulse AI Confidence",
    "📸 Premium Token Snapshot",
    "👥 Advanced Holder Intelligence",
    "📦 Advanced Bundle Analysis",
    "⚠️ Advanced Risk Analysis",
    "🚪 Entry Quality Assessment / Exit Confidence",
]

# Feature key -> (short title, one-line pitch) shown by the Upgrade
# Experience whenever a Free user taps a Premium-only control. Keyed by
# a short string each call site passes to premium_upsell_text() below —
# add an entry here and it's immediately available everywhere.
PREMIUM_GATED_TRADING_FEATURES = {
    "automation": (
        "Advanced Auto Buy / Auto Sell",
        "Let AlphaPulse buy and manage positions for you around the clock, "
        "filtered exactly the way you want.",
    ),
    "exit_rules": (
        "Take Profit / Stop Loss / Partial TP",
        "Set it once and AlphaPulse exits the position for you the moment "
        "your target or stop is hit — no need to watch the chart.",
    ),
    "limit_orders": (
        "Limit Orders",
        "Queue a buy at the price you want and AlphaPulse fires it "
        "automatically the instant the market gets there.",
    ),
    "dca_advanced": (
        "Fully Customizable DCA Engine",
        "Unlimited orders per schedule plus price-floor/ceiling guard rails, "
        "instead of the basic fixed schedule.",
    ),
}

# ============================================================
# Premium Signal Identity — one identifier, used everywhere
# ============================================================
# Single source of truth for the branding that must appear, byte-identical,
# on every Premium-exclusive surface: Signal Alerts, Token Snapshots, AI
# Analysis, and Notifications. Anything that renders Premium output should
# call format_premium_header()/format_premium_badge() rather than hardcode
# its own wording, so the identity can never drift between call sites.
PREMIUM_IDENTITY = "★ AlphaPulse PREMIUM"


def format_premium_header() -> str:
    """Top-of-card identity stamp for Premium Signal Alerts / Token
    Snapshots / AI Analysis. Callers still add their own emoji/title
    line below this if useful, but this exact line is what makes the
    card immediately recognizable as Premium-exclusive."""
    return f"{PREMIUM_IDENTITY}\n━━━━━━━━━━━━━━━━━━━━━"


def format_premium_badge() -> str:
    """Bottom-of-card / footer branding line stamped on every
    Premium-only alert or notification (Signal Alerts, Token Snapshot,
    AI Analysis, Notifications, Smart Wallet Consensus) — see Premium
    Signal Identity. Kept in one place so the wording/emoji never
    drifts between call sites."""
    return f"⚡ <i>Generated by {PREMIUM_IDENTITY} Intelligence</i>"


def premium_upsell_text(feature_key: str) -> str:
    """Upgrade Experience copy: brief reason + benefit, for a Free user
    who just tapped a Premium-only control. Never interrupts or degrades
    the surrounding Free flow — callers show this instead of the gated
    action, then let the user go right back to trading."""
    title, pitch = PREMIUM_GATED_TRADING_FEATURES.get(
        feature_key, ("This feature", "It's part of the Premium Trading Suite.")
    )
    return (
        f"🔒 <b>{title} is a Premium feature</b>\n\n"
        f"{pitch}\n\n"
        "Your Free trading (wallet, manual buy/sell, basic DCA, portfolio, "
        "history) keeps working exactly as it does today — this just adds "
        "more on top of it.\n\n"
        "💎 Tap below to see full Premium benefits and upgrade."
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_membership(session, user_id: int) -> PremiumMembership | None:
    res = await session.execute(
        select(PremiumMembership).where(PremiumMembership.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def get_or_create_membership(user_id: int) -> PremiumMembership:
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        if not membership:
            membership = PremiumMembership(user_id=user_id, status="expired")
            session.add(membership)
            await session.commit()
            await session.refresh(membership)
        return membership


async def is_premium(user_id: int) -> bool:
    """
    The single permission check every Premium-gated feature should use.
    Lazily expires a membership that's past its expires_at the moment
    it's checked, so status is always accurate even between sweep runs.
    """
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        if not membership or membership.status != "active":
            return False

        if membership.expires_at is not None and membership.expires_at <= _now():
            membership.status = "expired"
            await session.commit()
            return False

        return True


async def get_status(user_id: int) -> dict:
    """
    Full status snapshot for the /premium command: active/expired/never,
    remaining time if applicable, and expiry date.
    """
    async with async_session() as session:
        membership = await _get_membership(session, user_id)

        if not membership:
            return {"state": "never", "expires_at": None, "remaining": None}

        active = membership.status == "active"
        if active and membership.expires_at is not None and membership.expires_at <= _now():
            membership.status = "expired"
            await session.commit()
            active = False

        if not active:
            state = "revoked" if membership.status == "revoked" else "expired"
            return {"state": state, "expires_at": membership.expires_at, "remaining": None}

        if membership.expires_at is None:
            return {"state": "active_lifetime", "expires_at": None, "remaining": None}

        remaining = membership.expires_at - _now()
        return {"state": "active", "expires_at": membership.expires_at, "remaining": remaining}


async def activate_premium(user_id: int, duration_days: int | None = None, granted_by: str | None = None) -> PremiumMembership:
    """
    Activates Premium starting now. duration_days=None grants a lifetime
    membership (no expires_at). Overwrites any prior expiry — use
    extend_premium()/renew_premium() instead if you want to add time on
    top of an existing active membership.
    """
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        if not membership:
            membership = PremiumMembership(user_id=user_id)
            session.add(membership)

        membership.status = "active"
        membership.activated_at = _now()
        membership.expires_at = (_now() + timedelta(days=duration_days)) if duration_days else None
        membership.granted_by = granted_by
        membership.revoked_by = None

        await session.commit()
        await session.refresh(membership)

    logger.info(f"Premium activated for user {user_id} (duration_days={duration_days}, by={granted_by})")
    return membership


async def renew_premium(user_id: int, duration_days: int, granted_by: str | None = None) -> PremiumMembership:
    """
    Renews Premium for duration_days, extending from the LATER of "now"
    or the current expiry (so renewing before expiry doesn't lose the
    remaining time; renewing after expiry starts fresh from now).
    """
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        if not membership:
            membership = PremiumMembership(user_id=user_id)
            session.add(membership)

        base = membership.expires_at if (membership.expires_at and membership.expires_at > _now()) else _now()
        membership.status = "active"
        if membership.activated_at is None:
            membership.activated_at = _now()
        membership.expires_at = base + timedelta(days=duration_days)
        membership.granted_by = granted_by
        membership.revoked_by = None

        await session.commit()
        await session.refresh(membership)

    logger.info(f"Premium renewed for user {user_id} (+{duration_days}d, by={granted_by})")
    return membership


async def extend_premium(user_id: int, extra_days: int, granted_by: str | None = None) -> PremiumMembership:
    """Alias of renew_premium — kept as a distinct name since 'extend' and
    'renew' are both explicit requirements; behavior is identical (add
    time on top of whatever's left)."""
    return await renew_premium(user_id, extra_days, granted_by=granted_by)


async def revoke_premium(user_id: int, revoked_by: str | None = None) -> PremiumMembership | None:
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        if not membership:
            return None

        membership.status = "revoked"
        membership.revoked_by = revoked_by

        await session.commit()
        await session.refresh(membership)

    logger.info(f"Premium revoked for user {user_id} (by={revoked_by})")
    return membership


async def list_premium_users(status: str = "active", limit: int = 100) -> list[PremiumMembership]:
    async with async_session() as session:
        res = await session.execute(
            select(PremiumMembership)
            .where(PremiumMembership.status == status)
            .order_by(PremiumMembership.updated_at.desc())
            .limit(limit)
        )
        return res.scalars().all()


async def _sweep_once() -> list[int]:
    """Flips every stale active membership to expired, returns the
    affected user_ids. Shared by expire_stale_memberships() and the
    background loop below."""
    now = _now()
    async with async_session() as session:
        res = await session.execute(
            select(PremiumMembership).where(
                PremiumMembership.status == "active",
                PremiumMembership.expires_at.isnot(None),
                PremiumMembership.expires_at <= now,
            )
        )
        stale = res.scalars().all()
        expired_user_ids = [m.user_id for m in stale]
        for m in stale:
            m.status = "expired"
        if stale:
            await session.commit()

    return expired_user_ids


async def expire_stale_memberships() -> int:
    """
    Batch sweep: flips every "active" membership whose expires_at has
    passed to "expired". Run periodically (see premium_expiry_sweep_loop)
    so status is correct even for memberships nobody has checked via
    is_premium() recently; is_premium() itself is also always accurate on
    demand regardless of this sweep's cadence.
    """
    expired_user_ids = await _sweep_once()
    if expired_user_ids:
        logger.info(f"Premium expiry sweep: {len(expired_user_ids)} membership(s) expired")
    return len(expired_user_ids)


async def premium_expiry_sweep_loop(bot=None, interval_seconds: int = 3600):
    """
    Background loop: periodically expires stale memberships and (if a bot
    instance is provided) notifies affected users. Optional — is_premium()
    is correct even if this loop is never started, since it also checks
    expiry lazily on every call.
    """
    import asyncio

    logger.info("💎 Premium expiry sweep active")

    while True:
        try:
            expired_user_ids = await _sweep_once()

            if expired_user_ids:
                logger.info(f"Premium expiry sweep: {len(expired_user_ids)} membership(s) expired")

            if bot and expired_user_ids:
                for uid in expired_user_ids:
                    try:
                        await bot.send_message(
                            uid,
                            "💎 Your AlphaPulse Premium membership has expired.\n\n"
                            "Use /premium to renew and keep your Premium benefits active."
                        )
                    except Exception as e:
                        logger.warning(f"Premium expiry notification failed for {uid}: {e}")

        except Exception as e:
            logger.error(f"Premium expiry sweep error: {e}")

        await asyncio.sleep(interval_seconds)


# ============================================================
# Premium Signal alert opt-in (defaults on for every Premium member)
# ============================================================

async def set_signal_alerts(user_id: int, enabled: bool) -> None:
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        if not membership:
            membership = PremiumMembership(user_id=user_id, status="expired")
            session.add(membership)
        membership.signal_alerts_enabled = enabled
        await session.commit()


async def get_signal_alerts_enabled(user_id: int) -> bool:
    async with async_session() as session:
        membership = await _get_membership(session, user_id)
        return bool(membership.signal_alerts_enabled) if membership else True


# ============================================================
# Premium Intelligence Engine — master loop
# ============================================================
# Wires up Discovery / Scoring / Monitoring+Consensus / Maintenance as
# independent, self-healing background loops. Any one of them raising
# repeatedly cannot take the others down — each loop already catches
# and logs its own exceptions internally (see the respective service
# module), this function just starts all four as separate tasks.

async def start_premium_intelligence_engine(bot=None) -> None:
    """
    Call once at startup (see main.py). Runs schema migration for the
    new Premium tables, then launches the four autonomous loops. Safe
    to call even with an empty/fresh database — discovery will build
    the Smart Wallet DB up from zero with no manual seeding required.
    """
    from domain.intelligence.premium_wallet_discovery import (
        migrate_premium_schema,
        premium_discovery_loop,
    )
    from domain.intelligence.premium_wallet_scorer import premium_scoring_loop
    from domain.intelligence.premium_wallet_maintenance import premium_maintenance_loop
    from domain.intelligence.premium_signal_engine import premium_monitor_loop

    try:
        await migrate_premium_schema()
    except Exception as e:
        logger.error(f"Premium schema migration failed (non-fatal): {e}")

    logger.info("🚀 Premium Intelligence Engine starting (Discovery / Scoring / Monitor+Consensus / Maintenance)")

    asyncio.create_task(premium_discovery_loop(interval_seconds=PREMIUM_DISCOVERY_INTERVAL_SECONDS))
    asyncio.create_task(premium_scoring_loop(interval_seconds=PREMIUM_SCORING_INTERVAL_SECONDS))
    asyncio.create_task(premium_maintenance_loop(interval_seconds=PREMIUM_MAINTENANCE_INTERVAL_SECONDS))
    asyncio.create_task(premium_monitor_loop(interval_seconds=PREMIUM_MONITOR_INTERVAL_SECONDS, bot=bot))


async def get_premium_engine_stats() -> dict:
    from domain.intelligence.premium_wallet_maintenance import get_engine_stats
    return await get_engine_stats()


async def get_top_premium_wallets(limit: int = 15) -> list:
    from models.premium_wallet import PremiumWallet
    async with async_session() as session:
        res = await session.execute(
            select(PremiumWallet)
            .where(PremiumWallet.status == "active")
            .order_by(PremiumWallet.reputation_score.desc())
            .limit(limit)
        )
        return res.scalars().all()


async def get_recent_premium_signals(limit: int = 10) -> list:
    from domain.intelligence.premium_signal_engine import get_recent_premium_signals as _get
    return await _get(limit=limit)


async def trigger_manual_discovery_cycle() -> dict:
    """Manual override for admins (/premium_discover) — the engine never needs this to function normally."""
    from domain.intelligence.premium_wallet_discovery import run_discovery_cycle
    return await run_discovery_cycle()
