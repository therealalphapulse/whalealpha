"""Robinhood Chain EVM execution using standard JSON-RPC + Uniswap Trading API."""
from __future__ import annotations
import asyncio, logging, time
from typing import Any
import aiohttp
from eth_account import Account
from infra.kms.wallet_crypto import decrypt_secret
from config.settings import ROBINHOOD_EVM_CHAIN_ID, ROBINHOOD_RPC_URL, ROBINHOOD_UNISWAP_API_KEY

logger=logging.getLogger("WhaleAlpha.RobinhoodSwap")
NATIVE_ETH_ADDRESS="0x0000000000000000000000000000000000000000"
SwapError=RuntimeError
API_URL="https://trade-api.gateway.uniswap.org/v1"
UNIVERSAL_ROUTER_VERSION="2.1.1"
ERC20_BALANCE_OF="70a08231"; ERC20_DECIMALS="313ce567"; ERC20_SYMBOL="95d89b41"
TRANSFER_TOPIC="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

async def rpc_call(method:str, params:list[Any]|None=None)->Any:
    payload={"jsonrpc":"2.0","id":1,"method":method,"params":params or []}
    async with aiohttp.ClientSession() as session:
        async with session.post(ROBINHOOD_RPC_URL,json=payload,timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status!=200: raise SwapError(f"Robinhood Chain RPC HTTP {resp.status}")
            data=await resp.json(content_type=None)
            if data.get("error") is not None: raise SwapError(str(data["error"]))
            return data.get("result")

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

async def get_token_balance(address:str, token:str)->dict:
    data="0x"+ERC20_BALANCE_OF+address[2:].lower().rjust(64,"0")
    raw=await rpc_call("eth_call",[{"to":token,"data":data},"latest"])
    raw_amount=int(raw,16)
    decimals=await get_mint_decimals(token)
    ui_amount=(raw_amount/(10**decimals)) if decimals else float(raw_amount)
    return {"raw_amount":raw_amount,"decimals":decimals,"ui_amount":ui_amount,"token_address":token,"token_accounts":[]}

async def get_mint_decimals(token:str)->int:
    raw=await rpc_call("eth_call",[{"to":token,"data":"0x"+ERC20_DECIMALS},"latest"])
    return int(raw,16)

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
    if not ROBINHOOD_UNISWAP_API_KEY:
        raise SwapError("UNISWAP_API_KEY is not configured; Robinhood Chain live trading is fail-closed until the production API key is set.")
    headers={"x-api-key":ROBINHOOD_UNISWAP_API_KEY,"accept":"application/json","content-type":"application/json","x-permit2-disabled":"true","x-universal-router-version":UNIVERSAL_ROUTER_VERSION}
    async with aiohttp.ClientSession() as s:
        async with s.request(method,API_URL+path,headers=headers,json=body,timeout=aiohttp.ClientTimeout(total=15)) as r:
            data=await r.json(content_type=None)
            if r.status>=400: raise SwapError(f"Uniswap API {r.status}: {data}")
            return data

def _tx_from_api(tx:dict,from_address:str)->dict:
    if not tx.get("to") or not tx.get("data") or tx.get("data")=="0x": raise SwapError("Uniswap returned invalid transaction calldata.")
    out={"to":tx["to"],"from":from_address,"data":tx["data"],"value":int(tx.get("value","0")),"chainId":ROBINHOOD_EVM_CHAIN_ID,"nonce":int(tx.get("nonce",0))}
    if tx.get("gasLimit"): out["gas"]=int(tx["gasLimit"])
    if tx.get("gasPrice"): out["gasPrice"]=int(tx["gasPrice"])
    else:
        if tx.get("maxFeePerGas"): out["maxFeePerGas"]=int(tx["maxFeePerGas"])
        if tx.get("maxPriorityFeePerGas"): out["maxPriorityFeePerGas"]=int(tx["maxPriorityFeePerGas"])
    return out

async def _sign_send(private_key:bytes, tx:dict)->tuple[str,dict|None,str]:
    """Broadcasts and waits for confirmation. Returns (tx_hash, receipt_or_none, status)
    where status is one of "confirmed" (receipt status==1), "failed" (receipt status==0,
    i.e. an on-chain revert -- a definite, known outcome, safe to treat as a clean
    failure rather than something requiring reconciliation), or "unknown" (broadcast
    succeeded but no receipt was observed within the confirmation window -- outcome
    is NOT known and callers must not blindly retry)."""
    acct=Account.from_key(private_key)
    tx=dict(tx); tx["from"]=acct.address; tx["chainId"]=ROBINHOOD_EVM_CHAIN_ID
    tx["nonce"]=int(await rpc_call("eth_getTransactionCount",[acct.address,"pending"]))
    if "gas" not in tx:
        tx["gas"]=int(await rpc_call("eth_estimateGas",[{k:v for k,v in tx.items() if k!="nonce"}]),16)
    if "gasPrice" not in tx and "maxFeePerGas" not in tx:
        tx["gasPrice"]=int(await rpc_call("eth_gasPrice"),16)
    signed=acct.sign_transaction(tx)
    raw="0x"+signed.raw_transaction.hex()
    h=await rpc_call("eth_sendRawTransaction",[raw])
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
    return json.dumps(tx,separators=(",",":"))

async def sign_send_and_confirm(tx_payload:str,secret_bytes:bytes)->dict:
    import json
    try: tx_data=json.loads(tx_payload)
    except Exception as exc: raise SwapError("Invalid EVM transaction payload.") from exc
    tx=_tx_from_api(tx_data,Account.from_key(secret_bytes).address)
    signature,receipt,status=await _sign_send(secret_bytes,tx)
    return {"status":status,"signature":signature,"receipt":receipt,"err":None if status=="confirmed" else status}

async def _execute_swap(user_id:int,contract:str,amount_raw:int,input_token:str,output_token:str,slippage_bps:int)->dict:
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
        signature,receipt,status=await _sign_send(secret,tx)
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
