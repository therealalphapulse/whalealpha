"""
One-off, read-only diagnostic used to safely inspect the live database
before deciding how to fix the users.telegram_id type drift (see git
history around 2026-09-06/07). Prints results and exits 0 -- never
modifies anything. Safe to run repeatedly; remove once the schema
question is resolved.
"""
import asyncio
from infra.db.session import engine
from sqlalchemy import text


async def main():
    async with engine.begin() as conn:
        r = await conn.execute(text("SELECT COUNT(*) FROM users"))
        print("DIAG_USERS_ROW_COUNT:", r.scalar())

        r2 = await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ))
        print("DIAG_EXISTING_TABLES:", [row[0] for row in r2.fetchall()])

        r3 = await conn.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='users' AND column_name='telegram_id'"
        ))
        print("DIAG_TELEGRAM_ID_TYPE:", r3.scalar())

        r4 = await conn.execute(text(
            "SELECT telegram_id, username FROM users ORDER BY created_at ASC LIMIT 5"
        ))
        print("DIAG_SAMPLE_ROWS:", r4.fetchall())


asyncio.run(main())
