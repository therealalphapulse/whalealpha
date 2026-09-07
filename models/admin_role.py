from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, func, Index, ForeignKey
from infra.db.session import Base


class AdminRole(Base):
    """
    RBAC role assignment for a Telegram admin (Owner, Super Admin,
    Premium Manager, Support, Analyst — see services/admin_rbac.py for
    the role/permission definitions). One row per admin user_id.

    user_id is String, matching the live users.telegram_id column type
    (see models/admin_activity_log.py for the full explanation: the
    live production `users` table's telegram_id is `character varying`,
    not BigInteger as models/user.py declares -- this table is new and
    had never been created before, so it surfaces the same drift).
    domain/admin/admin_rbac.py stringifies every int user_id before
    querying/constructing rows here and converts back to int on read
    where the return type is declared as int (e.g.
    get_admin_ids_with_permission).
    """
    __tablename__ = "admin_roles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)
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
