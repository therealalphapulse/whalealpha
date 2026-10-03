"""signal_tokens: re-pump re-delivery tracking-cycle columns

Revision ID: 0014_add_signal_repump_tracking_cycles

Adds the columns behind re-pump re-delivery (domain/signals/
repump_redelivery.py). As with prior signal_tokens columns, the idempotent
`ADD COLUMN IF NOT EXISTS` statements in
domain/signals/signal_tracker.py::migrate_signal_schema() are what actually
run in production (bootstrap); this file is kept for parity/history.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_add_signal_repump_tracking_cycles"
down_revision = "0013_auto_trade_pause_and_claim_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_tokens", sa.Column("tracking_cycle", sa.Integer(), nullable=True, server_default="1"))
    op.add_column("signal_tokens", sa.Column("cycle_started_at", sa.DateTime(), nullable=True))
    op.add_column("signal_tokens", sa.Column("repump_trough_market_cap", sa.Float(), nullable=True))
    op.add_column("signal_tokens", sa.Column("repump_redelivery_count", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("signal_tokens", sa.Column("last_repump_redelivery_at", sa.DateTime(), nullable=True))
    op.add_column("signal_tokens", sa.Column("prior_cycles_json", sa.Text(), nullable=True, server_default="[]"))


def downgrade() -> None:
    for col in (
        "prior_cycles_json", "last_repump_redelivery_at", "repump_redelivery_count",
        "repump_trough_market_cap", "cycle_started_at", "tracking_cycle",
    ):
        op.drop_column("signal_tokens", col)
