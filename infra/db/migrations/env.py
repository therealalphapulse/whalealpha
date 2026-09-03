"""
infra/db/migrations/env.py

NEW in v4 (Bible §7 — Database Evolution Strategy). This is the standard
Alembic env.py, wired to AlphaPulse's existing config: it reads
DATABASE_URL the same way `infra/db/session.py` does (no separate,
divergent config to keep in sync), and imports every model in `models/`
so `alembic revision --autogenerate` can diff against the real, complete
metadata rather than a partial one.

Usage (from the repo root, with a real DATABASE_URL and alembic
installed — neither is available in the sandbox this was authored in,
see infra/db/migrations/README.md):

    alembic -c infra/db/alembic.ini upgrade head
    alembic -c infra/db/alembic.ini revision --autogenerate -m "description"
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make the repo root importable (this file lives under infra/db/migrations/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from infra.db.session import Base  # noqa: E402
import models  # noqa: E402,F401  (imports every model so Base.metadata is complete)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("DATABASE_URL", "")
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
