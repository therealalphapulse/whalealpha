#!/usr/bin/env python3
"""
Read-only smoke test for the new provider integrations (AlphaPulse Provider
Integration Task, 2026-08-19): Ankr (RPC), Moralis (holder fallback), Shyft
(holder fallback).

This performs exactly one safe, read-only network call per configured
provider and reports pass/fail. It does NOT modify any state, does NOT
touch Railway variables, and does NOT deploy anything -- it's meant to be
run manually (e.g. `railway run python scripts/audit_new_providers.py` or
locally with a .env file) before/after merging, to confirm each new
credential actually works before relying on it in production.

A provider whose API key isn't set is reported as SKIPPED, not FAILED --
this script never invents credentials and is safe to run in any
environment regardless of which of the three keys are configured.
"""

import asyncio
import os
import sys

# Well-known, permanently-populated mainnet mint (USDC) -- used only to
# confirm each provider's holder/RPC endpoint responds correctly. Never
# used for anything beyond this one-off read.
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def check_ankr() -> tuple[str, str]:
    api_key = os.getenv("ANKR_API_KEY")
    if not api_key:
        return "SKIP", "ANKR_API_KEY not set"
    import aiohttp

    url = f"https://rpc.ankr.com/solana/{api_key}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []}
    try:
        timeout = aiohttp.ClientTimeout(total=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                status = resp.status
                body = await resp.json(content_type=None)
    except Exception as exc:
        return "FAIL", f"{type(exc).__name__}: {exc}"

    if status != 200:
        return "FAIL", f"HTTP {status}: {body}"
    if isinstance(body, dict) and body.get("result") == "ok":
        return "PASS", "getHealth -> ok"
    return "FAIL", f"unexpected response: {body}"


async def check_moralis() -> tuple[str, str]:
    api_key = os.getenv("MORALIS_API_KEY")
    if not api_key:
        return "SKIP", "MORALIS_API_KEY not set"
    from domain.intelligence._moralis_holder_fallback import fetch_token_holders

    try:
        accounts = await fetch_token_holders(_USDC_MINT)
    except Exception as exc:
        return "FAIL", f"{type(exc).__name__}: {exc}"

    if accounts is None:
        return "FAIL", "fetch_token_holders returned None (unavailable/unconfigured)"
    return "PASS", f"top-holders returned {len(accounts)} accounts for USDC"


async def check_shyft() -> tuple[str, str]:
    api_key = os.getenv("SHYFT_API_KEY")
    if not api_key:
        return "SKIP", "SHYFT_API_KEY not set"
    from domain.intelligence._shyft_holder_fallback import fetch_token_holders

    try:
        accounts = await fetch_token_holders(_USDC_MINT)
    except Exception as exc:
        return "FAIL", f"{type(exc).__name__}: {exc}"

    if accounts is None:
        return "FAIL", "fetch_token_holders returned None (unavailable/unconfigured)"
    return "PASS", f"get_owners returned {len(accounts)} accounts for USDC"


async def main() -> int:
    checks = {
        "Ankr (RPC failover)": check_ankr,
        "Moralis (holder fallback)": check_moralis,
        "Shyft (holder fallback)": check_shyft,
    }

    results = {}
    for name, fn in checks.items():
        results[name] = await fn()

    print("\nNew provider integration audit (read-only)")
    print("=" * 60)
    failed = False
    for name, (status, detail) in results.items():
        print(f"[{status:4s}] {name}: {detail}")
        if status == "FAIL":
            failed = True
    print("=" * 60)

    if failed:
        print("One or more configured providers failed a live check.")
        return 1
    print("All configured providers passed. Unconfigured providers were skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
