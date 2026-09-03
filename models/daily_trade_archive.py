from sqlalchemy import Column, BigInteger, Integer, Float, String, DateTime, func, UniqueConstraint, ForeignKey
from infra.db.session import Base


class DailyTradeArchive(Base):
    """
    One row per user per calendar day (UTC): a closed-out snapshot of that
    day's paper trading activity (trades opened/closed, wins/losses, net
    PnL, ending balance).

    Purely additive/historical — this table is the ONLY thing the daily
    reset job (services/paper_engine.py: archive_daily_trades_for_all_users)
    ever writes to. It never touches PaperPortfolio balances, PaperTrade
    rows, PaperSettings, PaperAutoBuyFilter, Watchlist, or TrackedWallet.
    Open positions carry over across the day boundary untouched, and the
    per-user daily auto-buy limit is already computed live from today's
    date (see get_trades_opened_today_count), so a fresh trading day starts
    naturally at midnight with no destructive reset required at all.
    """
    __tablename__ = "daily_trade_archives"
    __table_args__ = (
        UniqueConstraint("user_id", "archive_date", name="uq_daily_trade_archive_user_date"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    archive_date = Column(String, nullable=False, index=True)  # 'YYYY-MM-DD' (UTC)

    trades_opened = Column(Integer, default=0)
    trades_closed = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    net_pnl_usd = Column(Float, default=0.0)
    ending_balance = Column(Float, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<DailyTradeArchive user={self.user_id} date={self.archive_date}>"
