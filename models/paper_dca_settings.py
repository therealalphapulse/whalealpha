from sqlalchemy import Column, BigInteger, Float, Boolean, Integer, String, Text, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperDCASettings(Base):
    """
    Per-user Dollar-Cost Averaging configuration for the Paper Trading
    Auto-Buy engine. This does not create a separate trading system — it's
    read by services/paper_engine.py (the existing Auto-Buy engine) and by
    services/paper_monitor.py (the existing TP/SL price-check loop) to
    decide whether a new buy signal / price move should add to an already
    open position instead of behaving as a standalone trade.

    Two DCA triggers are supported, both funneling into the same
    add_dca_fill() core in paper_engine.py:
      1. Duplicate-signal merge: an Auto-Buy signal fires for a contract
         the user already has an open position in -> merged as a DCA fill
         instead of opening a second, separate position.
      2. Price-drawdown ladder: while a position is open, price drops
         through the configured level(s) below the current average entry
         -> triggers the next scheduled DCA fill automatically.

    Levels come from either the simple default (single trigger %% / amount
    reused for every fill) or a fully custom ladder (custom_levels_json),
    letting users define their own DCA strategy per the feature request.
    """

    __tablename__ = "paper_dca_settings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)

    enabled = Column(Boolean, default=False)

    # Total number of buys counted into one position, INCLUDING the
    # initial buy (e.g. max_entries=3 -> initial buy + 2 DCA add-ins).
    max_entries = Column(Integer, default=3)

    # Simple/default mode: every DCA level uses the same drop-trigger %%
    # and USD amount. Used whenever custom_levels_json is not set.
    default_trigger_drop_pct = Column(Float, default=15.0)
    default_entry_amount_usd = Column(Float, default=25.0)

    # Custom mode: JSON list of {"drop_pct": float, "amount_usd": float}
    # ordered by ascending drop_pct, one entry per DCA fill after the
    # initial buy. When set, this takes priority over the default fields
    # above and max_entries is derived from its length + 1.
    custom_levels_json = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PaperDCASettings user={self.user_id} enabled={self.enabled}>"
