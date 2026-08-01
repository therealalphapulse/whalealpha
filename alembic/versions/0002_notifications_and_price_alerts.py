"""Add User.notify_signals and the price_alerts table.

Revision ID: 0002_notifications_and_price_alerts
Revises: 0001_initial
Create Date: 2026-07-31

Backs three of the previously-missing features:
  * Signal -> notification wiring (User.notify_signals opt-out flag)
  * % price-increase alerts (new price_alerts table)

Same disclaimer as 0001: hand-authored against db/models.py, no live
Postgres to autogenerate against in this sandbox. Diff against
`alembic revision --autogenerate` before trusting this in production.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_notifications_and_price_alerts"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

alert_direction_enum = sa.Enum("UP", "DOWN", "BOTH", name="alertdirection", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    alert_direction_enum.create(bind, checkfirst=True)

    op.add_column(
        "users",
        sa.Column("notify_signals", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "price_alerts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_mint", sa.String(), nullable=False),
        sa.Column("threshold_pct", sa.Float(), nullable=False),
        sa.Column("direction", alert_direction_enum, nullable=False, server_default="BOTH"),
        sa.Column("reference_price_usd", sa.Float(), nullable=False),
        sa.Column("reset_on_trigger", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_price_alerts_active_token_mint", "price_alerts", ["active", "token_mint"]
    )


def downgrade() -> None:
    op.drop_index("ix_price_alerts_active_token_mint", table_name="price_alerts")
    op.drop_table("price_alerts")
    op.drop_column("users", "notify_signals")

    bind = op.get_bind()
    alert_direction_enum.drop(bind, checkfirst=True)
