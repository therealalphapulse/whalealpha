# Whale Alpha (Python port)

Solana smart-wallet intelligence platform: an admin-curated database of
high-performing ("elite") Solana wallets, a scoring/discovery engine, a
signal engine that fires when multiple trusted wallets accumulate the same
token, and manual + rule-based auto trading via a Telegram bot.

This is a faithful Python port of the original TypeScript/grammY project —
see `PORTING_NOTES.md` for everything that changed, was added, or needed a
judgment call during the port.

**This is not a copy-trading bot.** Auto Trading only executes when Whale
Alpha's own signal engine emits a qualified signal that clears the user's
configured risk rules — never simply because a tracked wallet bought
something.

## Stack

- **Telegram bot**: aiogram v3 (long-polling, no inbound HTTP port needed)
- **Solana**: solders + solana-py
- **Database**: PostgreSQL via SQLAlchemy 2.0 (async) + Alembic migrations
- **Queue/rate limiting**: redis-py (async)
- **Config**: pydantic v2 + pydantic-settings
- **Encryption**: `cryptography` (AES-256-GCM), wire-compatible with the
  original TS `iv:authTag:ciphertext` format
- **Logging**: structlog (structured JSON in production, pretty console in dev)
- **Testing**: pytest + pytest-asyncio
- **Linting/typing**: ruff + mypy (strict)

## Quick start

```bash
cp .env.example .env        # fill in your own RPC / bot token / DB url
pip install -e ".[dev]"
alembic upgrade head
python -m scripts.seed      # optional: fake example wallets for local testing
python -m whale_alpha.main
```

Or with Docker Compose (app + Postgres + Redis):

```bash
cp .env.example .env
docker compose up --build
```

## Deploying on Railway

- Single `Dockerfile`, multi-stage build, runs as a non-root user.
- Reads `DATABASE_URL` / `REDIS_URL` from env — point these at Railway's
  managed Postgres/Redis via `${{Postgres.DATABASE_URL}}` /
  `${{Redis.REDIS_URL}}` service references in your Railway service config.
- No inbound HTTP port required (long-polling, not webhooks) — don't expose
  a port in the Railway service settings.
- Run `alembic upgrade head` as a Railway pre-deploy/release command (or a
  one-off shell) before the first deploy.

## What's actually in this repo

- Full module structure, typed interfaces, and real algorithms for wallet
  scoring, signal aggregation, and risk-rule evaluation (see `src/whale_alpha/engines/*`) —
  ported 1:1 and cross-validated numerically against the original TS logic
  (see `PORTING_NOTES.md`).
- A working Telegram bot (aiogram) with real command wiring and an
  admin-only wallet management flow enforcing RBAC.
- SQLAlchemy models + Alembic migration mirroring the original Prisma
  schema exactly, plus repository-style services for the whale database.
- Encryption utilities (AES-256-GCM) for at-rest secret handling, rate
  limiting, structured logging, Docker/CI setup.
- **New**: restart-safe trade reconciliation (`engines/reconciliation.py`) —
  not present in the original, added per explicit porting requirement.
- Integration points that need your own credentials/infra, clearly marked
  `TODO(integration)`: your Solana RPC endpoint (Helius/Triton/QuickNode),
  the Jupiter swap API (already wired to the real public endpoint), a
  price/liquidity feed (Birdeye/DexScreener), and whatever whale-discovery
  data source you license.
- No real historical wallet data is bundled. `scripts/seed.py` creates a
  handful of clearly-fake example wallets so you can see the schema and run
  the bot end-to-end locally before pointing it at real data.

Treat this as a strong starting point for a serious build, not a finished
trading system. Before risking real capital, get a security review, add the
integration tests in `tests/integration` against a devnet wallet, and read
`docs/ARCHITECTURE.md` and `docs/SECURITY.md`.
