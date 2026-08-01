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

- **Telegram bot**: aiogram v3 (long-polling for Telegram updates)
- **Inbound webhook**: aiohttp — a small HTTP server runs alongside the bot
  to receive whale wallet activity from an indexer (Helius enhanced webhooks
  by default; see "Feature status" below)
- **Solana**: solders + solana-py
- **Database**: PostgreSQL via SQLAlchemy 2.0 (async) + Alembic migrations
- **Queue/rate limiting/FSM storage**: redis-py (async) — also backs the
  `/connectwallet` conversation state so it survives a restart
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
- Telegram updates use long-polling (no inbound port needed for the bot
  itself), **but** the whale-wallet ingestion webhook (`WEBHOOK_PORT`,
  default `8080`) does need an exposed port — set that up in Railway's
  service networking settings and point your indexer's webhook config at
  the resulting public URL + `WEBHOOK_PATH`.
- Run `alembic upgrade head` as a Railway pre-deploy/release command (or a
  one-off shell) before the first deploy.

## Feature status

Everything below has a real, wired-up code path — not a stub — as of this
port. See each module's docstring for exact assumptions/judgment calls made
along the way (flagged the same way the original TS TODOs were).

| Feature | Where | Notes |
|---|---|---|
| Whale wallet tracking | `integrations/helius_webhook.py` | Inbound aiohttp server parses Helius enhanced-webhook payloads and calls `engines/monitor.ingest_wallet_buy_event`. Verify the payload parser against a real Helius delivery before depending on it. |
| Signal firing | `engines/scheduler.py`, `engines/signal.py` | Unchanged confidence-scoring algorithm; now also fills in `entry_zone_low/high` from a live price and actually notifies + auto-trades (below) instead of just writing a DB row. |
| Signal → notification | `services/notification.py` | DMs every user with `notify_signals=True` (default on; toggle with `/mute` / `/unmute`). |
| Auto buy/sell | `engines/auto_trading.py`, `engines/trade_executor.py` | USD→lamports sizing now uses a live SOL/USD price (`integrations/price_feed.py`) instead of the old `/ 1` placeholder. `build_eligible_users` assembles each user's risk state from the DB — see its docstring for the documented approximations (no per-position cost-basis table exists yet). |
| Manual buy/sell | `bot/commands/manual_trading.py` (`/buy`, `/sell`) | Goes through the same `trade_executor.py` choke point as auto-trades, tagged `TradeSource.MANUAL`. |
| Wallet connection | `bot/commands/wallet.py` (`/connectwallet`, `/disconnectwallet`) | FSM-based key entry over Redis-backed storage. Read the security note in that module's docstring before using this with real funds — pasting a key into Telegram is inherently risky. |
| % price-increase alerts | `engines/price_alerts.py`, `bot/commands/alerts.py` (`/alert`, `/alerts`, `/removealert`) | New `PriceAlert` table (migration `0002_...`), polling loop, per-alert cooldown. |

## What else is in this repo

- Full module structure, typed interfaces, and real algorithms for wallet
  scoring, signal aggregation, and risk-rule evaluation (see `src/whale_alpha/engines/*`) —
  ported 1:1 and cross-validated numerically against the original TS logic
  (see `PORTING_NOTES.md`).
- A working Telegram bot (aiogram) with real command wiring and an
  admin-only wallet management flow enforcing RBAC.
- SQLAlchemy models + Alembic migrations mirroring the original Prisma
  schema (plus the new `PriceAlert` table / `notify_signals` column), plus
  repository-style services for the whale database.
- Encryption utilities (AES-256-GCM) for at-rest secret handling, rate
  limiting, structured logging, Docker/CI setup.
- Restart-safe trade reconciliation (`engines/reconciliation.py`) — not
  present in the original, added per explicit porting requirement.
- A price feed integration (`integrations/price_feed.py`) defaulting to
  Jupiter's public Price API, with an override for a paid/self-hosted feed.
- No real historical wallet data is bundled. `scripts/seed.py` creates a
  handful of clearly-fake example wallets so you can see the schema and run
  the bot end-to-end locally before pointing it at real data.

Treat this as a strong starting point for a serious build, not a finished,
audited trading system. Before risking real capital: get a security review
(especially of the custodial key-handling in `/connectwallet` and
`utils/security/encryption.py`), add integration tests in `tests/integration`
against a devnet wallet, load-test the webhook endpoint, and read
`docs/ARCHITECTURE.md` and `docs/SECURITY.md`.
