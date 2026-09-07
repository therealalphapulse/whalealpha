"""Temporary read-only diagnostic, part 2 -- check alembic_version state."""
import asyncio
from infra.db.session import engine
from sqlalchemy import text


async def main():
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT version_num FROM alembic_version"))
        print("DIAG_ALEMBIC_VERSION:", r.fetchall())

        r2 = await conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='users' ORDER BY ordinal_position"
        ))
        print("DIAG_USERS_COLUMNS:", r2.fetchall())


asyncio.run(main())
