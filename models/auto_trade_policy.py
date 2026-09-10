"""
models/auto_trade_policy.py

Per-user configuration for the standalone Trojan-inspired Auto-Trade
Engine (domain/trading/auto_trade/). This is a brand-new, additive
table -- it does not read, write, or alter models/real_autobuy_filter.py
or models/real_wallet.py's automation columns, which remain exactly as
they are for the existing AutoBuy/AutoTrade implementation
(domain/trading/real/). The two engines are fully isolated at the data
layer as well as the code layer; a user can in principle have either,
both, or neither enabled without any interaction between them.

The wallet itself (keys, on-chain balance) is still the user's single
RealWallet (models/real_wallet.py) -- that is shared, reused infra, not
duplicated. Only the *policy/automation* config and the resulting
positions/executions are new and separate.
"""

from sqlalchemy import Column, BigInteger, String, Float, Boolean, Integer, DateTime, func
from infra.db.session import Base


class AutoTradePolicy(Base):
    """A user's Auto-Trade Engine configuration (§4 of the spec).

    user_id is stored as a plain indexed BigInteger, not a declared
    ForeignKey -- same convention as models/auto_buy_claim.py, whose
    migration (0005_create_auto_buy_claims) also does not add a real
    FK constraint to the users table.
    """

    __tablename__ = "auto_trade_policies"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, unique=True, index=True)

    auto_trade_enabled = Column(Boolean, nullable=False, default=False)
    auto_trade_enabled_at = Column(DateTime, nullable=True)
    kill_switch = Column(Boolean, nullable=False, default=False)

    # Signal tier gate. Comma-separated list of allowed tiers (see
    # domain/trading/auto_trade/signal_adapter.py::score_to_tier), e.g.
    # "HIGH,MEDIUM". NULL/empty = all tiers allowed.
    allowed_signal_tiers = Column(String, nullable=True)
    min_score = Column(Float, nullable=True)

    # Canonical trade size in SOL, spent directly on each buy -- no
    # market-price conversion needed at execution time (see Buy Amount
    # fix: previously stored as a USDT amount and divided by a fetched
    # SOL/USD price to get the swap size; that indirection is gone).
    buy_amount_sol = Column(Float, nullable=False, default=0.1)

    take_profit_pct = Column(Float, nullable=True, default=50.0)
    stop_loss_pct = Column(Float, nullable=True, default=30.0)
    trailing_stop_enabled = Column(Boolean, nullable=False, default=False)
    trailing_stop_pct = Column(Float, nullable=True)

    # New, additive: minimum unrealized gain (%) from entry required
    # before the trailing exit arms and starts tracking the peak price.
    # NULL/0 preserves the exact legacy behavior (arms on any gain above
    # entry) for any policy created before this field existed.
    trailing_activation_pct = Column(Float, nullable=True)
    # New, additive: pullback (%) from the highest observed price,
    # post-activation, that triggers the Auto-Trade sell. When unset,
    # exit_engine falls back to trailing_stop_pct so existing configured
    # policies keep behaving exactly as they did before this field
    # existed.
    trailing_retracement_pct = Column(Float, nullable=True)

    daily_trade_limit = Column(Integer, nullable=False, default=5)
    max_open_positions = Column(Integer, nullable=False, default=5)
    max_total_exposure_usdt = Column(Float, nullable=True)
    max_position_size_usdt = Column(Float, nullable=True)

    slippage_bps = Column(Integer, nullable=False, default=150)
    priority_fee_tier = Column(String, nullable=False, default="auto")
    cooldown_seconds = Column(Integer, nullable=False, default=120)
    allow_multiple_positions_same_token = Column(Boolean, nullable=False, default=False)

    # Daily counters -- same UTC-day-bucket pattern as
    # models/real_wallet.py's auto_daily_spent_sol / auto_daily_buy_count,
    # but scoped to this engine's own trades only.
    daily_spent_usdt = Column(Float, nullable=False, default=0.0)
    daily_spent_date = Column(String, nullable=True)
    daily_trade_count = Column(Integer, nullable=False, default=0)
    daily_trade_count_date = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def has_tier_filter(self) -> bool:
        return bool(self.allowed_signal_tiers and self.allowed_signal_tiers.strip())

    def allowed_tiers_set(self) -> frozenset[str]:
        if not self.has_tier_filter():
            return frozenset()
        return frozenset(t.strip().upper() for t in self.allowed_signal_tiers.split(",") if t.strip())

    def __repr__(self):
        return (
            f"<AutoTradePolicy user={self.user_id} enabled={self.auto_trade_enabled} "
            f"buy_amount_sol={self.buy_amount_sol} tp={self.take_profit_pct} sl={self.stop_loss_pct}>"
        )
