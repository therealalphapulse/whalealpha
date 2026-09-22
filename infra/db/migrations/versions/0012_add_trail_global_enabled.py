"""Add global Trailing Stop toggle: real_wallets.trail_global_enabled."""
from alembic import op
import sqlalchemy as sa

revision = "0012_add_trail_global_enabled"
down_revision = "0011_add_trailing_stop_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("real_wallets", sa.Column("trail_global_enabled", sa.Boolean(), nullable=True, server_default="false"))


def downgrade():
    op.drop_column("real_wallets", "trail_global_enabled")
