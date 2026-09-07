from sqlalchemy import Column, BigInteger, String, DateTime, func, ForeignKey
from infra.db.session import Base


class AdminActivityLog(Base):
    """
    Audit trail for admin actions (add/remove admin, change role, and any
    other action logged via services.admin_rbac.log_action). Append-only.
    """
    __tablename__ = "admin_activity_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    admin_user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    admin_username = Column(String, nullable=True)
    action = Column(String, nullable=False)  # e.g. "add_admin", "remove_admin", "change_role"
    target_user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=True)
    detail = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    def __repr__(self):
        return f"<AdminActivityLog {self.admin_user_id}:{self.action}>"
