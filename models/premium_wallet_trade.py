from sqlalchemy import Column, BigInteger, String, Float, DateTime, ForeignKey, func
from infra.db.session import Base


class PremiumWalletTrade(Base):
    """
    A single detected buy or sell by a tracked Premium (Smart Money)
    wallet. Rows are created by the monitoring pass in
    services/premium_signal_engine.py whenever a wallet's on-chain
    activity shows a token transfer in/out. The scorer
    (services/premium_wallet_scorer.py) turns sequences of these into
    win-rate / ROI / drawdown / holding-time statistics per wallet.

    This is intentionally a best-effort ledger, not exact accounting:
    entry/exit prices are snapshotted from DexScreener at detection
    time (not the exact fill price), which is good enough for relative
    wallet-quality scoring without needing a full indexer.
    """

    __tablename__ = "premium_wallet_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wallet_id = Column(BigInteger, ForeignKey("premium_wallets.id"), nullable=False, index=True)
    wallet_address = Column(String, nullable=False, index=True)

    token_mint = Column(String, nullable=False, index=True)
    token_symbol = Column(String, nullable=True)

    side = Column(String, nullable=False)  # "buy" | "sell"
    amount = Column(Float, nullable=True)
    price_usd_at_detection = Column(Float, nullable=True)
    value_usd_at_detection = Column(Float, nullable=True)
    entry_liquidity_usd = Column(Float, nullable=True)

    # Rug pull / honeypot / scam-token exposure tracking (buys only).
    # "1" = at least one known danger flag from services/goplus.py at
    # detection time, "0" = checked and clean, NULL = not checked
    # (e.g. security API unavailable) — never treated as risky by
    # default, per the "unknown != guilty" convention used elsewhere.
    is_flagged_risky = Column(String, nullable=True)
    risk_flags = Column(String, nullable=True)  # comma list, e.g. "honeypot,blacklisted"

    signature = Column(String, nullable=True, unique=True, index=True)
    detected_at = Column(DateTime, server_default=func.now(), index=True)

    # Populated once a matching sell is found for an earlier buy of the
    # same token by the same wallet (FIFO match) — see
    # services/premium_wallet_scorer.py._match_round_trips().
    closed = Column(String, nullable=True, default="open")  # "open" | "closed"
    roi_pct = Column(Float, nullable=True)
    hold_minutes = Column(Float, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<PremiumWalletTrade {self.wallet_address} {self.side} {self.token_symbol}>"
