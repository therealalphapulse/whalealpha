from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, func, ForeignKey
from infra.db.session import Base


class RealLimitOrder(Base):
    """
    Premium Real Wallet Limit Orders — "buy this token automatically if
    its price reaches <trigger_price>", ticked by
    services/real_limit_order_engine.py and executed through
    services.real_trade_engine.execute_real_buy (source="limit_order")
    once the condition is met. Standalone table, no changes to any
    existing model.

    Premium-gated the same way as models/real_exit_rule.py — see
    services/real_limit_order_engine.py.

    direction:
        "buy_below" — fires when live price <= trigger_price (classic
                      "limit buy the dip").
        "buy_above" — fires when live price >= trigger_price (breakout /
                      momentum entry).
    """

    __tablename__ = "real_limit_orders"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    contract = Column(String, nullable=False)
    name = Column(String, nullable=True)
    symbol = Column(String, nullable=True)

    direction = Column(String, nullable=False, default="buy_below")
    trigger_price = Column(Float, nullable=False)

    sol_amount = Column(Float, nullable=False)
    slippage_bps = Column(Integer, nullable=True)
    priority_fee_tier = Column(String, nullable=True)

    # "pending" -> being watched.
    # "filled" -> condition met, buy executed.
    # "cancelled" -> user cancelled before filling.
    # "failed" -> condition met but the buy itself failed (not retried
    #             automatically, to avoid hammering a broken swap route).
    # "expired" -> past expires_at without filling.
    status = Column(String, nullable=False, default="pending")

    tx_signature = Column(String, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)

    # Optional — a limit order left open forever against a dead/illiquid
    # token is just dead weight; nullable means "never expires".
    expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<RealLimitOrder user={self.user_id} {self.symbol} {self.direction}@{self.trigger_price} {self.status}>"
