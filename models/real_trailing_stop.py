from sqlalchemy import Column, BigInteger, String, Float, DateTime, Text, func, ForeignKey
from infra.db.session import Base


class RealTrailingStop(Base):
    """
    Real Wallet trailing-stop automation, attached to an open RealTrade.

    Additive-only (same convention as models/real_exit_rule.py): does not
    modify models/real_trade.py or services/real_trade_engine.py's
    schema. A RealTrade with no trailing stop behaves exactly as it does
    today. Ticked by domain/trading/real/real_trailing_stop_engine.py,
    which reuses services.real_trade_engine.execute_real_sell
    (fraction=...) for the actual on-chain sell -- no duplicated
    swap/signing logic, and the same atomic "open" -> "selling" claim on
    RealTrade means a trailing stop can never race a manual sell, a
    fixed TP/SL rule (models/real_exit_rule.py), or another trailing
    stop into a double-sell of the same position.

    Only ever created for a position opened via a manual buy or an
    enabled Auto Buy on Signal (RealTrade.source in {"manual",
    "automation"}) -- see
    domain/trading/real/real_trailing_stop_engine.attach_from_filter_if_enabled
    and app_platform/commands/real_wallet.py's manual "Trailing Stop"
    UI. DCA-sourced positions (source="dca") are never auto-attached
    and have no manual entry point either.

    Config (activation_pct/trail_pct/initial_stop_loss_pct/
    trailing_step_pct/profit_tiers_json) is snapshotted onto the row at
    creation time, exactly like RealExitRule.trigger_pct -- changing a
    user's default trailing-stop settings later never mutates an
    already-armed/watching row.

    State machine (status):
        "watching"  -- attached, position has not yet moved up enough
                       to arm the trailing stop. If initial_stop_loss_pct
                       is set, that protective floor is evaluated here.
        "armed"     -- price has cleared the activation threshold;
                       highest_price_seen/current_stop_price now update
                       as new highs print, and a close back through
                       current_stop_price triggers the exit.
        "triggered" -- fired successfully, sell executed.
        "cancelled" -- user cancelled it, a newer trailing stop
                       superseded it, or the position was closed by
                       some other mechanism (manual sell, a fixed TP/SL
                       rule, etc.) before this one fired.
        "failed"    -- fired, but the sell hit a *terminal* condition
                       (no Real Wallet configured, position already
                       fully closed elsewhere, sell amount rounds to
                       zero). Transient execution failures do NOT land
                       here -- the row stays "armed"/"watching" and the
                       engine retries next tick with a fresh price and
                       on-chain balance, same convention as
                       real_exit_engine.py.

    highest_price_seen and current_stop_price are persisted on every
    tick that changes them (not just in memory), so a worker
    restart/redeploy resumes from the exact watermark it left off at --
    it never has to (and never does) reset the peak back to entry price.
    """

    __tablename__ = "real_trailing_stops"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    trade_id = Column(BigInteger, nullable=False, index=True)

    entry_price = Column(Float, nullable=False)

    # --- snapshotted configuration (set once at creation) ---
    activation_pct = Column(Float, nullable=False)          # % gain from entry required to arm
    trail_pct = Column(Float, nullable=False)                # base retracement % from peak once armed
    initial_stop_loss_pct = Column(Float, nullable=True)      # protective floor while still "watching"
    trailing_step_pct = Column(Float, nullable=False, default=0.0)  # min peak improvement (%) before the stop is moved
    # JSON list of {"gain_pct": float, "trail_pct": float}, sorted ascending
    # by gain_pct -- profit-tier / dynamic trailing (tighter retracement the
    # further price runs). Empty/NULL means "always use trail_pct".
    profit_tiers_json = Column(Text, nullable=True)
    sell_fraction = Column(Float, nullable=False, default=1.0)

    # --- live, persisted state ---
    status = Column(String, nullable=False, default="watching")
    highest_price_seen = Column(Float, nullable=True)
    current_stop_price = Column(Float, nullable=True)
    last_price_seen = Column(Float, nullable=True)
    armed_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)

    tx_signature = Column(String, nullable=True)
    trigger_reason = Column(String, nullable=True)  # "initial_stop_loss" | "trailing_stop"
    triggered_at = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return (
            f"<RealTrailingStop trade={self.trade_id} {self.status} "
            f"activation={self.activation_pct}% trail={self.trail_pct}% stop={self.current_stop_price}>"
        )
