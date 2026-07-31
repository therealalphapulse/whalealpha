# Contributing to Whale Alpha (Python port)

## Development setup
1. `cp .env.example .env` and fill in local values (a devnet RPC and a Postgres/Redis
   instance are enough to develop against).
2. `pip install -e ".[dev]"`
3. `alembic upgrade head`
4. `python -m whale_alpha.main`

## Guidelines
- All engine logic (`src/whale_alpha/engines/**`) must be pure/testable where possible —
  no direct network calls inside scoring/risk math; inject adapters instead.
- Any change to the whale-wallet admin API must keep RBAC checks and audit logging intact.
- Never log private keys, decrypted secrets, or full RPC responses containing user funds
  data. Use `whale_alpha.utils.logger` redaction helpers.
- Add unit tests for new engine logic under `tests/unit`.
- Run `ruff check . && ruff format --check . && mypy src && pytest` before opening a PR.

## Commit style
Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
