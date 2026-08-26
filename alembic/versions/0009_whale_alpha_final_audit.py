"""Persist mandatory Whale Alpha final release audits.

Revision ID: 0009_whale_alpha_final_audit
Revises: 0008_quote_alert_state
"""
from __future__ import annotations
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0009_whale_alpha_final_audit"
down_revision: str | None = "0008_quote_alert_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "whale_alpha_audits",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("analysis_id", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=False),
        sa.Column("strategy_version", sa.String(), nullable=False),
        sa.Column("rules_version", sa.String(), nullable=False),
        sa.Column("scoring_model_version", sa.String(), nullable=False),
        sa.Column("audit_mode", sa.String(), nullable=False),
        sa.Column("token_mint", sa.String(), nullable=False),
        sa.Column("pair_address", sa.String(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("final_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("final_tier", sa.String(), nullable=False, server_default="NO SIGNAL"),
        sa.Column("findings", sa.ARRAY(sa.String()), nullable=False),
        sa.Column("corrections", sa.ARRAY(sa.String()), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id"),
    )
    op.create_index("ix_whale_alpha_audits_token_created_at", "whale_alpha_audits", ["token_mint", "created_at"])
    op.create_index("ix_whale_alpha_audits_approved_created_at", "whale_alpha_audits", ["approved", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_whale_alpha_audits_approved_created_at", table_name="whale_alpha_audits")
    op.drop_index("ix_whale_alpha_audits_token_created_at", table_name="whale_alpha_audits")
    op.drop_table("whale_alpha_audits")
