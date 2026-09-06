from sqlalchemy import Column, BigInteger, String, DateTime, func, ForeignKey
from infra.db.session import Base


class AdminActivityLog(Base):
    """
    Audit trail for admin actions (add/remove admin, change role, and any
    other action logged via services.admin_rbac.log_action). Append-only.

    admin_user_id / target_user_id are String, matching users.telegram_id
    -- despite models/user.py declaring telegram_id as BigInteger, the
    live production `users` table's telegram_id column is actually
    `character varying` (a legacy type this v4 model rewrite never
    reconciled against the deployed schema; every other table with a
    users.telegram_id FK already exists in production and was never
    re-validated against its current BigInteger model declaration, so
    the drift was silent). This table is new and had never been created
    before, so it was the first to surface the mismatch -- do not change
    this back to BigInteger without first migrating users.telegram_id
    itself (and every other FK to it) in a reviewed, staged migration.
    """
    __tablename__ = "admin_activity_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    admin_user_id = Column(String, ForeignKey("users.telegram_id"), nullable=False, index=True)
    admin_username = Column(String, nullable=True)
    action = Column(String, nullable=False)  # e.g. "add_admin", "remove_admin", "change_role"
    target_user_id = Column(String, ForeignKey("users.telegram_id"), nullable=True)
    detail = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    def __repr__(self):
        return f"<AdminActivityLog {self.admin_user_id}:{self.action}>"
