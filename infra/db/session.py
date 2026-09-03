import os
import logging
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is missing. Provide a PostgreSQL connection string "
        "(any provider — Railway, RDS, Cloud SQL, self-hosted, or the "
        "`db` service in docker-compose.yml)."
    )

# Some providers (Railway among them) hand out postgresql:// but SQLAlchemy
# async needs postgresql+asyncpg://
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# v4 (Bible §7): explicit pool sizing instead of engine defaults — the
# audit found no pool_size/max_overflow configured anywhere, which was
# never validated against real concurrent load. Defaults below are sized
# for a single Bot Gateway + a couple of workers (the v4.0 baseline
# topology); tune upward per replica count as the deployment scales, or
# set DB_POOL_SIZE/DB_MAX_OVERFLOW directly. At the 10k-user tier the
# Bible calls for PgBouncer in front of Postgres instead of raising this
# further app-side.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

logger = logging.getLogger("AlphaPulse.DB")


class Base(DeclarativeBase):
    pass


async def init_db():
    """
    Dev/local-only convenience path: creates any tables that don't exist
    yet via SQLAlchemy metadata, exactly as v3 did.

    v4 (Bible §7): this is no longer how schema changes reach production.
    Alembic (infra/db/migrations/) is now the source of truth for schema
    evolution — see infra/db/migrations/versions/0001_baseline.py. This
    function remains only so a fresh local/dev/CI database (including this
    sandbox, which has no live Postgres to run `alembic upgrade` against)
    can still be bootstrapped in one call. It must never be relied on in
    a deployed environment that already has data.
    """
    import models  # noqa: F401

    logger.warning(
        "init_db() creates missing tables via metadata.create_all — this "
        "is a dev/local convenience only. Production schema changes go "
        "through Alembic (infra/db/migrations/), never through this "
        "function."
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("✅ Database initialized (dev/local mode)")


async def close_db():
    await engine.dispose()
    logger.info("🔌 Database connection closed")
