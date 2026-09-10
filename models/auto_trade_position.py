"""
models/auto_trade_position.py

Durable position + trade-lifecycle state for the standalone Auto-Trade
Engine (§10, §14, §15, §19, §27 of the spec). This is a brand-new
table -- fully separate from models/real_trade.py, which remains
exactly as-is for the existing AutoBuy/AutoTrade implementation.

One row per signal-driven trade attempt. `state` carries the position
through its entire lifecycle so a worker restart can always resume
correctly from durable DB state (§9/§33 "Crash Recovery") rather than
in-memory/log state.
"""

from sqlalchemy import Column, BigInteger, String, Float, Integer, Boolean, DateTime, Text, func
from infra.db.session import Base


class AutoTradeState:
    INTENT_CREATED = "TRADE_INTENT_CREATED"
    BUY_VALIDATING = "BUY_VALIDATING"
    BUY_QUOTING = "BUY_QUOTING"
    BUY_TX_BUILDING = "BUY_TX_BUILDING"
    BUY_SUBMITTED = "BUY_SUBMITTED"
    BUY_CONFIRMING = "BUY_CONFIRMING"
    BUY_CONFIRMED = "BUY_CONFIRMED"
    BUY_FAILED = "BUY_FAILED"
    BUY_RECONCILING = "BUY_RECONCILING"

    POSITION_OPEN = "POSITION_OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    EXIT_TRIGGERED = "EXIT_TRIGGERED"
    SELL_VALIDATING = "SELL_VALIDATING"
    SELL_BALANCE_RESOLVING = "SELL_BALANCE_RESOLVING"
    SELL_QUOTING = "SELL_QUOTING"
    SELL_TX_BUILDING = "SELL_TX_BUILDING"
    SELL_SUBMITTED = "SELL_SUBMITTED"
    SELL_CONFIRMING = "SELL_CONFIRMING"
    SELL_CONFIRMED = "SELL_CONFIRMED"
    SELL_FAILED = "SELL_FAILED"
    SELL_RECONCILING = "SELL_RECONCILING"

    RECONCILING = "RECONCILING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CLOSED = "CLOSED"
    ERROR = "ERROR"

    NON_TERMINAL = frozenset({
        INTENT_CREATED, BUY_VALIDATING, BUY_QUOTING, BUY_TX_BUILDING,
        BUY_SUBMITTED, BUY_CONFIRMING, BUY_RECONCILING,
        POSITION_OPEN, PARTIALLY_CLOSED, EXIT_TRIGGERED,
        SELL_VALIDATING, SELL_BALANCE_RESOLVING, SELL_QUOTING,
        SELL_TX_BUILDING, SELL_SUBMITTED, SELL_CONFIRMING,
        SELL_RECONCILING, RECONCILING, RECONCILIATION_REQUIRED,
    })

    OPEN_LIKE = frozenset({POSITION_OPEN, PARTIALLY_CLOSED, EXIT_TRIGGERED,
                            SELL_VALIDATING, SELL_BALANCE_RESOLVING, SELL_QUOTING,
                            SELL_TX_BUILDING, SELL_SUBMITTED, SELL_CONFIRMING,
                            SELL_RECONCILING})


class AutoTradePosition(Base):
    __tablename__ = "auto_trade_positions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    signal_id = Column(BigInteger, nullable=True, index=True)
    claim_id = Column(BigInteger, nullable=True, index=True)

    contract = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    symbol = Column(String, nullable=True)

    state = Column(String, nullable=False, default=AutoTradeState.INTENT_CREATED, index=True)

    policy_snapshot_json = Column(Text, nullable=True)

    signal_price = Column(Float, nullable=True)
    entry_price = Column(Float, nullable=True)
    requested_usdt = Column(Float, nullable=True)
    sol_spent = Column(Float, nullable=True)
    token_quantity = Column(Float, nullable=True)
    remaining_quantity = Column(Float, nullable=True)
    token_decimals = Column(Integer, nullable=True)
    buy_tx_signature = Column(String, nullable=True)
    buy_fees_sol = Column(Float, nullable=True, default=0.0)

    highest_observed_price = Column(Float, nullable=True)
    trailing_trigger_price = Column(Float, nullable=True)

    total_cost_basis_sol = Column(Float, nullable=True)
    realized_proceeds_sol = Column(Float, nullable=False, default=0.0)
    realized_pnl_sol = Column(Float, nullable=False, default=0.0)
    realized_pnl_pct = Column(Float, nullable=True)
    total_fees_sol = Column(Float, nullable=False, default=0.0)
    average_exit_price = Column(Float, nullable=True)

    exit_reason = Column(String, nullable=True)
    last_error = Column(String, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)

    opened_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<AutoTradePosition {self.symbol} user={self.user_id} state={self.state}>"
