"""Robinhood Chain EVM execution using standard JSON-RPC + Uniswap Trading API."""
from __future__ import annotations
import asyncio, logging, time
from typing import Any
import aiohttp
from eth_account import Account
from infra.kms.wallet_crypto import decrypt_secret
from config.settings import ROBINHOOD_EVM_CHAIN_ID, ROBINHOOD_RPC_URL, ROBINHOOD_RPC_FALLBACK_URLS, ROBINHOOD_UNISWAP_API_KEY
from infra.locks import try_acquire_lock, release_lock

logger=logging.getLogger("WhaleAlpha.RobinhoodSwap")
NATIVE_ETH_ADDRESS="0x0000000000000000000000000000000000000000"
SwapError=RuntimeError
API_URL="https://trade-api.gateway.uniswap.org/v1"
UNIVERSAL_ROUTER_VERSION="2.1.1"
ERC20_BALANCE_OF="70a08231"; ERC20_DECIMALS="313ce567"; ERC20_SYMBOL="95d89b41"
TRANSFER_TOPIC="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_RPC_URLS=[ROBINHOOD_RPC_URL,*ROBINHOOD_RPC_FALLBACK_URLS]

async def rpc_call(method:str, params:list[Any]|None=None)->Any:
    """Tries each configured RPC endpoint in order (ROBINHOOD_RPC_URL first,
    then config.settings.ROBINHOOD_RPC_FALLBACK_URLS), falling through to
    the next ONLY on a transport-level failure (timeout, connection error,
    HTTP 5xx) -- never on a valid JSON-RPC error response (data["error"]),
    since that's the chain's own answer (e.g. "insufficient funds",
    "execution reverted") and would be identical on every provider;
    retrying elsewhere would just waste the confirmation-window budget in
    _sign_send for no chance of a different outcome. Without this, every
    buy/sell/approval/balance-check depended on a single endpoint's uptime."""
    payload={"jsonrpc":"2.0","id":1,"method":method,"params":params or []}
    last_transport_error:Exception|None=None
    for i,url in enumerate(_RPC_URLS):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url,json=payload,timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status>=500:
                        raise aiohttp.ClientError(f"HTTP {resp.status}")
                    if resp.status!=200:
                        raise SwapError(f"Robinhood Chain RPC HTTP {resp.status}")
                    data=await resp.json(content_type=None)
                    if data.get("error") is not None: raise SwapError(str(data["error"]))
                    return data.get("result")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_transport_error=exc
            if i+1<len(_RPC_URLS):
                logger.warning("RPC endpoint %d/%d failed (%s), trying next: %s", i+1, len(_RPC_URLS), url, exc)
                continue
            raise SwapError(f"All {len(_RPC_URLS)} Robinhood Chain RPC endpoint(s) failed; last error: {exc}") from exc
    raise SwapError(f"All RPC endpoints failed: {last_transport_error}")

async def assert_chain()->None:
    cid=int(await rpc_call("eth_chainId"),16)
    if cid!=ROBINHOOD_EVM_CHAIN_ID: raise SwapError(f"Wrong chain: RPC returned {cid}, expected {ROBINHOOD_EVM_CHAIN_ID}.")

async def get_native_balance(address:str)->float:
    return int(await rpc_call("eth_getBalance",[address,"latest"]),16)/10**18

async def eth_usd_price()->float|None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.coingecko.com/api/v3/simple/price",params={"ids":"ethereum","vs_currencies":"usd"},timeout=5) as r:
                if r.status==200:return float((await r.json()).get("ethereum",{}).get("usd"))
    except Exception: pass
    return None

# Decimals are immutable for a deployed ERC-20 contract, so caching them
# in-process eliminates a repeat RPC round-trip on every subsequent wallet
# check or swap involving the same token. This is purely a latency
# optimization -- values returned are identical to an uncached call.
_decimals_cache:dict[str,int]={}

async def get_token_balance(address:str, token:str)->dict:
    data="0x"+ERC20_BALANCE_OF+address[2:].lower().rjust(64,"0")
    # Balance and decimals are independent reads; fetching them concurrently
    # instead of sequentially roughly halves this call's wall-clock latency.
    raw,decimals=await asyncio.gather(
        rpc_call("eth_call",[{"to":token,"data":data},"latest"]),
        get_mint_decimals(token),
    )
    raw_amount=int(raw,16)
    ui_amount=(raw_amount/(10**decimals)) if decimals else float(raw_amount)
    return {"raw_amount":raw_amount,"decimals":decimals,"ui_amount":ui_amount,"token_address":token,"token_accounts":[]}

