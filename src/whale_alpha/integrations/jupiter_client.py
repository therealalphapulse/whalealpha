"""Jupiter aggregator quote/swap client — port of src/integrations/jupiter/jupiterClient.ts.

Thin, correctly-shaped client for Jupiter's public quote/swap API. This
performs a real HTTP call against the endpoint in JUPITER_API_BASE — no
fabricated responses — but you must supply your own transaction-signing flow
(see engines/trade_executor.py) since that requires the user's actual signer,
which this repo does not (and should not) hardcode.

TODO(integration): signing. In production, never let the server hold a
plaintext key in memory longer than the signing operation, and strongly
prefer: (a) a non-custodial flow where the user signs client-side via a wallet
adapter, or (b) if custodial signing is unavoidable, sign inside a KMS/HSM
boundary. The reference (custodial, encrypted-key) signer in
engines/trade_executor.py is a starting point requiring a security review,
not a production-ready signer — carried over verbatim from the TS original.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from whale_alpha.config import Env
from whale_alpha.utils.logger import child_logger

log = child_logger("jupiter")


@dataclass
class QuoteParams:
    input_mint: str
    output_mint: str
    amount: int  # in the smallest unit of input_mint
    slippage_bps: int


@dataclass
class QuoteResponse:
    in_amount: str
    out_amount: str
    price_impact_pct: str
    route_plan: list[Any]
    raw: dict[str, Any]


@dataclass
class SwapResponse:
    swap_transaction: str  # base64-encoded unsigned transaction
    # Height after which this transaction's blockhash lease expires and it can
    # no longer land on-chain. Required by engines/reconciliation.py to decide
    # whether a SUBMITTED trade that isn't found on-chain yet has definitely
    # failed, or might still land — see `_blockhash_definitely_expired`.
    last_valid_block_height: int | None


class JupiterError(Exception):
    pass


async def get_quote(client: httpx.AsyncClient, env: Env, params: QuoteParams) -> QuoteResponse:
    url = f"{env.JUPITER_API_BASE}/quote"
    query = {
        "inputMint": params.input_mint,
        "outputMint": params.output_mint,
        "amount": str(params.amount),
        "slippageBps": str(params.slippage_bps),
    }
    res = await client.get(url, params=query)
    if res.status_code >= 400:
        body = res.text
        log.error("Jupiter quote request failed", status=res.status_code, body=body)
        raise JupiterError(f"Jupiter quote failed: {res.status_code}")
    data = res.json()
    return QuoteResponse(
        in_amount=data["inAmount"],
        out_amount=data["outAmount"],
        price_impact_pct=data["priceImpactPct"],
        route_plan=data.get("routePlan", []),
        raw=data,
    )


async def get_swap_transaction(
    client: httpx.AsyncClient, env: Env, quote: QuoteResponse, user_public_key: str
) -> SwapResponse:
    """Returns the base64-encoded unsigned transaction plus its blockhash's
    last valid block height, for the caller to sign, submit, and later
    reconcile if the process crashes before confirmation."""
    url = f"{env.JUPITER_API_BASE}/swap"
    payload = {
        "quoteResponse": quote.raw,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
    }
    res = await client.post(url, json=payload)
    if res.status_code >= 400:
        body = res.text
        log.error("Jupiter swap-transaction request failed", status=res.status_code, body=body)
        raise JupiterError(f"Jupiter swap transaction failed: {res.status_code}")
    data = res.json()
    return SwapResponse(
        swap_transaction=data["swapTransaction"],
        last_valid_block_height=data.get("lastValidBlockHeight"),
    )
