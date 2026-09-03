"""validate the NOT VALID foreign keys added in 0002 (run only after confirming zero orphans)

Revision ID: 0003_validate_user_foreign_keys
Revises: 0002_add_user_foreign_keys
Create Date: v4.0 foundation

Do not run this migration as part of a routine `alembic upgrade head`
without first completing the orphan-check step described in
infra/db/migrations/README.md. `VALIDATE CONSTRAINT` scans the full table
and will fail the migration (leaving the constraint NOT VALID, which is
safe) if any existing row violates it — but running that scan against a
large, live table is exactly the kind of operation that should be a
deliberate, scheduled step, not something bundled silently into a
broader deploy.
"""

from alembic import op

from infra.db.migrations._0002_add_user_foreign_keys_targets import FK_TARGETS

revision = "0003_validate_user_foreign_keys"
down_revision = "0002_add_user_foreign_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, _column, constraint_name in FK_TARGETS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint_name};")


def downgrade() -> None:
    # Validating a constraint is not reversible in a meaningful sense
    # (Postgres has no "un-validate"); downgrading 0002 (which drops the
    # constraints entirely) is the correct rollback path if needed.
    pass
