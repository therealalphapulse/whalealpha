"""add robinhood_token_watch table

Revision ID: 0006_add_robinhood_token_watch
Revises: 0005_add_multi_discovery_signals
Create Date: WhaleAlpha Discovery Engine B v2 -- intelligent fresh/revival scoring

Adds robinhood_token_watch: per-contract price/liquidity history for
Discovery Engine B, updated every cycle for every candidate observed
(not just alerted ones). Backs the new REVIVAL lane's "dumped, now
recovering" detection, which needs multi-cycle local-high/local-low
tracking that a single DexScreener snapshot can't provide on its own.

Purely additive (new table only) and safely reversible. Independent of
robinhood_discovery_signals (the alert/dedupe ledger) -- no foreign key
or shared row links the two.
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_add_robinhood_token_watch"
down_revision = "0005_add_multi_discovery_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "robinhood_token_watch",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_contract", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("samples_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("local_high_price", sa.Float(), nullable=True),
        sa.Column("local_high_at", sa.DateTime(), nullable=True),
        sa.Column("local_low_price_since_high", sa.Float(), nullable=True),
        sa.Column("local_low_at", sa.DateTime(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("last_liquidity", sa.Float(), nullable=True),
        sa.Column("last_volume_1h", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("token_contract", name="uq_robinhood_token_watch_token_contract"),
    )
    op.create_index(
        "ix_robinhood_token_watch_token_contract",
        "robinhood_token_watch",
        ["token_contract"],
    )


def downgrade() -> None:
    op.drop_index("ix_robinhood_token_watch_token_contract", table_name="robinhood_token_watch")
    op.drop_table("robinhood_token_watch")
