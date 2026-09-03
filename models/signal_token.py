from sqlalchemy import Column, BigInteger, String, Float, Boolean, DateTime, func, Text
from sqlalchemy.orm import relationship
from infra.db.session import Base

class SignalToken(Base):
    __tablename__ = "signal_tokens"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    contract = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=True)
    symbol = Column(String, nullable=True)

    # Socials for Leaderboard
    twitter_url = Column(String, nullable=True)
    telegram_url = Column(String, nullable=True)

    # Message IDs for Reply-Tagging (JSON: {"user_id": message_id})
    message_ids_json = Column(Text, nullable=True, default="{}")

    # Entry Data
    entry_price = Column(Float, nullable=True)
    entry_market_cap = Column(Float, nullable=True)
    entry_liquidity = Column(Float, nullable=True)
    entry_score = Column(Float, nullable=True)

    # Live Data
    current_price = Column(Float, nullable=True)
    current_market_cap = Column(Float, nullable=True)
    current_liquidity = Column(Float, nullable=True)

    # Performance
    ath_price = Column(Float, nullable=True)
    ath_market_cap = Column(Float, nullable=True)
    current_multiple = Column(Float, nullable=True, default=1.0)
    ath_multiple = Column(Float, nullable=True, default=1.0)
    highest_alerted_multiple = Column(Float, nullable=True, default=1.0)
    lowest_alerted_multiple = Column(Float, nullable=True, default=1.0)

    pair_url = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    status = Column(String, nullable=True, default="active")

    # Production incident fix (see PRODUCTION_AUDIT_REPORT.md /
    # SIGNAL_ENGINE_REEVALUATION.md history): `status == "active"` is
    # set the instant this row is created, before the Signal Alert
    # card has actually been delivered to any subscriber -- it is NOT
    # evidence anyone was ever alerted. `alert_delivered` is only ever
    # flipped True by signal_tracker.mark_signal_alert_delivered(),
    # called from domain/signals/pump_radar.py::pump_radar_loop AFTER
    # the subscriber-delivery loop confirms at least one successful
    # send. Both the paper auto-buy path (pump_radar.auto_buy_for_new_signal)
    # and the real-money automation engine
    # (domain/trading/real/real_automation_engine.py) require this to
    # be True before treating a signal as buy-eligible -- neither may
    # trade off `status == "active"` alone.
    alert_delivered = Column(Boolean, nullable=False, default=False, server_default="false")
    alert_delivered_at = Column(DateTime, nullable=True)

    # True only when this signal's Signal Alert reached subscribers via
    # domain/signals/pump_radar.py::redeliver_undelivered_signal_alerts()
    # (the first attempt failed to everyone), never on a normal first-try
    # delivery. Lets the real-wallet auto-buy "signal source" filter
    # (RealAutoBuyFilter.auto_buy_signal_source) distinguish New vs
    # Redelivered signals. Defaults False so every existing row reads as
    # a normal fresh alert.
    was_redelivered = Column(Boolean, nullable=False, default=False)

    # First Milestone Snapshot support (see domain/signals/signal_tracker.py
    # ::send_milestone_alert). Per-chat map of {chat_id: message_id} for the
    # First Milestone Snapshot message sent to a chat that never received
    # this signal's original Signal Alert -- mirrors message_ids_json, but
    # tracks the Snapshot (root) message instead, so every later milestone
    # for that same chat can quote/reply to it instead of the (nonexistent)
    # original alert. Only ever populated on a signal's first milestone.
    first_milestone_message_ids_json = Column(Text, nullable=True, default="{}")

    # Holder / bundle / dev analysis snapshot (refreshed on each milestone
    # alert so follow-up cards show live data too)
    total_holders = Column(BigInteger, nullable=True)
    top_holder_pct = Column(Float, nullable=True)
    top10_holder_pct = Column(Float, nullable=True)
    top25_holder_pct = Column(Float, nullable=True)
    dev_holding_pct = Column(Float, nullable=True)
    bundle_wallet_count = Column(BigInteger, nullable=True)
    bundle_pct = Column(Float, nullable=True)

    # Snapshot of the 4-component conviction-scorer breakdown at the
    # moment this signal was created (JSON: liquidity/holder/momentum/
    # wallet/narrative sub-scores). Lets services/signal_calibration.py
    # later check which components actually predicted ath_multiple,
    # instead of guessing at scoring weights forever.
    entry_breakdown_json = Column(Text, nullable=True)

    signaled_at = Column(DateTime, server_default=func.now())
    last_checked_at = Column(DateTime, nullable=True)

    events = relationship("SignalEvent", back_populates="signal", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<SignalToken {self.symbol}>"
