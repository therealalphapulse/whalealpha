"""Add bridge_withdrawals table (canonical Arbitrum bridge: Robinhood Chain -> Ethereum)."""
from alembic import op
import sqlalchemy as sa
revision="0010_add_bridge_withdrawals"
down_revision="0009_robinhood_chain_wallet"
branch_labels=None
depends_on=None

def upgrade():
    op.create_table(
        "bridge_withdrawals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id"), nullable=False, index=True),
        sa.Column("amount_eth", sa.Float(), nullable=False),
        sa.Column("destination_address", sa.String(), nullable=False),
        sa.Column("network", sa.String(), nullable=False, server_default="mainnet"),
        sa.Column("l2_tx_hash", sa.String(), nullable=False),
        sa.Column("l2_to_l1_position", sa.String(), nullable=False),
        sa.Column("l2_block_number", sa.BigInteger(), nullable=False),
        sa.Column("l2_block_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("initiated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("claimable_after", sa.DateTime(), nullable=False),
        sa.Column("claimed", sa.Boolean(), server_default=sa.false()),
        sa.Column("l1_claim_tx_hash", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claim_failure_reason", sa.String(), nullable=True),
    )

def downgrade():
    op.drop_table("bridge_withdrawals")
