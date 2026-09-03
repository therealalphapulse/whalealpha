# AlphaPulse v4 — Database Migrations

This directory replaces v3's hand-written `migrate_*_schema()` functions
(previously in `signal_tracker.py`, `kol_tracker.py`, `paper_engine.py`,
`solana_wallet.py`, run unconditionally on every app boot) with versioned,
reviewable Alembic migrations — see the v4 Architecture Bible §7.

**Those hand-written functions still exist and still run at startup** (see
`app_platform/gateway/bootstrap.py`). They are not retired by this
directory's existence — only once a real `alembic revision --autogenerate`
pass has been run against a live copy of the production database and its
output verified to match `0001_baseline.py`'s intent (an empty stamp point,
because the schema already exists) should they be removed. That
verification could not be performed in the environment this migration
scaffolding was authored in (no live Postgres, no network access) — it is
the first thing to do with this directory before relying on it.

## Setup

```bash
pip install alembic
export DATABASE_URL=postgresql://...
```

## Adopting Alembic on an existing (already-running) database

```bash
alembic -c infra/db/alembic.ini stamp 0001_baseline
```

This tells Alembic "the database is already at this revision" without
running any SQL — correct for a database whose schema already exists via
`create_all()` + the old migrate_*_schema() functions.

## Fresh (new/empty) database

```bash
python -m app_platform.gateway.bootstrap   # runs init_db()'s create_all path
alembic -c infra/db/alembic.ini stamp 0001_baseline
```

## Rolling out the missing `users` foreign keys (0002 → 0003)

The audit found ~25 tables with a `user_id`-shaped column that was
indexed but had no `ForeignKey` constraint to `users.telegram_id` — the
`models/*.py` files were fixed in this v4 pass to declare these FKs at
the ORM level. Migrations `0002` and `0003` make that real in the
database, in two deliberately separate steps:

**Step 0 — table names are already verified against `models/*.py`'s
actual `__tablename__` values** (not guessed — see the comment at the top
of `_0002_add_user_foreign_keys_targets.py`). They have not been
cross-checked against a *live* database, since none was available while
authoring this. If a deployed database has ever diverged from what
`models/*.py` declares, `0002` will fail immediately and safely with a
Postgres "relation does not exist" error on the mismatched table —
resolve that before continuing rather than editing around it blindly.

**Step 1 — add constraints as `NOT VALID` (fast, safe on a live table):**

```bash
alembic -c infra/db/alembic.ini upgrade 0002_add_user_foreign_keys
```

This enforces the FK for all new writes immediately, without scanning or
locking existing rows.

**Step 2 — check for orphans before validating.** Run this query for
*every* table in `_0002_add_user_foreign_keys_targets.py` before
proceeding:

```sql
SELECT t.<column>
FROM <table> t
LEFT JOIN users u ON u.telegram_id = t.<column>
WHERE t.<column> IS NOT NULL AND u.telegram_id IS NULL;
```

Any rows returned are orphans — a `user_id` referencing a user that no
longer exists (or never did). Resolve each one manually (re-attach to the
correct user, or soft-delete/archive the row) before continuing. **Do
not** run step 3 while orphans exist; it will fail (safely — the
constraint stays `NOT VALID`, nothing breaks) rather than silently
deleting data on your behalf, by design (Bible §13 risk assessment).

**Step 3 — validate, once orphans are confirmed at zero:**

```bash
alembic -c infra/db/alembic.ini upgrade 0003_validate_user_foreign_keys
```

This is the point at which the constraints become fully enforced,
matching what `models/*.py` now declares at the ORM level.

## Going forward

All future schema changes go through a new Alembic revision:

```bash
alembic -c infra/db/alembic.ini revision --autogenerate -m "describe the change"
```

Review the generated migration before committing it — autogenerate is a
starting point, not a guarantee of correctness, especially for anything
involving data backfills or the same kind of phased rollout used above.
