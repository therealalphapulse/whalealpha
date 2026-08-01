# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [0.2.0] - Unreleased
### Added
- **Whale wallet tracking**: inbound Helius-webhook receiver (`integrations/helius_webhook.py`,
  aiohttp) wired to `engines/monitor.ingest_wallet_buy_event` — previously nothing called
  that function at all.
- **Signal → notification**: `services/notification.py` DMs subscribed users when a Signal
  is generated; `User.notify_signals` + `/mute` / `/unmute` (migration `0002_...`).
- **Signal → auto-trading**: scheduler now calls `engines.auto_trading.process_signal_for_auto_trading`
  for eligible users (`build_eligible_users`, new). Fixed the hardcoded `/ 1` USD→lamports
  placeholder using a real price feed.
- **Price feed**: `integrations/price_feed.py`, defaults to Jupiter's public Price API with
  in-process caching; used for trade sizing, signal entry zones, and price alerts.
- **Manual trading**: `/buy` and `/sell` commands (`bot/commands/manual_trading.py`) — did
  not exist before; only auto-trading had a caller for `trade_executor.py`.
- **Wallet connection**: `/connectwallet` / `/disconnectwallet` (`bot/commands/wallet.py`),
  FSM-based key entry over Redis-backed FSM storage (`Dispatcher(storage=RedisStorage(...))`).
- **% price-increase alerts**: new `PriceAlert` model + migration, `engines/price_alerts.py`
  polling loop, `/alert` / `/alerts` / `/removealert` commands — this feature had zero code
  path (no polling loop, no threshold check, no alert message) before this change.
- Unit tests for all of the above pure-logic pieces (`tests/unit/test_helius_webhook.py`,
  `test_price_alerts.py`, `test_wallet_commands.py`, `test_price_feed.py`).

### Verified in this environment
- Full package imports cleanly end-to-end (`import whale_alpha.main`).
- `pytest tests/unit` — 34/34 passing (15 original + 19 new).
- `ruff check` clean on every new/modified file.
- Not verified: no live Postgres/Redis/Solana RPC/Telegram bot token available in this
  environment, so none of this has been exercised against real infrastructure yet. See
  README.md "Feature status" for per-feature caveats (esp. the Helius payload shape and
  the auto-trading portfolio-value approximation).

## [0.1.0] - Unreleased
### Added
- Python port of the original TypeScript/grammY Whale Alpha scaffold: bot (aiogram),
  engines (scoring/monitor/signal/risk/auto-trading/trade-executor), SQLAlchemy schema +
  Alembic migration mirroring the original Prisma schema, admin RBAC, encryption
  utilities, Docker + CI, tailored for Railway deployment.
- Wallet scoring algorithm and signal confidence aggregation — numerically
  cross-validated against the original TS logic during the port (see PORTING_NOTES.md).
- Auto-trading risk engine with per-user configurable limits — ported 1:1.
- **New**: restart-safe trade reconciliation (`engines/reconciliation.py`), added per
  explicit porting requirement; not present in the original TS codebase.

### Known limitations
- Discovery engine and price/liquidity feed are typed adapters awaiting a licensed data
  source; not yet wired to a live provider (carried over from the original).
- No production security audit has been performed.
- Full dependency install + `pytest`/`ruff`/`mypy` run could not be executed in the
  environment this port was produced in (no network access to install third-party
  packages); see PORTING_NOTES.md for what was verified instead and what you must run
  yourself before deploying.
