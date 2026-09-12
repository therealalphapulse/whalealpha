"""
tests/test_sellability_check.py

Standalone tests for domain.intelligence.sellability_check -- the
Production Risk Layer's live buy->sell Jupiter simulation gate.

Run directly: python tests/test_sellability_check.py

Follows the same manual-asyncio-runner + monkeypatch-the-imported-name
convention already used by tests/test_rugcheck_fallback.py in this repo
(no pytest-asyncio dependency).
"""

import asyncio
import sys

import domain.intelligence.sellability_check as sc

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name}")


async def run() -> bool:
    # 1. Normal sellable token: small (~3%) round-trip loss -> must pass.
    async def q_normal(inp, outp, amount, slippage_bps=150):
        if outp != sc.WRAPPED_SOL_MINT if hasattr(sc, "WRAPPED_SOL_MINT") else True:
            pass
        # BUY leg (SOL->token) vs SELL leg (token->SOL): identify by which
        # side is the wrapped-SOL mint.
        return {"outAmount": "1000000", "priceImpactPct": "0.01"} if outp == "TOKEN_A" \
            else {"outAmount": "48500000", "priceImpactPct": "0.01"}
    sc.get_quote = q_normal
    r = await sc.verify_sellability("TOKEN_A", chain="solana")
    check("normal sellable token -> not reject", r["reject"] is False and r["checked"] is True)
    check("normal sellable token -> round_trip_loss ~3%",
          abs(r["details"]["round_trip_loss_pct"] - 3.0) < 0.5)

    # 2. No sell route -> the core honeypot signal.
    async def q_no_sell(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "1000000"} if outp == "TOKEN_B" else {"outAmount": "0"}
    sc.get_quote = q_no_sell
    r = await sc.verify_sellability("TOKEN_B", chain="solana")
    check("no sell route -> reject", r["reject"] is True)
    check("no sell route -> reason mentions honeypot",
          any("honeypot" in x.lower() for x in r["reasons"]))

    # 3. No buy route -> no real entry liquidity.
    async def q_no_buy(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "0"}
    sc.get_quote = q_no_buy
    r = await sc.verify_sellability("TOKEN_C", chain="solana")
    check("no buy route -> reject", r["reject"] is True)
    check("no buy route -> reason mentions buy route",
          any("buy route" in x.lower() for x in r["reasons"]))

    # 4. Excessive round-trip loss (hidden 50% sell tax).
    async def q_tax(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "1000000"} if outp == "TOKEN_D" else {"outAmount": "25000000"}
    sc.get_quote = q_tax
    r = await sc.verify_sellability("TOKEN_D", chain="solana")
    check("50%% hidden-tax token -> reject", r["reject"] is True)
    check("50%% hidden-tax token -> reason mentions sell tax",
          any("tax" in x.lower() for x in r["reasons"]))

    # 5. Jupiter unreachable -> fail closed (never a silent pass-through).
    async def q_error(inp, outp, amount, slippage_bps=150):
        raise sc.SwapError("jupiter unreachable")
    sc.get_quote = q_error
    r = await sc.verify_sellability("TOKEN_E", chain="solana")
    check("jupiter unreachable -> fail-closed reject",
          r["reject"] is True and r["checked"] is False)

    # 6. Kill switch: SELLABILITY_CHECK_ENABLED=False must be fully non-blocking.
    sc.SELLABILITY_CHECK_ENABLED = False
    r = await sc.verify_sellability("TOKEN_F", chain="solana")
    check("disabled via config -> non-blocking", r["reject"] is False and r["checked"] is False)
    sc.SELLABILITY_CHECK_ENABLED = True

    # 7. Non-Solana chain -> non-blocking, and never makes a network call.
    called = {"n": 0}
    async def q_should_not_be_called(*a, **kw):
        called["n"] += 1
        return {"outAmount": "1"}
    sc.get_quote = q_should_not_be_called
    r = await sc.verify_sellability("TOKEN_G", chain="robinhood")
    check("non-Solana chain -> non-blocking", r["reject"] is False and r["checked"] is False)
    check("non-Solana chain -> no Jupiter call made", called["n"] == 0)

    # 8. Caching: a second check for the same contract must not re-simulate.
    call_count = {"n": 0}
    async def q_counting(inp, outp, amount, slippage_bps=150):
        call_count["n"] += 1
        return {"outAmount": "1000000"} if outp == "TOKEN_H" else {"outAmount": "970000"}
    sc.get_quote = q_counting
    r1 = await sc.verify_sellability("TOKEN_H", chain="solana")
    calls_after_first = call_count["n"]
    r2 = await sc.verify_sellability("TOKEN_H", chain="solana")
    check("cache avoids a second Jupiter round-trip", call_count["n"] == calls_after_first)
    check("cached result is identical", r1 == r2)

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
