"""
Role-Based Access Control for AlphaPulse administration.

Roles (least to most privileged for anything mutating):
  analyst          - read-only analytics/reports.
  support          - view users, no configuration permissions.
  premium_manager  - approve/reject/extend/cancel Premium, view payment requests.
  super_admin      - premium_manager permissions + broadcast + user moderation.
                     Cannot touch Owner-only settings or the Owner account.
  owner            - everything. Exactly one, set via OWNER_ID in .env.
                     Can never be removed or demoted (enforced in remove_admin
                     / change_role below, not just in the UI layer).

Legacy compatibility: anyone in config.settings.ADMIN_IDS is treated as
having every permission, same as Owner, so existing deployments don't
lose access the moment this ships. New installs should use OWNER_ID +
this module instead of growing ADMIN_IDS further.
"""

import logging
from sqlalchemy import select

from infra.db.session import async_session
from config.settings import OWNER_ID, ADMIN_IDS
from models.admin_role import AdminRole
from models.admin_activity_log import AdminActivityLog

logger = logging.getLogger("AlphaPulse.RBAC")

ROLES = ["owner", "super_admin", "premium_manager", "support", "analyst"]

ROLE_LABELS = {
    "owner": "👑 Owner",
    "super_admin": "🛡️ Super Admin",
    "premium_manager": "💎 Premium Manager",
    "support": "🎧 Support",
    "analyst": "📊 Analyst",
}

# Permission strings used throughout the bot. Every admin-facing handler
# should call has_permission(user_id, "...") with one of these rather
# than checking role strings directly, so adding/renaming permissions
# later doesn't require touching every handler.
ROLE_PERMISSIONS = {
    "owner": {"*"},  # wildcard - short-circuited in has_permission anyway
    "super_admin": {
        "approve_premium", "reject_premium", "extend_premium", "cancel_premium",
        "view_premium_users", "view_payment_requests",
        "broadcast", "view_analytics", "moderate_users",
        "view_admins",
    },
    "premium_manager": {
        "approve_premium", "reject_premium", "extend_premium", "cancel_premium",
        "view_premium_users", "view_payment_requests",
        "view_admins",
    },
    "support": {
        "view_users", "handle_support", "view_admins",
    },
    "analyst": {
        "view_analytics", "view_reports", "view_signal_stats", "view_admins",
    },
}

# Owner-only permissions — never granted to any other role, checked
# explicitly wherever they matter (add/remove admin, change role,
# configure payment methods/plans, system settings).
OWNER_ONLY_PERMISSIONS = {
    "manage_admins", "configure_premium", "configure_payment_methods",
    "configure_subscription_plans", "system_settings", "ban_unban_users",
}


async def ensure_owner_bootstrapped() -> None:
    """Call once at startup. Creates/repairs the Owner's admin_roles row
    so the Admin Panel has something to display for them; does NOT gate
    the Owner's actual access (that's always OWNER_ID-based, see
    has_permission/is_admin below), so this failing softly is fine."""
    if not OWNER_ID:
        logger.warning("OWNER_ID is not set — RBAC owner bootstrap skipped. Set OWNER_ID in .env.")
        return
    try:
        async with async_session() as session:
            result = await session.execute(select(AdminRole).where(AdminRole.user_id == OWNER_ID))
            row = result.scalar_one_or_none()
            if row:
                if row.role != "owner" or not row.is_active:
                    row.role = "owner"
                    row.is_active = True
                    await session.commit()
            else:
                session.add(AdminRole(user_id=OWNER_ID, role="owner", is_active=True, added_by="system"))
                await session.commit()
                logger.info(f"Owner bootstrapped: {OWNER_ID}")
    except Exception as e:
        logger.error(f"ensure_owner_bootstrapped failed: {e}")


async def get_role(user_id: int) -> str | None:
    if user_id == OWNER_ID:
        return "owner"
    async with async_session() as session:
        result = await session.execute(
            select(AdminRole).where(AdminRole.user_id == user_id, AdminRole.is_active == True)  # noqa: E712
        )
        row = result.scalar_one_or_none()
        return row.role if row else None


async def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID or user_id in ADMIN_IDS:
        return True
    return await get_role(user_id) is not None


