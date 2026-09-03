from sqlalchemy import Column, BigInteger, String, DateTime, func
from infra.db.session import Base


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    last_active = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<User {self.telegram_id}>"
