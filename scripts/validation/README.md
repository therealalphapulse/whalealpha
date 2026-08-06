# Dependency-free runtime validation scripts

`pytest`'s unit suite (`tests/unit/`) covers the pure decision-logic in this
repo (`evaluate_promotion`, `evaluate_retention`, `decide_history_fetch_outcome`,
etc.) and needs the project's real dependencies installed (`pip install -e .`)
to run at all, since importing `whale_alpha.config.Env` pulls in
`pydantic-settings`, `whale_alpha.engines.discovery` pulls in `sqlalchemy`,
and so on.

The scripts in this directory exist for a narrower purpose: they exercise the
**real, unmodified, shipped** `utils/http_retry.py` and
`integrations/wallet_discovery_source.py` modules end-to-end — genuine
`asyncio` execution, not a description of expected behavior — in an
environment where none of those third-party packages are installed (e.g. an
offline sandbox with no PyPI access). Each script fakes only the one or two
external imports it can't avoid (`httpx`, `structlog`, `solana.rpc.async_api`,
`whale_alpha.config.Env`) via lightweight stand-ins registered directly in
`sys.modules`, and lets every other import resolve to the real package on
disk. Every assertion runs against real production code paths — the retry/
backoff math, the circuit breaker state machine, the RPC swap-reconstruction
parser, the full PRIMARY → stale cache → RPC fallback → retry-queue chain in
`fetch_wallet_swap_history` — not a re-implementation or a mock of that logic.

**Run with plain `python3`, no installed dependencies required:**

```bash
python3 scripts/validation/validate_http_retry.py       # Task 3: retry/backoff/circuit-breaker/cache/metrics
python3 scripts/validation/validate_rpc_swap_parser.py   # Task 2: RPC-based swap reconstruction parser
python3 scripts/validation/validate_fallback_chain.py    # Task 2: full fallback-chain orchestration
```

These are a **supplement** to the real `pytest` suite in `tests/unit/`, not a
replacement for it — once real dependencies are installed (`pip install -e .`
+ a Postgres instance for anything DB-backed), the full test suite and a real
`run_discovery_cycle` against live providers are the actual bar for
production sign-off. See `docs/PHASE1_TASK2_TASK3_VALIDATION_REPORT.md` for
what was and wasn't possible to verify in the sandbox these were authored in.
