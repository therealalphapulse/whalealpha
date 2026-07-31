"""Integration tests intentionally require real infra and are NOT run in CI by
default (see pyproject.toml `[tool.pytest.ini_options] testpaths = ["tests"]`
— note only tests/unit is exercised by the CI workflow's `pytest -v` because
tests/integration tests are marked `skip` below). To run these:

  1. Point SOLANA_RPC_URL at devnet (https://api.devnet.solana.com) — never
     mainnet for automated test runs.
  2. Fund a throwaway devnet keypair via `solana airdrop`.
  3. Set DATABASE_URL to a disposable test Postgres instance.
  4. Remove the `pytest.mark.skip` decorators below and run:
     pytest tests/integration -v

Suggested coverage once wired up:
  - engines.monitor.ingest_wallet_buy_event persists a WalletEvent and updates
    last_active_at
  - engines.scheduler evaluates all tokens and creates exactly one Signal per
    qualifying cluster (no duplicates within the same window)
  - engines.trade_executor.execute_trade against a real devnet swap (small
    amount, devnet SOL)
  - WhaleWalletAdminService.add_wallet rejects a non-admin actor (ForbiddenError)
  - engines.reconciliation.reconcile_pending_trades against a real devnet
    transaction left SUBMITTED by a simulated crash (NEW — not in the
    original TS test scaffold, added because reconciliation is new
    functionality per porting requirement #3)
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="needs SOLANA_RPC_URL=devnet + a disposable test database — see module docstring")
def test_end_to_end_signal_to_auto_trade_flow() -> None:
    # Intentionally left as a scaffold — see file header.
    pass
