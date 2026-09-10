"""
domain/trading/auto_trade/

Standalone, Trojan-inspired Auto-Trade Engine. Sits downstream of an
already-qualified AlphaPulse signal (domain/signals/) and owns the
complete signal-driven trade lifecycle: authorization -> buy ->
on-chain confirmation -> position management -> TP/SL/trailing exits
-> sell -> reconciliation -> P&L.

Isolation contract (see AUTO_TRADE_ARCHITECTURE.docx, esp. §2, §37,
§45):
  * This package does NOT read from or write to models/real_trade.py,
    models/real_wallet.py's automation columns, models/real_autobuy_filter.py,
    models/auto_buy_claim.py, or any file under domain/trading/real/ or
    domain/trading/real's execution/exit engines. Those remain the
    existing, untouched AutoBuy/AutoTrade implementation.
  * This package DOES reuse shared, non-business-logic infra: the
    user's RealWallet (models/real_wallet.py) for keys/balance,
    domain/trading/real/jupiter_swap.py + jupiter_price.py for
    on-chain execution primitives, infra/kms/wallet_crypto.py for
    signing, infra/db/session.py, and infra/locks.py -- the same low-level
    building blocks the rest of the app already uses, per the spec's
    "reuse existing infrastructure, do not duplicate it" directive.
  * This package DOES NOT discover tokens, score tokens, or generate
    signals. It only reads already-qualified, already-alert-delivered
    SignalToken rows (read-only) via signal_adapter.py -- exactly the
    same qualification gate domain/trading/real/real_automation_engine.py
    uses (status == "active" and alert_delivered == True).
  * There is no copy-trading anywhere in this package. The only trade
    trigger is a qualifying AlphaPulse SignalToken.

Module map (mirrors the spec's §38 suggested structure, adapted to this
repo's flat domain/trading/<engine>/ convention):
  constants.py        - shared enums / rejection reasons
  signal_adapter.py    - SignalToken -> AutoTradeSignal normalization
  policy_service.py    - AutoTradePolicy CRUD, policy snapshot, daily counters
  risk_gate.py          - user-level + pre-trade execution risk checks
  claims.py              - idempotency claim helpers
  execution.py          - buy/sell swap primitives (built on jupiter_swap.py)
  orchestrator.py       - the Trade Orchestrator: signal -> policy -> risk -> buy -> position
  position_manager.py   - open-position queries, live price/PnL view, sellable-balance resolution
  exit_engine.py         - TP / SL / trailing-stop evaluation + sell trigger
  pnl.py                 - realized P&L computation from the execution ledger
  reconciliation.py      - DB-vs-chain reconciliation + crash recovery
  worker.py               - the two background loops (scan-and-buy, monitor-and-exit)
"""
