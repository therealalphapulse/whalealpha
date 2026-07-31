# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

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
