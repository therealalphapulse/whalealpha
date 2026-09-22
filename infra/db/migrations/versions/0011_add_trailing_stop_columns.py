"""Add Trailing Stop columns: real_exit_rules.arm_pct/high_water_price, real_wallets.trail_default_pct/trail_default_arm_pct."""
from alembic import op
import sqlalchemy as sa

revision = "0011_add_trailing_stop_columns"
down_revision = "0010_add_bridge_withdrawals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("real_exit_rules", sa.Column("arm_pct", sa.Float(), nullable=False, server_default="0.0"))
    op.add_column("real_exit_rules", sa.Column("high_water_price", sa.Float(), nullable=True))
    op.add_column("real_wallets", sa.Column("trail_default_pct", sa.Float(), nullable=True, server_default="10.0"))
    op.add_column("real_wallets", sa.Column("trail_default_arm_pct", sa.Float(), nullable=True, server_default="0.0"))


def downgrade():
    op.drop_column("real_wallets", "trail_default_arm_pct")
    op.drop_column("real_wallets", "trail_default_pct")
    op.drop_column("real_exit_rules", "high_water_price")
    op.drop_column("real_exit_rules", "arm_pct")