async def get_mint_decimals(token:str)->int:
    key=token.lower()
    cached=_decimals_cache.get(key)
    if cached is not None: return cached
    raw=await rpc_call("eth_call",[{"to":token,"data":"0x"+ERC20_DECIMALS},"latest"])
    decimals=int(raw,16)
    _decimals_cache[key]=decimals
    return decimals

async def get_token_symbol(token:str)->str:
    try:
        raw=await rpc_call("eth_call",[{"to":token,"data":"0x"+ERC20_SYMBOL},"latest"])
        b=bytes.fromhex(raw[2:]);
        if len(b)>=64:
            off=int.from_bytes(b[:32],"big")
            if off+32<=len(b):
                n=int.from_bytes(b[off:off+32],"big"); return b[off+32:off+32+n].decode(errors="ignore")[:24]
        return bytes.fromhex(raw[2:]).rstrip(b"\x00").decode(errors="ignore")[:24]
    except Exception:return token[:8]

async def _api(method:str,path:str,body:dict|None=None)->dict:
    """Retries up to 2 extra times (3 attempts total) on a transport-level
    failure or HTTP 5xx from Uniswap's Trading API, with a short backoff --
    never on 4xx (a client-side problem, e.g. bad slippage/route params,
    that would just repeat identically). This is general reliability
    hygiene for a single-provider dependency with no alternative routing
    source wired up; it does not eliminate the dependency, just absorbs
    brief blips."""
    if not ROBINHOOD_UNISWAP_API_KEY:
        raise SwapError("UNISWAP_API_KEY is not configured; Robinhood Chain live trading is fail-closed until the production API key is set.")
    headers={"x-api-key":ROBINHOOD_UNISWAP_API_KEY,"accept":"application/json","content-type":"application/json","x-permit2-disabled":"true","x-universal-router-version":UNIVERSAL_ROUTER_VERSION}
    attempts=3
    last_exc:Exception|None=None
    for attempt in range(attempts):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.request(method,API_URL+path,headers=headers,json=body,timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status>=500:
                        raise aiohttp.ClientError(f"HTTP {r.status}")
                    data=await r.json(content_type=None)
                    if r.status>=400: raise SwapError(f"Uniswap API {r.status}: {data}")
                    return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc=exc
            if attempt+1<attempts:
                logger.warning("Uniswap API %s %s transient failure (attempt %d/%d), retrying: %s", method, path, attempt+1, attempts, exc)
                await asyncio.sleep(0.5*(attempt+1))
                continue
            raise SwapError(f"Uniswap API {method} {path} failed after {attempts} attempts: {exc}") from exc
    raise SwapError(f"Uniswap API {method} {path} failed: {last_exc}")

def _tx_from_api(tx:dict,from_address:str)->dict:
    if not tx.get("to") or not tx.get("data") or tx.get("data")=="0x": raise SwapError("Uniswap returned invalid transaction calldata.")
    out={"to":tx["to"],"from":from_address,"data":tx["data"],"value":int(tx.get("value","0")),"chainId":ROBINHOOD_EVM_CHAIN_ID,"nonce":int(tx.get("nonce",0))}
    if tx.get("gasLimit"): out["gas"]=int(tx["gasLimit"])
    if tx.get("gasPrice"): out["gasPrice"]=int(tx["gasPrice"])
    else:
        if tx.get("maxFeePerGas"): out["maxFeePerGas"]=int(tx["maxFeePerGas"])
        if tx.get("maxPriorityFeePerGas"): out["maxPriorityFeePerGas"]=int(tx["maxPriorityFeePerGas"])
    return out

async def _fee_fields(priority_fee_wei:int|str="auto") -> dict:
    """Builds EIP-1559 fee fields (maxFeePerGas/maxPriorityFeePerGas).

    IMPORTANT, confirmed directly against docs.robinhood.com/chain
    (Differences from Ethereum) on 2026-09-30: Robinhood Chain uses strict
    first-come-first-served sequencing. There are no Priority Gas
    Auctions, and the production endpoint currently reports a ZERO
    priority fee -- raising this value does NOT buy earlier inclusion
    here, unlike Ethereum L1 or a PGA-enabled Arbitrum Orbit chain. This
    function exists for correctness (honoring whatever priority_fee_tier
    a user actually selected, rather than silently ignoring it as before)
    and forward-compatibility (if Robinhood Chain ever enables PGA), NOT
    as a speed/reliability mechanism. The actual fix for a transaction
    that doesn't confirm is rpc_call's multi-provider failover above --
    on an FCFS chain with ~100ms blocks, a transaction still unconfirmed
    after more than a few seconds is almost certainly an RPC-level
    problem, not a fee one. Deliberately NOT implemented: bump-and-
    resubmit-at-higher-gas on a stuck transaction, since FCFS ordering
    means a higher fee cannot move a transaction earlier -- that
    complexity (and its replace-by-fee edge cases) would add real risk
    for no actual benefit on this chain.
    """
    try:
        block=await rpc_call("eth_getBlockByNumber",["pending",False])
        base_fee=int(block["baseFeePerGas"],16)
    except Exception:
        return {}  # chain/endpoint doesn't expose EIP-1559 fields -- caller falls back to legacy gasPrice
    if priority_fee_wei=="auto" or priority_fee_wei is None:
        try:
            priority_fee=int(await rpc_call("eth_maxPriorityFeePerGas"),16)
        except Exception:
            priority_fee=0
    else:
        # execution.py's PRIORITY_FEE_TIERS values ("fast"=2, "turbo"=5) are
        # small integers denominated in Gwei, not wei, despite this
        # parameter's name -- converting here, once, in the one place that
        # actually consumes the value.
        priority_fee=int(priority_fee_wei)*10**9
    max_fee=base_fee*2+priority_fee  # standard 2x-base-fee headroom for movement between build and inclusion
    return {"maxFeePerGas":max_fee,"maxPriorityFeePerGas":priority_fee}

async def _acquire_wallet_lock(address:str, wait_seconds:float=20.0, ttl_seconds:int=30) -> str:
    """Serializes nonce-fetch -> sign -> broadcast across EVERY caller that
    can sign for this wallet -- manual /buy /sell (real_trade_engine.py,
    running in the whalealpha gateway process), auto-trade buys/sells
    (execution.py, orchestrator.py, exit_engine.py, running in the
    trading-worker process), and DCA/limit orders -- all funnel through
    _sign_send, so locking here is the single choke point that covers
    all of them regardless of which process or loop initiated the call.
    A Redis-backed lock (infra.locks) is required for this to actually be
    cross-process; see that module's own fallback note for what happens
    if REDIS_URL is unset (degrades to a per-process lock only).

    Fails CLOSED: if the lock can't be acquired within wait_seconds
    (including if Redis itself is unavailable, which infra.locks also
    treats as fail-closed), this raises rather than proceeding
    unprotected -- for transaction-signing code, a bounded wait-then-error
    is a better outcome than a silent nonce race that could drop or
    corrupt a real transaction."""
    key=f"evm_wallet:{address.lower()}"
    deadline=time.monotonic()+wait_seconds
    while True:
        token=await try_acquire_lock(key, ttl_seconds=ttl_seconds)
        if token:
            return token
        if time.monotonic()>=deadline:
            raise SwapError(f"Timed out waiting for another transaction on wallet {address} to finish broadcasting.")
        await asyncio.sleep(0.3)

async def _sign_send(private_key:bytes, tx:dict, priority_fee_wei:int|str="auto")->tuple[str,dict|None,str]:
    """Broadcasts and waits for confirmation. Returns (tx_hash, receipt_or_none, status)
    where status is one of "confirmed" (receipt status==1), "failed" (receipt status==0,
    i.e. an on-chain revert -- a definite, known outcome, safe to treat as a clean
    failure rather than something requiring reconciliation), or "unknown" (broadcast
    succeeded but no receipt was observed within the confirmation window -- outcome
    is NOT known and callers must not blindly retry).

    The wallet lock (_acquire_wallet_lock) is held ONLY from just before the
    nonce fetch through the broadcast call -- NOT through the confirmation
    wait below. Once eth_sendRawTransaction succeeds, the next transaction
    for this wallet can safely claim nonce+1 immediately; holding the lock
    for the full ~60s confirmation window would serialize unrelated
    transactions for no reason and make "wallet busy" timeouts far more
    likely under any real trading volume."""
    acct=Account.from_key(private_key)
    tx=dict(tx); tx["from"]=acct.address; tx["chainId"]=ROBINHOOD_EVM_CHAIN_ID

    lock_key=f"evm_wallet:{acct.address.lower()}"
    lock_token=await _acquire_wallet_lock(acct.address)
    try:
        tx["nonce"]=int(await rpc_call("eth_getTransactionCount",[acct.address,"pending"]),16)
        if "gas" not in tx:
            tx["gas"]=int(await rpc_call("eth_estimateGas",[{k:v for k,v in tx.items() if k!="nonce"}]),16)
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            fee_fields=await _fee_fields(priority_fee_wei)
            if fee_fields:
                tx.update(fee_fields)
            else:
                tx["gasPrice"]=int(await rpc_call("eth_gasPrice"),16)
        signed=acct.sign_transaction(tx)
        raw="0x"+signed.raw_transaction.hex()
        h=await rpc_call("eth_sendRawTransaction",[raw])
    finally:
        await release_lock(lock_key, lock_token)

    deadline=time.monotonic()+60
    receipt=None
    while time.monotonic()<deadline:
        try:
            receipt=await rpc_call("eth_getTransactionReceipt",[h])
        except SwapError as exc:
            logger.warning("Receipt poll error for %s: %s", h, exc)
            receipt=None
        if receipt: break
        await asyncio.sleep(1)
    if not receipt:
        logger.warning("Transaction %s broadcast but confirmation not observed within 60s; status unknown.", h)
        return h, None, "unknown"
    if int(receipt.get("status","0x0"),16)!=1:
        return h, receipt, "failed"
    return h, receipt, "confirmed"

async def _approval_if_needed(private_key:bytes,wallet_address:str,token:str,amount_raw:int,token_out:str)->None:
    if token.lower()==NATIVE_ETH_ADDRESS.lower(): return
    data=await _api("POST","/check_approval",{"walletAddress":wallet_address,"token":token,"amount":str(amount_raw),"chainId":ROBINHOOD_EVM_CHAIN_ID,"tokenOut":token_out,"tokenOutChainId":ROBINHOOD_EVM_CHAIN_ID})
    approval=data.get("approval")
    if approval:
        tx=_tx_from_api(approval,wallet_address)
        _,_,approval_status=await _sign_send(private_key,tx)
        if approval_status!="confirmed":
            raise SwapError(f"Token approval transaction did not confirm (status={approval_status}); stopping before the swap to avoid signing against an unapproved allowance.")
    cancel=data.get("cancel")
    if cancel: raise SwapError("Uniswap requires an approval reset before the swap; the wallet flow stopped before spending funds.")

async def get_quote(input_mint:str,output_mint:str,amount_wei:int,slippage_bps:int=150,swapper:str|None=None)->dict:
    await assert_chain()
    if not swapper or not swapper.startswith("0x"): raise SwapError("A real Robinhood Chain wallet address is required to quote a swap.")
    body={"tokenIn":input_mint,"tokenOut":output_mint,"tokenInChainId":ROBINHOOD_EVM_CHAIN_ID,"tokenOutChainId":ROBINHOOD_EVM_CHAIN_ID,"type":"EXACT_INPUT","amount":str(amount_wei),"swapper":swapper,"slippageTolerance":slippage_bps/100.0,"protocols":["V2","V3","V4"],"routingPreference":"BEST_PRICE"}
    response=await _api("POST","/quote",body)
    if response.get("routing")!="CLASSIC": raise SwapError(f"Unsupported live routing returned by Uniswap: {response.get('routing')}. AMM-only execution is enabled for deterministic server-side signing.")
    quote=dict(response["quote"]); quote["outAmount"]=str((quote.get("output") or {}).get("amount") or "0")
    quote["_response"]=response; quote["_token_in"]=input_mint; quote["_token_out"]=output_mint; quote["_amount_in"]=int(amount_wei); quote["_swapper"]=swapper
    return quote

async def build_swap_transaction(quote:dict,user_public_key:str,priority_fee_wei:int|str="auto") -> str:
    import json
    response=quote.get("_response") or {}
    if response.get("routing")!="CLASSIC": raise SwapError("Only CLASSIC Uniswap AMM routes are enabled for server-side signing.")
    swap=await _api("POST","/swap",{"quote":(quote.get("_response") or {}).get("quote",quote),"deadline":int(time.time()+60),"safetyMode":"SAFE","simulateTransaction":True})
    tx=swap.get("swap") or {}
    if not tx.get("data") or tx.get("data")=="0x": raise SwapError("Uniswap returned empty swap calldata.")
    tx["_priority_fee_wei"]=priority_fee_wei  # carried through the JSON round-trip to sign_send_and_confirm; see _fee_fields
    return json.dumps(tx,separators=(",",":"))

async def sign_send_and_confirm(tx_payload:str,secret_bytes:bytes)->dict:
    import json
    try: tx_data=json.loads(tx_payload)
    except Exception as exc: raise SwapError("Invalid EVM transaction payload.") from exc
    priority_fee_wei=tx_data.pop("_priority_fee_wei","auto")
    tx=_tx_from_api(tx_data,Account.from_key(secret_bytes).address)
    signature,receipt,status=await _sign_send(secret_bytes,tx,priority_fee_wei=priority_fee_wei)
    return {"status":status,"signature":signature,"receipt":receipt,"err":None if status=="confirmed" else status}

async def _execute_swap(user_id:int,contract:str,amount_raw:int,input_token:str,output_token:str,slippage_bps:int,priority_fee_wei:int|str="auto")->dict:
    from domain.trading.real.robinhood_wallet import get_real_wallet
    from infra.kms.wallet_crypto import decrypt_secret
    wallet=await get_real_wallet(user_id)
    if not wallet:return {"ok":False,"reason":"No active Robinhood Chain wallet. Use /wallet to create one."}
    secret=decrypt_secret(wallet.encrypted_secret,wallet.encryption_nonce)
    try:
        await _approval_if_needed(secret,wallet.public_key,input_token,amount_raw,output_token)
        body={"tokenIn":input_token,"tokenOut":output_token,"tokenInChainId":ROBINHOOD_EVM_CHAIN_ID,"tokenOutChainId":ROBINHOOD_EVM_CHAIN_ID,"type":"EXACT_INPUT","amount":str(amount_raw),"swapper":wallet.public_key,"slippageTolerance":slippage_bps/100.0,"protocols":["V2","V3","V4"],"routingPreference":"BEST_PRICE"}
        quote=await _api("POST","/quote",body)
        if quote.get("routing")!="CLASSIC": raise SwapError(f"Unsupported live routing returned by Uniswap: {quote.get('routing')}. AMM-only execution is enabled for deterministic server-side signing.")
        swap=await _api("POST","/swap",{"quote":quote["quote"],"deadline":int(time.time()+60),"safetyMode":"SAFE","simulateTransaction":True})
        tx=_tx_from_api(swap.get("swap",{}),wallet.public_key)
        signature,receipt,status=await _sign_send(secret,tx,priority_fee_wei=priority_fee_wei)
        if status=="failed":
            return {"ok":False,"uncertain":False,"signature":signature,"reason":f"Transaction reverted on-chain (tx: {signature})."}
        if status=="unknown":
            return {"ok":False,"uncertain":True,"signature":signature,"reason":f"Transaction broadcast but confirmation not observed (tx: {signature})."}
        return {"ok":True,"signature":signature,"confirmation":status,"quote":quote,"receipt":receipt,"amount_raw":amount_raw}
    finally:
        del secret

async def execute_buy(user_id:int,contract:str,eth_amount:float,slippage_bps:int=150,**_:Any)->dict:
    if eth_amount<=0:return {"ok":False,"reason":"Amount must be greater than 0 ETH."}
    return await _execute_swap(user_id,contract,int(eth_amount*10**18),NATIVE_ETH_ADDRESS,contract,slippage_bps)

async def execute_sell(user_id:int,contract:str,token_amount:float,slippage_bps:int=150,**_:Any)->dict:
    if token_amount<=0:return {"ok":False,"reason":"Token amount must be greater than 0."}
    decimals=await get_mint_decimals(contract)
    return await _execute_swap(user_id,contract,int(token_amount*10**decimals),contract,NATIVE_ETH_ADDRESS,slippage_bps)

async def get_confirmed_transaction_deltas(signature:str,wallet_address:str,contract:str)->dict:
    receipt=await rpc_call("eth_getTransactionReceipt",[signature])
    if not receipt: raise SwapError("Transaction receipt not available yet.")
    target=wallet_address.lower().replace("0x",""); token=contract.lower(); delta=0
    for log in receipt.get("logs",[]):
        if log.get("address","").lower()!=token or not log.get("topics") or log["topics"][0].lower()!=TRANSFER_TOPIC or len(log["topics"])<3: continue
        frm=log["topics"][1][-40:].lower(); to=log["topics"][2][-40:].lower(); value=int(log.get("data","0x0"),16)
        if to==target: delta+=value
        if frm==target: delta-=value
    block=int(receipt.get("blockNumber","0x0"),16); before=0; after=0
    try:
        if block>0: before=int(await rpc_call("eth_getBalance",[wallet_address,hex(block-1)]),16)
        after=int(await rpc_call("eth_getBalance",[wallet_address,hex(block)]),16)
    except Exception as exc: logger.warning("Native balance delta lookup failed for %s: %s",signature,exc)
    return {"token_delta_raw":delta,"sol_delta_wei":after-before,"native_delta_wei":after-before}
