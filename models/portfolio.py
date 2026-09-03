from sqlalchemy import Column, BigInteger, String, Float, DateTime, func, UniqueConstraint, ForeignKey
from infra.db.session import Base


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    contract = Column(String, nullable=False)

    token_name = Column(String, nullable=True)
    token_symbol = Column(String, nullable=True)

    token_amount = Column(Float, nullable=False, default=0.0)
    entry_price = Column(Float, nullable=False, default=0.0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "contract", name="uq_user_portfolio_contract"),
    )

    def __repr__(self):
        return f"<PortfolioPosition {self.user_id}:{self.contract}>"
