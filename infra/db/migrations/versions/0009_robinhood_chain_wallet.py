"""Migrate real wallet/trading identity from Solana to Robinhood Chain EVM."""
from alembic import op
import sqlalchemy as sa
revision="0009_robinhood_chain_wallet"
down_revision="0008_auto_trade_buy_amount_sol"
branch_labels=None
depends_on=None

def upgrade():
    op.add_column("real_wallets", sa.Column("chain_id", sa.Integer(), nullable=True))
    op.add_column("real_wallets", sa.Column("network", sa.String(), nullable=True))
    op.execute("UPDATE real_wallets SET is_active=FALSE WHERE is_active=TRUE")

def downgrade():
    op.drop_column("real_wallets","network")
    op.drop_column("real_wallets","chain_id")
