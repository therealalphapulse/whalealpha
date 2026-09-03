from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, Float, Text, func
from infra.db.session import Base


class KolWallet(Base):
    __tablename__ = "kol_wallets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    label = Column(String, nullable=False)
    handle = Column(String, nullable=True)
    x_username = Column(String, nullable=True)

    wallet_address = Column(String, nullable=False, unique=True, index=True)

    category = Column(String, nullable=True, default="solana_kol")
    tags = Column(Text, nullable=True)

    provider = Column(String, nullable=True, default="kol_provider")
    provider_id = Column(String, nullable=True)

    source_url = Column(String, nullable=True)
    verification_status = Column(String, nullable=True, default="provider_synced")

    pnl_30d = Column(Float, nullable=True)
    win_rate = Column(Float, nullable=True)
    follower_count = Column(BigInteger, nullable=True)
    score = Column(Float, nullable=True)

    active = Column(Boolean, default=True)

    # Used by local transaction alerting / compatibility
    last_signature = Column(String, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    # Used by provider-sync alerting
    provider_last_signature = Column(String, nullable=True)
    provider_last_active = Column(String, nullable=True)

    raw_data = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<KolWallet {self.label}:{self.wallet_address}>"