async def has_permission(user_id: int, permission: str) -> bool:
    if user_id == OWNER_ID or user_id in ADMIN_IDS:
        return True
    role = await get_role(user_id)
    if not role:
        return False
    if permission in OWNER_ONLY_PERMISSIONS:
        return False  # owner-only, and we already excluded the owner above
    return permission in ROLE_PERMISSIONS.get(role, set())


async def list_admins() -> list[AdminRole]:
    async with async_session() as session:
        result = await session.execute(select(AdminRole).order_by(AdminRole.role, AdminRole.created_at))
        return result.scalars().all()


async def get_admin(user_id: int) -> AdminRole | None:
    async with async_session() as session:
        result = await session.execute(select(AdminRole).where(AdminRole.user_id == user_id))
        return result.scalar_one_or_none()


async def add_admin(user_id: int, role: str, username: str | None, added_by: int) -> tuple[bool, str]:
    if role not in ROLES or role == "owner":
        return False, "Invalid role."
    if user_id == OWNER_ID:
        return False, "That user is already the Owner."

    async with async_session() as session:
        result = await session.execute(select(AdminRole).where(AdminRole.user_id == user_id))
        existing = result.scalar_one_or_none()
        if existing:
            if existing.is_active:
                return False, "That user is already an administrator."
            existing.is_active = True
            existing.role = role
            existing.username = username or existing.username
            existing.added_by = str(added_by)
            await session.commit()
        else:
            session.add(AdminRole(
                user_id=user_id, role=role, username=username,
                is_active=True, added_by=str(added_by),
            ))
            await session.commit()

    await log_action(added_by, "add_admin", target_user_id=user_id, detail=f"role={role}")
    return True, f"Added as {ROLE_LABELS.get(role, role)}."


async def remove_admin(user_id: int, removed_by: int) -> tuple[bool, str]:
    if user_id == OWNER_ID:
        return False, "The Owner cannot be removed."
    async with async_session() as session:
        result = await session.execute(select(AdminRole).where(AdminRole.user_id == user_id))
        row = result.scalar_one_or_none()
        if not row or not row.is_active:
            return False, "That user isn't an active administrator."
        row.is_active = False
        await session.commit()

    await log_action(removed_by, "remove_admin", target_user_id=user_id)
    return True, "Administrator removed."


async def change_role(user_id: int, new_role: str, changed_by: int) -> tuple[bool, str]:
    if user_id == OWNER_ID:
        return False, "The Owner's role cannot be changed."
    if new_role not in ROLES or new_role == "owner":
        return False, "Invalid role."
    async with async_session() as session:
        result = await session.execute(select(AdminRole).where(AdminRole.user_id == user_id))
        row = result.scalar_one_or_none()
        if not row or not row.is_active:
            return False, "That user isn't an active administrator."
        old_role = row.role
        row.role = new_role
        await session.commit()

    await log_action(changed_by, "change_role", target_user_id=user_id, detail=f"{old_role} -> {new_role}")
    return True, f"Role changed to {ROLE_LABELS.get(new_role, new_role)}."


async def log_action(admin_user_id: int, action: str, target_user_id: int | None = None,
                      detail: str | None = None, admin_username: str | None = None) -> None:
    try:
        async with async_session() as session:
            session.add(AdminActivityLog(
                admin_user_id=admin_user_id, admin_username=admin_username,
                action=action, target_user_id=target_user_id, detail=detail,
            ))
            await session.commit()
    except Exception as e:
        logger.warning(f"log_action failed (admin={admin_user_id}, action={action}): {e}")


async def get_admin_ids_with_permission(permission: str) -> list[int]:
    """Used to notify the right people when something needs review (e.g.
    a manual payment proof) — Owner + legacy ADMIN_IDS always included
    since they implicitly have every permission."""
    ids = set(ADMIN_IDS)
    if OWNER_ID:
        ids.add(OWNER_ID)
    admins = await list_admins()
    for a in admins:
        if not a.is_active:
            continue
        if permission in ROLE_PERMISSIONS.get(a.role, set()):
            ids.add(a.user_id)
    return list(ids)


async def get_recent_activity(limit: int = 30) -> list[AdminActivityLog]:
    async with async_session() as session:
        result = await session.execute(
            select(AdminActivityLog).order_by(AdminActivityLog.created_at.desc()).limit(limit)
        )
        return result.scalars().all()
