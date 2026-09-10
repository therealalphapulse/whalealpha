"""Native ETH and ERC-20 withdrawals on Robinhood Chain."""
from __future__ import annotations
import re
from eth_account import Account
from infra.kms.wallet_crypto import decrypt_secret
from domain.trading.real.robinhood_swap import rpc_call, get_native_balance, _sign_send
from domain.trading.real.robinhood_wallet import get_real_wallet
SOL_WITHDRAW_RESERVE=0.0005
SOLANA_ADDRESS_RE=re.compile(r"^0x[a-fA-F0-9]{40}$")

def validate_withdraw_address(address:str)->bool:return bool(SOLANA_ADDRESS_RE.fullmatch(address.strip()))
async def get_max_sol_withdrawable(user_id:int)->float:
    w=await get_real_wallet(user_id)
    if not w:return 0.0
    bal=await get_native_balance(w.public_key)
    return max(0.0,bal-SOL_WITHDRAW_RESERVE)

def _erc20_transfer_data(to:str,amount_raw:int)->str:return "0xa9059cbb"+to[2:].lower().rjust(64,"0")+hex(amount_raw)[2:].rjust(64,"0")
async def execute_sol_withdrawal(user_id:int,address:str,amount:float)->dict:
    w=await get_real_wallet(user_id)
    if not w:return {"ok":False,"reason":"No active Robinhood Chain wallet."}
    if not validate_withdraw_address(address):return {"ok":False,"reason":"Invalid Robinhood Chain address."}
    if amount<=0:return {"ok":False,"reason":"Amount must be greater than 0."}
    secret=decrypt_secret(w.encrypted_secret,w.encryption_nonce)
    try:
        balance=await get_native_balance(w.public_key)
        if amount+SOL_WITHDRAW_RESERVE>balance:return {"ok":False,"reason":"Insufficient ETH after reserving network gas."}
        tx={"to":address,"value":int(amount*10**18),"data":"0x"}
        sig,_=await _sign_send(secret,tx); return {"ok":True,"signature":sig,"confirmation":"confirmed"}
    except Exception as e:return {"ok":False,"reason":str(e)}
    finally:del secret
async def execute_spl_withdrawal(user_id:int,mint:str,symbol:str,amount:float,decimals:int,address:str)->dict:
    w=await get_real_wallet(user_id)
    if not w:return {"ok":False,"reason":"No active Robinhood Chain wallet."}
    if not validate_withdraw_address(address):return {"ok":False,"reason":"Invalid Robinhood Chain address."}
    secret=decrypt_secret(w.encrypted_secret,w.encryption_nonce)
    try:
        raw=int(amount*10**decimals)
        tx={"to":mint,"data":_erc20_transfer_data(address,raw),"value":0}
        sig,_=await _sign_send(secret,tx); return {"ok":True,"signature":sig,"confirmation":"confirmed"}
    except Exception as e:return {"ok":False,"reason":str(e)}
    finally:del secret
