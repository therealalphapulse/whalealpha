"""High-potential token hunter persistence.

Revision ID: 0007_token_hunter
Revises: 0006_blockchain_scan_progress
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_token_hunter"
down_revision: str | None = "0006_blockchain_scan_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "token_opportunities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("mint", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=True),
        sa.Column("detection_source", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="SCORED"),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("age_minutes", sa.Float(), nullable=True),
        sa.Column("market_cap_usd", sa.Float(), nullable=True),
        sa.Column("liquidity_usd", sa.Float(), nullable=True),
        sa.Column("price_usd", sa.Float(), nullable=True),
        sa.Column("volume_5m_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("volume_1h_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("buys_5m", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sells_5m", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("buys_1h", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sells_1h", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("score_breakdown", sa.JSON(), nullable=True),
        sa.Column("risk_level", sa.String(), nullable=True),
        sa.Column("risk_flags", sa.ARRAY(sa.String()), nullable=False),
        sa.Column("key_reasons", sa.ARRAY(sa.String()), nullable=False),
        sa.Column("alert_status", sa.String(), nullable=False, server_default="NOT_ATTEMPTED"),
        sa.Column("alert_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alert_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alert_error", sa.String(), nullable=True),
        sa.Column("mc_after_5m", sa.Float(), nullable=True),
        sa.Column("mc_after_15m", sa.Float(), nullable=True),
        sa.Column("mc_after_30m", sa.Float(), nullable=True),
        sa.Column("mc_after_1h", sa.Float(), nullable=True),
        sa.Column("max_mc_usd", sa.Float(), nullable=True),
        sa.Column("min_mc_usd", sa.Float(), nullable=True),
        sa.Column("max_return_pct", sa.Float(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mint"),
    )
    op.create_index("ix_token_opportunities_status_detected_at", "token_opportunities", ["status", "detected_at"])
    op.create_index("ix_token_opportunities_score", "token_opportunities", ["score"])

    op.create_table(
        "token_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("opportunity_id", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("market_cap_usd", sa.Float(), nullable=True),
        sa.Column("liquidity_usd", sa.Float(), nullable=True),
        sa.Column("price_usd", sa.Float(), nullable=True),
        sa.Column("volume_5m_usd", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["token_opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_token_snapshots_opportunity_observed_at", "token_snapshots", ["opportunity_id", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_token_snapshots_opportunity_observed_at", table_name="token_snapshots")
    op.drop_table("token_snapshots")
    op.drop_index("ix_token_opportunities_score", table_name="token_opportunities")
    op.drop_index("ix_token_opportunities_status_detected_at", table_name="token_opportunities")
    op.drop_table("token_opportunities")
