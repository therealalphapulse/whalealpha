"""
models/auto_trade_execution.py

The Auto-Trade Engine's Trade Ledger (§28 of the spec) -- one durable
row per buy/sell execution attempt. This is the audit trail: a
position's full history should be reconstructable from this table
without relying on logs. Brand-new, additive table.
"""

from sqlalchemy import Column, BigInteger, String, Float, DateTime, func
from infra.db.session import Base


class AutoTradeExecution(Base):
    __tablename__ = "auto_trade_executions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    position_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False, index=True)

    side = Column(String, nullable=False)  # "buy" | "sell"
    contract = Column(String, nullable=False)

    requested_amount = Column(Float, nullable=True)
    actual_amount = Column(Float, nullable=True)
    requested_price = Column(Float, nullable=True)
    actual_price = Column(Float, nullable=True)
    quote_amount = Column(Float, nullable=True)
    fees_sol = Column(Float, nullable=True, default=0.0)

    transaction_signature = Column(String, nullable=True, index=True)
    provider = Column(String, nullable=False, default="jupiter")
    route = Column(String, nullable=True)

    # "submitted" | "confirmed_success" | "confirmed_failure" | "unknown" (§12)
    status = Column(String, nullable=False, default="submitted")
    error = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    confirmed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<AutoTradeExecution pos={self.position_id} {self.side} {self.status}>"
