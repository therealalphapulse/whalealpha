from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, func
from infra.db.session import Base


class PumpAlertedToken(Base):
    __tablename__ = "pump_alerted_tokens"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    contract = Column(String, nullable=False, unique=True, index=True)
    symbol = Column(String, nullable=True)
    pump_score = Column(Float, nullable=True)

    # Internal signal-history fields (never exposed to users — see
    # services/pump_radar.py cooldown gate). token_name captures the
    # display name alongside the already-present symbol; first_alerted_at
    # preserves the very first alert timestamp even as alerted_at is
    # updated on every subsequent re-arm; cooldown_expires_at is the
    # timestamp before which this contract must be ignored by every
    # signal generation process outright.
    token_name = Column(String, nullable=True)
    first_alerted_at = Column(DateTime, nullable=True)
    cooldown_expires_at = Column(DateTime, nullable=True)

    # How many times this contract has been alerted, including re-arms
    # (see services/pump_radar.py TP_REARM_MULTIPLE / REARM_RETRACE_RATIO).
    times_alerted = Column(Integer, nullable=True, default=1)

    # ath_multiple the SignalToken had at the moment of the most recent
    # alert (initial or re-arm). A re-arm is only allowed once
    # SignalToken.ath_multiple climbs strictly above this value again —
    # i.e. a genuinely new, higher high — not just because the price is
    # currently sitting in the retrace zone of the SAME high. Without
    # this, a token that pumped once and is merely chopping around near
    # the retrace threshold can flip was_already_alerted() true/false
    # across consecutive scan cycles and get re-alerted repeatedly for
    # one single pump, which is the bug this column fixes.
    last_alert_ath_multiple = Column(Float, nullable=True)

    alerted_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<PumpAlertedToken {self.contract}>"
