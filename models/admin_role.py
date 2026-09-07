from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, func, Index, ForeignKey
from infra.db.session import Base


class AdminRole(Base):
    """
    RBAC role assignment for a Telegram admin (Owner, Super Admin,
    Premium Manager, Support, Analyst — see services/admin_rbac.py for
    the role/permission definitions). One row per admin user_id.
    """
    __tablename__ = "admin_roles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)
    username = Column(String, nullable=True)
    role = Column(String, nullable=False)  # owner | super_admin | premium_manager | support | analyst
    is_active = Column(Boolean, default=True, nullable=False)
    added_by = Column(String, nullable=True)  # "system" or the admin_user_id (str) who added them
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_admin_roles_role_created_at", "role", "created_at"),
    )

    def __repr__(self):
        return f"<AdminRole {self.user_id}:{self.role} active={self.is_active}>"
