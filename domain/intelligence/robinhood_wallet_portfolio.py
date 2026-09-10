"""Robinhood Chain wallet balances and Stock Token metadata."""
from __future__ import annotations
import asyncio, aiohttp
from domain.trading.real.robinhood_swap import get_native_balance,get_token_balance,get_mint_decimals
from config.settings import ROBINHOOD_EVM_CHAIN_ID
ASSETS_URL="https://api.robinhood.com/rhj/assets"
async def _assets()->list[dict]:
    async with aiohttp.ClientSession() as s:
        async with s.get(ASSETS_URL,timeout=10) as r:
            if r.status!=200: raise RuntimeError(f"Robinhood assets API HTTP {r.status}")
            return (await r.json()).get("assets",[])
async def fetch_wallet_fungible_tokens(address:str)->list[dict]|None:
    try:
        native=await get_native_balance(address)
        out=[{"mint":"0x0000000000000000000000000000000000000000","symbol":"ETH","name":"Ether","amount":native,"decimals":18,"chain":"robinhood"}] if native>0 else []
        assets=await _assets()
        candidates=[]
        for a in assets:
            if a.get("status")!="ASSET_STATUS_ACTIVE":continue
            dep=next((d for d in a.get("deployments",[]) if int(d.get("chainId",0))==ROBINHOOD_EVM_CHAIN_ID),None)
            if dep:candidates.append((a,dep["contractAddress"]))
        sem=asyncio.Semaphore(20)
        async def one(a,addr):
            async with sem:
                try:
                    bal=await get_token_balance(address,addr)
                    amount=bal["raw_amount"]/(10**bal["decimals"])
                    if amount<=0:return None
                    return {"mint":addr,"symbol":a.get("tokenSymbol") or addr[:8],"name":a.get("tokenName") or addr,"amount":amount,"decimals":18,"chain":"robinhood","logo_url":a.get("logoUrl"),"trading_capabilities":a.get("tradingCapabilities")}
                except Exception:return None
        rows=await asyncio.gather(*(one(a,addr) for a,addr in candidates))
        out.extend(x for x in rows if x)
        return out
    except Exception:return None
async def get_wallet_portfolio_value(address:str)->dict|None:
    tokens=await fetch_wallet_fungible_tokens(address)
    if tokens is None:return None
    # Exact USD marking is intentionally omitted here unless a trusted price source is available.
    return {"total_value_usd":None,"tokens":tokens}
def format_usd(value):
    if value is None:return "—"
    return f"${float(value):,.2f}"
async def build_wallet_portfolio_report(address:str)->str:
    tokens=await fetch_wallet_fungible_tokens(address)
    if tokens is None:return "⚠️ Robinhood Chain portfolio data is temporarily unavailable."
    lines=["💼 <b>Robinhood Chain Portfolio</b>","━━━━━━━━━━━━━━━━━━━━━"]
    for t in tokens[:50]:lines.append(f"• <b>{t['symbol']}</b>: {t['amount']:,.6f}")
    if not tokens:lines.append("No assets found in this wallet.")
    return "\n".join(lines)
