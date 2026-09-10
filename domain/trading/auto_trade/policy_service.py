"""domain/trading/auto_trade/policy_service.py

§4 (User Auto-Trade Policy) and §5 (Configuration Snapshot).

Daily-counter reservation (`register_daily_trade` / `register_exposure`)
follows the exact same atomic "UPDATE ... WHERE current + delta <= cap
RETURNING id" pattern as domain/trading/real/solana_wallet.py's
register_auto_spend/register_auto_buy, applied to this engine's own
auto_trade_policies table -- so two concurrent evaluations can never
both reserve the same daily slot, without needing a separate lock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from sqlalchemy import select, text

from infra.db.session import async_session, engine
from models.auto_trade_policy import AutoTradePolicy

from .constants import (
    DEFAULT_SOL_PER_TRADE,
    DEFAULT_DAILY_TRADE_LIMIT,
    DEFAULT_MAX_OPEN_POSITIONS,
    DEFAULT_COOLDOWN_SECONDS,
)

logger = logging.getLogger("AlphaPulse.AutoTrade.Policy")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class TradePolicySnapshot:
    """§5 -- immutable snapshot taken at authorization time. Stored as
    JSON on AutoTradePosition.policy_snapshot_json so a later change to
    the user's live policy never mutates an already-open position's
    rules."""

    auto_trade_enabled: bool
    buy_amount_sol: float
    take_profit_pct: float | None
    stop_loss_pct: float | None
    trailing_stop_enabled: bool
    trailing_stop_pct: float | None
    slippage_bps: int
    priority_fee_tier: str
    max_position_size_usdt: float | None
    created_at: str
    # New, additive fields (appended at the end so dataclass field
    # ordering stays valid, and so TradePolicySnapshot.from_json can
    # unpack older snapshot JSON that predates these keys via their
    # defaults below -- no change to any existing field).
    trailing_activation_pct: float | None = None
    trailing_retracement_pct: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | None) -> "TradePolicySnapshot | None":
        if not raw:
            return None
        try:
            return TradePolicySnapshot(**json.loads(raw))
        except Exception:
            return None


async def get_policy(user_id: int) -> AutoTradePolicy | None:
    async with async_session() as session:
        result = await session.execute(select(AutoTradePolicy).where(AutoTradePolicy.user_id == user_id))
        return result.scalar_one_or_none()


async def get_or_create_policy(user_id: int) -> AutoTradePolicy:
    policy = await get_policy(user_id)
    if policy:
        return policy
    async with async_session() as session:
        policy = AutoTradePolicy(
            user_id=user_id,
            buy_amount_sol=DEFAULT_SOL_PER_TRADE,
            daily_trade_limit=DEFAULT_DAILY_TRADE_LIMIT,
            max_open_positions=DEFAULT_MAX_OPEN_POSITIONS,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
        )
        session.add(policy)
        await session.commit()
        await session.refresh(policy)
        return policy


async def update_policy_field(user_id: int, field: str, value) -> bool:
    """Generic single-field update, mirroring
    real_automation_engine.update_filter's shape. Only allows known
    columns to avoid ever writing an arbitrary attribute."""
    allowed = {
        "auto_trade_enabled", "kill_switch", "allowed_signal_tiers", "min_score",
        "buy_amount_sol", "take_profit_pct", "stop_loss_pct",
        "trailing_stop_enabled", "trailing_stop_pct",
        "trailing_activation_pct", "trailing_retracement_pct", "daily_trade_limit",
        "max_open_positions", "max_total_exposure_usdt", "max_position_size_usdt",
        "slippage_bps", "priority_fee_tier", "cooldown_seconds",
        "allow_multiple_positions_same_token",
    }
    if field not in allowed:
        return False
    policy = await get_or_create_policy(user_id)
    async with async_session() as session:
        db_policy = await session.get(AutoTradePolicy, policy.id)
        setattr(db_policy, field, value)
        if field == "auto_trade_enabled" and value:
            db_policy.auto_trade_enabled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
    return True


def snapshot_policy(policy: AutoTradePolicy) -> TradePolicySnapshot:
    return TradePolicySnapshot(
        auto_trade_enabled=bool(policy.auto_trade_enabled),
        buy_amount_sol=float(policy.buy_amount_sol or DEFAULT_SOL_PER_TRADE),
        take_profit_pct=policy.take_profit_pct,
        stop_loss_pct=policy.stop_loss_pct,
        trailing_stop_enabled=bool(policy.trailing_stop_enabled),
        trailing_stop_pct=policy.trailing_stop_pct,
        trailing_activation_pct=policy.trailing_activation_pct,
        trailing_retracement_pct=policy.trailing_retracement_pct,
        slippage_bps=int(policy.slippage_bps or 150),
        priority_fee_tier=policy.priority_fee_tier or "auto",
        max_position_size_usdt=policy.max_position_size_usdt,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def signal_is_after_activation(signal_detected_at, policy: AutoTradePolicy) -> bool:
    activation_at = policy.auto_trade_enabled_at
    if activation_at is None or signal_detected_at is None:
        return True
    if activation_at.tzinfo is None:
        activation_at = activation_at.replace(tzinfo=timezone.utc)
    if signal_detected_at.tzinfo is None:
        signal_detected_at = signal_detected_at.replace(tzinfo=timezone.utc)
    return signal_detected_at > activation_at


async def register_daily_trade(user_id: int, limit: int) -> dict:
    """Atomically reserve one daily trade slot (§6 DAILY_LIMIT_REACHED)."""
    if not 1 <= int(limit) <= 50:
        return {"ok": False, "reason": "Daily trade limit must be between 1 and 50."}
    today = _today_str()
    async with async_session() as session:
        result = await session.execute(text("""
            UPDATE auto_trade_policies
            SET daily_trade_count = CASE WHEN daily_trade_count_date = :today THEN COALESCE(daily_trade_count, 0) + 1 ELSE 1 END,
                daily_trade_count_date = :today
            WHERE user_id = :user_id AND auto_trade_enabled = true AND kill_switch = false
              AND (CASE WHEN daily_trade_count_date = :today THEN COALESCE(daily_trade_count, 0) ELSE 0 END) < :limit
            RETURNING id
        """), {"today": today, "user_id": user_id, "limit": int(limit)})
        reserved = result.first()
        await session.commit()
    if reserved:
        return {"ok": True}
    return {"ok": False, "reason": f"Daily Auto-Trade limit reached ({int(limit)} trades today)."}


async def release_daily_trade(user_id: int) -> None:
    today = _today_str()
    async with async_session() as session:
        await session.execute(text("""
            UPDATE auto_trade_policies
            SET daily_trade_count = GREATEST(0, COALESCE(daily_trade_count, 0) - 1)
            WHERE user_id = :user_id AND daily_trade_count_date = :today
        """), {"user_id": user_id, "today": today})
        await session.commit()


async def register_exposure(user_id: int, amount_usdt: float, cap_usdt: float | None) -> dict:
    """Atomically reserve `amount_usdt` of today's spend against
    max_total_exposure_usdt (§6 MAX_EXPOSURE_REACHED). If cap_usdt is
    None, exposure is unbounded and this always succeeds."""
    if cap_usdt is None:
        return {"ok": True}
    today = _today_str()
    async with async_session() as session:
        result = await session.execute(text("""
            UPDATE auto_trade_policies
            SET daily_spent_usdt = CASE WHEN daily_spent_date = :today THEN COALESCE(daily_spent_usdt, 0.0) + :amount ELSE :amount END,
                daily_spent_date = :today
            WHERE user_id = :user_id AND auto_trade_enabled = true AND kill_switch = false
              AND (CASE WHEN daily_spent_date = :today THEN COALESCE(daily_spent_usdt, 0.0) ELSE 0.0 END + :amount) <= :cap
            RETURNING id
        """), {"today": today, "user_id": user_id, "amount": amount_usdt, "cap": cap_usdt})
        reserved = result.first()
        await session.commit()
    if reserved:
        return {"ok": True}
    return {"ok": False, "reason": "Maximum total exposure reached for today."}


async def release_exposure(user_id: int, amount_usdt: float) -> None:
    today = _today_str()
    async with async_session() as session:
        await session.execute(text("""
            UPDATE auto_trade_policies SET daily_spent_usdt = GREATEST(0.0, COALESCE(daily_spent_usdt, 0.0) - :amount)
            WHERE user_id = :user_id AND daily_spent_date = :today
        """), {"user_id": user_id, "amount": amount_usdt, "today": today})
        await session.commit()


async def migrate_auto_trade_schema() -> None:
    """Idempotent production migration for the Auto-Trade Engine's policy table.

    This app's live boot path (Bible \u00a77/\u00a78) does not run Alembic --
    schema changes for tables introduced after the Alembic baseline are
    applied here on every boot, the same way as every other
    migrate_*_schema() function (see domain/trading/real/solana_wallet.py,
    domain/signals/signal_tracker.py, etc.), via the Procfile `release`
    step. (Two Alembic revision files under infra/db/migrations/ exist for
    this same change -- 0008_auto_trade_buy_amount_sol and
    0008_add_trailing_activation_and_retracement -- but both branch from
    0007 and neither is actually invoked by anything in this repo's boot
    sequence, so they never ran against production. This function is the
    one that actually applies.)

    buy_amount_usdt -> buy_amount_sol: the per-trade size was stored and
    labeled as a USDT amount but spent as-is on-chain (never actually
    converted at the point of storage). Existing dollar-denominated values
    can't be safely reinterpreted as a SOL amount, so the rename resets
    them to the new field's safe default -- this only fires once, the
    moment the old column is found and renamed; every boot after that the
    old column no longer exists, so this is a no-op.
    """
    statements = [
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='auto_trade_policies' AND column_name='buy_amount_usdt'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='auto_trade_policies' AND column_name='buy_amount_sol'
            ) THEN
                ALTER TABLE auto_trade_policies RENAME COLUMN buy_amount_usdt TO buy_amount_sol;
                UPDATE auto_trade_policies SET buy_amount_sol = {DEFAULT_SOL_PER_TRADE};
                ALTER TABLE auto_trade_policies ALTER COLUMN buy_amount_sol SET DEFAULT {DEFAULT_SOL_PER_TRADE};
            END IF;
        END $$;
        """,
        # Safety net for a brand-new DB where neither column exists yet
        # (init_db()'s create_all would already create this correctly from
        # the model, but this keeps the function self-sufficient like its
        # siblings).
        f"ALTER TABLE auto_trade_policies ADD COLUMN IF NOT EXISTS buy_amount_sol FLOAT DEFAULT {DEFAULT_SOL_PER_TRADE}",
        # Trailing Activation (%) / Trailing Retracement (%): additive,
        # nullable columns for the Auto-Trade trailing-stop feature. NULL
        # preserves existing behavior (trailing arms immediately, and the
        # legacy trailing_stop_pct is used as the retracement distance)
        # for every policy that hasn't set these yet -- see
        # domain/trading/auto_trade/exit_engine.py.
        "ALTER TABLE auto_trade_policies ADD COLUMN IF NOT EXISTS trailing_activation_pct FLOAT",
        "ALTER TABLE auto_trade_policies ADD COLUMN IF NOT EXISTS trailing_retracement_pct FLOAT",
    ]
    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
        logger.info("Auto-Trade policy schema migration complete")
    except Exception as e:
        logger.error(f"Auto-Trade policy schema migration error (non-fatal): {e}")
