"""
tests/test_sellability_check.py

Standalone tests for domain.intelligence.sellability_check -- the
Production Risk Layer's live buy->sell simulation gate, covering both
supported chains:

  * Solana         (via Jupiter's public quote endpoint)
  * Robinhood Chain (via Uniswap's Trading API quote endpoint)

Run directly: python tests/test_sellability_check.py

Follows the same manual-asyncio-runner + monkeypatch-the-imported-name
convention already used by tests/test_rugcheck_fallback.py in this repo
(no pytest-asyncio dependency). The module under test imports its quote
functions as `_jupiter_get_quote` / `_robinhood_get_quote`, so that is
what gets monkeypatched here, not the source modules themselves.
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
    # ================= Solana (Jupiter) =================

    async def q_normal(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "1000000", "priceImpactPct": "0.01"} if outp == "TOKEN_A" \
            else {"outAmount": "48500000", "priceImpactPct": "0.01"}
    sc._jupiter_get_quote = q_normal
    r = await sc.verify_sellability("TOKEN_A", chain="solana")
    check("[SOL] normal sellable token -> not reject", r["reject"] is False and r["checked"] is True)
    check("[SOL] round-trip loss ~3%", abs(r["details"]["round_trip_loss_pct"] - 3.0) < 0.5)

    async def q_no_sell(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "1000000"} if outp == "TOKEN_B" else {"outAmount": "0"}
    sc._jupiter_get_quote = q_no_sell
    r = await sc.verify_sellability("TOKEN_B", chain="solana")
    check("[SOL] no sell route -> reject", r["reject"] is True)
    check("[SOL] no sell route -> reason mentions honeypot",
          any("honeypot" in x.lower() for x in r["reasons"]))

    async def q_no_buy(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "0"}
    sc._jupiter_get_quote = q_no_buy
    r = await sc.verify_sellability("TOKEN_C", chain="solana")
    check("[SOL] no buy route -> reject", r["reject"] is True)

    async def q_tax(inp, outp, amount, slippage_bps=150):
        return {"outAmount": "1000000"} if outp == "TOKEN_D" else {"outAmount": "25000000"}
    sc._jupiter_get_quote = q_tax
    r = await sc.verify_sellability("TOKEN_D", chain="solana")
    check("[SOL] 50% hidden-tax token -> reject", r["reject"] is True)
    check("[SOL] 50% hidden-tax token -> reason mentions sell tax",
          any("tax" in x.lower() for x in r["reasons"]))

    async def q_error(inp, outp, amount, slippage_bps=150):
        raise sc.JupiterSwapError("jupiter unreachable")
    sc._jupiter_get_quote = q_error
    r = await sc.verify_sellability("TOKEN_E", chain="solana")
    check("[SOL] jupiter unreachable -> fail-closed reject",
          r["reject"] is True and r["checked"] is False)

    sc.SELLABILITY_CHECK_ENABLED = False
    r = await sc.verify_sellability("TOKEN_F", chain="solana")
    check("[SOL] disabled via config -> non-blocking", r["reject"] is False and r["checked"] is False)
    sc.SELLABILITY_CHECK_ENABLED = True

    called = {"n": 0}
    async def q_should_not_be_called(*a, **kw):
        called["n"] += 1
        return {"outAmount": "1"}
    sc._jupiter_get_quote = q_should_not_be_called
    r = await sc.verify_sellability("TOKEN_G", chain="some-other-chain")
    check("[SOL] unsupported chain -> non-blocking", r["reject"] is False and r["checked"] is False)
    check("[SOL] unsupported chain -> no network call made", called["n"] == 0)

    call_count = {"n": 0}
    async def q_counting(inp, outp, amount, slippage_bps=150):
        call_count["n"] += 1
        return {"outAmount": "1000000"} if outp == "TOKEN_H" else {"outAmount": "970000"}
    sc._jupiter_get_quote = q_counting
    r1 = await sc.verify_sellability("TOKEN_H", chain="solana")
    calls_after_first = call_count["n"]
    r2 = await sc.verify_sellability("TOKEN_H", chain="solana")
    check("[SOL] cache avoids a second Jupiter round-trip", call_count["n"] == calls_after_first)
    check("[SOL] cached result is identical", r1 == r2)

    # ================= Robinhood Chain (Uniswap Trading API) =================

    probe_wei = int(sc.SELLABILITY_PROBE_ETH_AMOUNT * sc.WEI_PER_ETH)
    sell_wei_97pct = int(probe_wei * 0.97)
    sell_wei_50pct = int(probe_wei * 0.50)

    seen_swapper = {}
    async def q_evm_normal(inp, outp, amount, slippage_bps=150, swapper=None):
        seen_swapper["addr"] = swapper
        return {"outAmount": "500000000000000000"} if outp == "0xTOKENA" else {"outAmount": str(sell_wei_97pct)}
    sc._robinhood_get_quote = q_evm_normal
    r = await sc.verify_sellability("0xTOKENA", chain="robinhood")
    check("[EVM] normal sellable token -> not reject", r["reject"] is False and r["checked"] is True)
    check("[EVM] round-trip loss ~3%", abs(r["details"]["round_trip_loss_pct"] - 3.0) < 0.5)
    check("[EVM] quote uses the configured unfunded probe address, never a real wallet",
          seen_swapper["addr"] == sc.ROBINHOOD_SELLABILITY_PROBE_ADDRESS)

    async def q_evm_no_sell(inp, outp, amount, slippage_bps=150, swapper=None):
        return {"outAmount": "500000000000000000"} if outp == "0xTOKENB" else {"outAmount": "0"}
    sc._robinhood_get_quote = q_evm_no_sell
    r = await sc.verify_sellability("0xTOKENB", chain="robinhood")
    check("[EVM] no sell route -> reject", r["reject"] is True)
    check("[EVM] no sell route -> reason mentions honeypot",
          any("honeypot" in x.lower() for x in r["reasons"]))

    async def q_evm_no_buy(inp, outp, amount, slippage_bps=150, swapper=None):
        return {"outAmount": "0"}
    sc._robinhood_get_quote = q_evm_no_buy
    r = await sc.verify_sellability("0xTOKENC", chain="robinhood")
    check("[EVM] no buy route -> reject", r["reject"] is True)

    async def q_evm_tax(inp, outp, amount, slippage_bps=150, swapper=None):
        return {"outAmount": "500000000000000000"} if outp == "0xTOKEND" else {"outAmount": str(sell_wei_50pct)}
    sc._robinhood_get_quote = q_evm_tax
    r = await sc.verify_sellability("0xTOKEND", chain="robinhood")
    check("[EVM] 50% hidden-tax token -> reject", r["reject"] is True)
    check("[EVM] 50% hidden-tax token -> reason mentions sell tax",
          any("tax" in x.lower() for x in r["reasons"]))

    async def q_evm_error(inp, outp, amount, slippage_bps=150, swapper=None):
        raise sc.RobinhoodSwapError("RPC/Uniswap API unreachable")
    sc._robinhood_get_quote = q_evm_error
    r = await sc.verify_sellability("0xTOKENE", chain="robinhood")
    check("[EVM] RPC/Uniswap API unreachable -> fail-closed reject",
          r["reject"] is True and r["checked"] is False)

    evm_call_count = {"n": 0}
    async def q_evm_counting(inp, outp, amount, slippage_bps=150, swapper=None):
        evm_call_count["n"] += 1
        return {"outAmount": "500000000000000000"} if outp == "0xTOKENH" else {"outAmount": str(sell_wei_97pct)}
    sc._robinhood_get_quote = q_evm_counting
    r1 = await sc.verify_sellability("0xTOKENH", chain="robinhood")
    n1 = evm_call_count["n"]
    r2 = await sc.verify_sellability("0xTOKENH", chain="robinhood")
    check("[EVM] cache avoids a second Uniswap round-trip", evm_call_count["n"] == n1)
    check("[EVM] cached result is identical", r1 == r2)

    # ---- Regression cases from production (2026-09-13): Uniswap's real
    # error payloads, verbatim, caused a class of legitimate tokens to be
    # needlessly fail-closed rejected with zero retry. ----

    UPSTREAM_TIMEOUT = ("Uniswap API 404: {\'errorCode\': \'UpstreamTimeoutError\', "
                        "\'detail\': \'A routing dependency timed out or failed; the request "
                        "may succeed on retry.\', \'requestId\': \'abc\'}")
    NO_ROUTE = ("Uniswap API 404: {\'errorCode\': \'NoRouteFoundError\', "
                "\'detail\': \'No route with sufficient liquidity was found for this "
                "pair.\', \'requestId\': \'def\'}")

    attempts = {"n": 0}
    async def q_flaky_then_ok(inp, outp, amount, slippage_bps=150, swapper=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sc.RobinhoodSwapError(UPSTREAM_TIMEOUT)
        return {"outAmount": "500000000000000000"} if outp == "0xFLAKY" else {"outAmount": str(sell_wei_97pct)}
    sc._robinhood_get_quote = q_flaky_then_ok
    r = await sc.verify_sellability("0xFLAKY", chain="robinhood")
    check("[EVM] transient UpstreamTimeoutError resolves on retry -> not reject",
          r["reject"] is False and r["checked"] is True)
    check("[EVM] retry actually happened", attempts["n"] >= 2)

    attempts2 = {"n": 0}
    async def q_always_timeout(inp, outp, amount, slippage_bps=150, swapper=None):
        attempts2["n"] += 1
        raise sc.RobinhoodSwapError(UPSTREAM_TIMEOUT)
    sc._robinhood_get_quote = q_always_timeout
    r = await sc.verify_sellability("0xALWAYSTIMEOUT", chain="robinhood")
    check("[EVM] persistent UpstreamTimeoutError -> still fail-closed after retries",
          r["reject"] is True and r["checked"] is False)
    check("[EVM] persistent UpstreamTimeoutError -> retried exactly SELLABILITY_EVM_MAX_RETRIES+1 times",
          attempts2["n"] == sc.SELLABILITY_EVM_MAX_RETRIES + 1)

    attempts3 = {"n": 0}
    async def q_no_route(inp, outp, amount, slippage_bps=150, swapper=None):
        attempts3["n"] += 1
        raise sc.RobinhoodSwapError(NO_ROUTE)
    sc._robinhood_get_quote = q_no_route
    r = await sc.verify_sellability("0xNOROUTE", chain="robinhood")
    check("[EVM] NoRouteFoundError -> decisive reject (checked=True, not unverified)",
          r["reject"] is True and r["checked"] is True)
    check("[EVM] NoRouteFoundError -> not retried (it is decisive, not transient)",
          attempts3["n"] == 1)

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
