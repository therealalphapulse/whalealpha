from sqlalchemy import Column, BigInteger, String, Float, Integer, Boolean, DateTime, func, ForeignKey
from infra.db.session import Base


class RealDCASchedule(Base):
    """
    A user-configured Dollar-Cost-Averaging schedule for the Real Wallet:
    "buy <amount_sol> of <contract> every <interval_seconds>, for
    <total_orders> orders", with optional price guards.

    This is intentionally a separate table from paper trading's
    PaperDCASettings/PaperDcaFill (models/paper_dca_settings.py,
    models/paper_dca_fill.py) — that system is a price-drawdown ladder
    bolted onto an existing paper position and is out of scope to touch.
    Real Wallet DCA is a standalone, interval-based order schedule: it
    does not require an existing open RealTrade, and every fill opens or
    adds to a RealTrade the same way a manual buy does (see
    services/real_dca_engine.py + services/real_trade_engine.execute_real_buy).

    Ticked by services/real_dca_engine.run_due_schedules(), invoked from
    the real_dca_scheduler_loop background task registered in main.py —
    same polling-loop pattern as services/paper_monitor.paper_monitor_loop
    and services/signal_tracker.signal_lifecycle_loop.
    """

    __tablename__ = "real_dca_schedules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    contract = Column(String, nullable=False)
    name = Column(String, nullable=True)
    symbol = Column(String, nullable=True)

    # Every order buys this many SOL worth of the token.
    amount_per_order_sol = Column(Float, nullable=False)

    # Minimum gap between orders. User-configurable, no fixed presets.
    interval_seconds = Column(Integer, nullable=False)

    # Total number of orders this schedule will place before completing.
    total_orders = Column(Integer, nullable=False)
    orders_filled = Column(Integer, nullable=False, default=0)

    # Optional guard rails — an order is skipped (not cancelled) if the
    # live price is outside this band when its turn comes up; it's
    # retried on the next interval tick instead of being lost.
    price_floor = Column(Float, nullable=True)
    price_ceiling = Column(Float, nullable=True)

    # Per-schedule trade settings; default to the wallet's own settings
    # (services.solana_wallet.get_wallet_settings) when left null.
    slippage_bps = Column(Integer, nullable=True)
    priority_fee_tier = Column(String, nullable=True)

    # "active" -> due orders fire on schedule.
    # "paused" -> user paused it manually, no orders fire, not completed.
    # "completed" -> total_orders reached.
    # "cancelled" -> user cancelled before completion.
    status = Column(String, nullable=False, default="active")

    # Consecutive failed-order counter. After too many in a row (see
    # services.real_dca_engine.MAX_CONSECUTIVE_FAILURES) the schedule is
    # auto-paused rather than silently retrying forever against, say, a
    # wallet that's run out of SOL.
    consecutive_failures = Column(Integer, nullable=False, default=0)

    next_run_at = Column(DateTime, nullable=False, server_default=func.now())
    last_run_at = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return (
            f"<RealDCASchedule user={self.user_id} {self.symbol} "
            f"{self.orders_filled}/{self.total_orders} {self.status}>"
        )
