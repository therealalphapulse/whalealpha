"""Persist Token Hunter quote-alert delivery state.

Revision ID: 0008_quote_alert_state
Revises: 0007_token_hunter
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_quote_alert_state"
down_revision: str | None = "0007_token_hunter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("token_opportunities", sa.Column("alert_reference_price_usd", sa.Float(), nullable=True))
    op.add_column("token_opportunities", sa.Column("alert_message_ids", sa.JSON(), nullable=True))
    op.add_column("token_opportunities", sa.Column("quote_milestones", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("token_opportunities", "quote_milestones")
    op.drop_column("token_opportunities", "alert_message_ids")
    op.drop_column("token_opportunities", "alert_reference_price_usd")
