from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, func, ForeignKey
from infra.db.session import Base


class RealExitRule(Base):
    """
    Premium Real Wallet exit automation — Take Profit / Stop Loss /
    Partial Take Profit rules attached to an open RealTrade.

    This is a standalone table (does not modify models/real_trade.py or
    services/real_trade_engine.py's schema) so it stays fully additive:
    a RealTrade with zero rules behaves exactly as it does today. Rules
    are ticked by services/real_exit_engine.py, which reuses
    services.real_trade_engine.execute_real_sell (fraction=...) for the
    actual on-chain sell — no duplicated swap/signing logic.

    Premium-gated: services/real_exit_engine.py only evaluates rules
    belonging to users who currently pass services.premium_service.is_premium
    (checked live on every tick, not just at creation) — see
    "Access Control" in the Premium Trading Suite spec. Free users can
    still trade normally; they just can't attach unattended TP/SL.

    kind:
        "tp"       — Take Profit: trigger when price >= entry_price * (1 + trigger_pct/100)
        "sl"       — Stop Loss:   trigger when price <= entry_price * (1 - trigger_pct/100)
        "ptp"      — Partial Take Profit: same trigger condition as "tp",
                     but sell_fraction is user-set (< 1.0) instead of always closing.

    A trade can have multiple rules at once (e.g. one SL + several PTP
    rungs) — each fires independently and once (status flips to
    "triggered" or "failed" and is not re-evaluated).
    """

    __tablename__ = "real_exit_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    trade_id = Column(BigInteger, nullable=False, index=True)

    kind = Column(String, nullable=False)  # "tp" | "sl" | "ptp"

    # Percentage move from entry_price that arms this rule (always
    # positive — direction is implied by `kind`). e.g. trigger_pct=50
    # on a "tp" rule fires at +50% from entry.
    trigger_pct = Column(Float, nullable=False)

    # Fraction of the position's remaining_quantity to sell when this
    # rule fires. 1.0 for a full TP/SL close; <1.0 for Partial TP rungs.
    sell_fraction = Column(Float, nullable=False, default=1.0)

    # "active" -> being watched by the exit engine.
    # "triggered" -> fired successfully, sell executed.
    # "cancelled" -> user removed it before firing.
    # "failed" -> fired, but the sell hit a *terminal* condition (no
    #             Real Wallet configured, the position was already fully
    #             closed elsewhere, or the sell amount rounds to zero) —
    #             kept for visibility, will not retry.
    #             Transient execution failures at fire time (an RPC or
    #             on-chain balance verification hiccup, a Jupiter
    #             quote/build/broadcast error, an on-chain rejection from
    #             slippage as price kept moving, etc.) do NOT land here:
    #             the rule stays "active" and real_exit_engine.py retries
    #             it on the next tick with a fresh balance and price.
    status = Column(String, nullable=False, default="active")

    tx_signature = Column(String, nullable=True)
    triggered_at = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<RealExitRule trade={self.trade_id} {self.kind} {self.trigger_pct}% x{self.sell_fraction} {self.status}>"
