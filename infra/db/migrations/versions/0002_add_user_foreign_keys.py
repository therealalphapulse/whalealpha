"""add foreign keys from user-owned tables to users.telegram_id (phased)

Revision ID: 0002_add_user_foreign_keys
Revises: 0001_baseline
Create Date: v4.0 foundation

NEW in v4 (Bible §7 + §13 Risk Assessment). The audit found ~25 tables
with a `user_id`/`admin_user_id`/`target_user_id` column that was indexed
but had no `ForeignKey` constraint back to `users.telegram_id` — the
database could not itself prevent an orphaned row referencing a
nonexistent user (models/*.py were fixed to declare these FKs at the ORM
level in this same v4 pass; this migration is what makes them real in an
already-running database that predates that change).

This is NOT a single blind `ADD CONSTRAINT` — that would table-scan and
lock every one of these tables, and would fail outright (rolling back the
whole migration) if even one existing row already has an orphaned
user_id, which is realistic to encounter on a database that's been
accepting user_id values with no enforcement for its entire history.

Instead, per the Bible's explicit phased plan:

  1. `upgrade()` below adds every FK as `NOT VALID` — Postgres enforces it
     for all NEW writes immediately, but does not scan/lock existing rows,
     so this step is fast and safe to run against a live, populated
     database.
  2. Before running step 3, an operator must run the orphan-check query
     (see infra/db/migrations/README.md) against each table and resolve
     any orphaned rows found (re-attach or soft-delete) — this migration
     deliberately does NOT do that resolution automatically, since
     silently deleting user data is not a decision a migration should
     make unattended.
  3. Once orphans are confirmed at zero, run the companion
     `0003_validate_user_foreign_keys` migration (validates every
     constraint added here, converting it from NOT VALID to fully
     enforced) — kept as a separate revision specifically so it's a
     distinct, deliberate step an operator runs only after confirming
     step 2, not something that happens automatically on `upgrade head`.

This migration has not been run against a live AlphaPulse database in
this environment (no network access, no live Postgres available where it
was authored) — it is written to the documented Postgres `NOT VALID` /
`VALIDATE CONSTRAINT` behavior and should be treated with the same
run-in-staging-first discipline as any other schema change to a database
already holding real user data.
"""

from alembic import op

from infra.db.migrations._0002_add_user_foreign_keys_targets import FK_TARGETS as _FK_TARGETS

revision = "0002_add_user_foreign_keys"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, column, constraint_name in _FK_TARGETS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD CONSTRAINT {constraint_name} "
            f"FOREIGN KEY ({column}) REFERENCES users(telegram_id) "
            f"NOT VALID;"
        )


def downgrade() -> None:
    for table, _column, constraint_name in _FK_TARGETS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint_name};")
