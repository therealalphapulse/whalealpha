from sqlalchemy import Column, BigInteger, String, Float, Boolean, DateTime, func, ForeignKey
from infra.db.session import Base


class RealAutoBuyFilter(Base):
    """Per-user real-wallet automation settings and quality filters."""

    __tablename__ = "real_autobuy_filters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)

    min_score = Column(Float, nullable=True)
    min_market_cap = Column(Float, nullable=True)
    max_market_cap = Column(Float, nullable=True)
    min_liquidity_usd = Column(Float, nullable=True)
    max_bundle_pct = Column(Float, nullable=True)
    max_dev_holding_pct = Column(Float, nullable=True)

    # New user-facing automation settings. USDT is the canonical setting;
    # sol_per_trade remains for backwards compatibility with existing rows.
    auto_buy_amount_usdt = Column(Float, nullable=True)
    take_profit_pct = Column(Float, nullable=True)
    stop_loss_pct = Column(Float, nullable=True)
    daily_auto_buy_limit = Column(BigInteger, nullable=False, default=5)

    sol_per_trade = Column(Float, nullable=False, default=0.1)
    allow_multiple_positions_same_token = Column(Boolean, default=False)

    # Which Signal Alerts / events this wallet's automation is eligible to
    # act on. Historical values (unchanged behavior): "new" (only signals
    # delivered on the first attempt), "redelivered" (only signals whose
    # alert had to be retried -- see SignalToken.was_redelivered /
    # pump_radar.redeliver_undelivered_signal_alerts()), "both" (default --
    # no behavior change for existing users, equivalent to
    # "new_redelivered" below). Extended values add a signal's First
    # Milestone alert as an eligible auto-buy source (see
    # domain/trading/real/real_automation_engine.py::run_first_milestone_auto_buy
    # and ::signal_source_components): "first_milestone", "new_redelivered",
    # "new_first_milestone", "redelivered_first_milestone",
    # "new_redelivered_first_milestone". Validated in
    # real_automation_engine.update_filter().
    auto_buy_signal_source = Column(String, nullable=False, default="both")

    # Default Trailing Stop config, auto-attached to every position this
    # wallet's automation opens (mirrors how take_profit_pct/stop_loss_pct
    # above are auto-attached) -- see
    # domain/trading/real/real_trailing_stop_engine.attach_from_filter_if_enabled.
    # Snapshotted onto the resulting models/real_trailing_stop.py row at
    # creation time, so changing these later never mutates an
    # already-attached trailing stop. Manual buys get their own
    # independent "Set Trailing Stop" UI per position
    # (app_platform/commands/real_wallet.py) and do not read these
    # defaults -- these fields govern the automation path only.
    trailing_stop_enabled = Column(Boolean, nullable=False, default=False)
    trailing_activation_pct = Column(Float, nullable=True)
    trailing_pct = Column(Float, nullable=True)
    trailing_initial_stop_loss_pct = Column(Float, nullable=True)
    trailing_step_pct = Column(Float, nullable=True)
    # JSON string, e.g. '[{"gain_pct":50,"trail_pct":10},{"gain_pct":100,"trail_pct":5}]'
    trailing_profit_tiers = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def has_active_filters(self) -> bool:
        return any([
            self.min_score is not None,
            self.min_market_cap is not None,
            self.max_market_cap is not None,
            self.min_liquidity_usd is not None,
            self.max_bundle_pct is not None,
            self.max_dev_holding_pct is not None,
        ])

    def __repr__(self):
        return (
            f"<RealAutoBuyFilter user={self.user_id} "
            f"auto_buy_amount_usdt={self.auto_buy_amount_usdt} "
            f"daily_auto_buy_limit={self.daily_auto_buy_limit}>"
        )