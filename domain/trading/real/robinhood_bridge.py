"""Canonical Arbitrum bridge integration: Ethereum <-> Robinhood Chain.

Robinhood Chain is an Arbitrum Orbit rollup, so its canonical bridge IS
the standard Arbitrum bridge -- there is no Robinhood-specific bridge
protocol here. This module only calls Arbitrum's own audited L1/L2
contracts (Delayed Inbox, ArbSys precompile, NodeInterface precompile,
Outbox); it implements no bridge/lock-mint logic of its own. Contract
addresses are pulled from config.settings, sourced from
docs.robinhood.com/chain/protocol-contracts.

THREE-STEP WITHDRAWAL FLOW (Robinhood Chain -> Ethereum)
----------------------------------------------------------
Per docs.robinhood.com/chain/bridging, a withdrawal is NOT a single
transaction:
  1. initiate_withdrawal_to_ethereum() -- call ArbSys.withdrawEth() on L2.
     Burns the ETH on L2 and queues an L2-to-L1 message. Recorded in the
     bridge_withdrawals table so we can track it.
  2. Wait out the ~7-day fraud-proof challenge period
     (BRIDGE_WITHDRAWAL_CHALLENGE_DAYS). Nothing to do here but wait.
  3. claim_withdrawal() -- once claimable, fetch a Merkle proof from the
     NodeInterface precompile and submit it to Outbox.executeTransaction()
     on L1. This is the step that actually releases the ETH on Ethereum;
     it costs L1 gas.

DEPOSITS (Ethereum -> Robinhood Chain) are a single L1 transaction
(Inbox.depositEth) and confirm on L2 in ~10 minutes.

TESTING NOTE: this integrates with real, audited Arbitrum contracts, but
has not been exercised against a live chain from this environment. Test
with a small amount -- ideally on Robinhood Chain Testnet first -- before
relying on it for meaningful funds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from eth_account import Account
from sqlalchemy import select

from infra.kms.wallet_crypto import decrypt_secret
from infra.db.session import AsyncSessionLocal
from models.bridge_withdrawal import BridgeWithdrawal
from domain.trading.real.robinhood_wallet import get_real_wallet
from config.settings import (
    ETHEREUM_CHAIN_ID,
    ETHEREUM_RPC_URL,
    ROBINHOOD_EVM_CHAIN_ID,
    ROBINHOOD_RPC_URL,
    ROBINHOOD_BRIDGE_DELAYED_INBOX,
    ROBINHOOD_BRIDGE_OUTBOX,
    ARBSYS_PRECOMPILE,
    NODE_INTERFACE_PRECOMPILE,
    BRIDGE_WITHDRAWAL_CHALLENGE_DAYS,
    SEPOLIA_CHAIN_ID,
    SEPOLIA_RPC_URL,
    ROBINHOOD_TESTNET_EVM_CHAIN_ID,
    ROBINHOOD_TESTNET_RPC_URL,
    ROBINHOOD_TESTNET_BRIDGE_DELAYED_INBOX,
    ROBINHOOD_TESTNET_BRIDGE_OUTBOX,
)

logger = logging.getLogger("WhaleAlpha.RobinhoodBridge")

BridgeError = RuntimeError

# Function selectors, computed as keccak256(signature)[:4] (verified
# against Arbitrum's published ABIs/source), not hand-typed.
SELECTOR_DEPOSIT_ETH = "0xad9d4ba3"  # depositEth(address)
SELECTOR_WITHDRAW_ETH = "0x25e16063"  # withdrawEth(address)
SELECTOR_CONSTRUCT_OUTBOX_PROOF = "0x42696350"  # constructOutboxProof(uint64,uint64)
SELECTOR_EXECUTE_TRANSACTION = "0x08635a95"  # executeTransaction(bytes32[],uint256,address,address,uint256,uint256,uint256,uint256,bytes)
SELECTOR_SEND_MERKLE_TREE_STATE = "0x33430ff9"  # sendMerkleTreeState()

# topic0 for ArbSys L2ToL1Tx(address,address,uint256,uint256,uint256,uint256,uint256,uint256,bytes)
L2_TO_L1_TX_TOPIC = "0x3e7aafa77dbf186b7fd488006beff893744caa3c4f6f299e8a709fa2087374fc"

_RPC_TIMEOUT = aiohttp.ClientTimeout(total=15)


class _NetworkConfig:
    """Bundles the L1 + L2 endpoint/contract set for one network so every
    function below takes a single `network` argument instead of six.
    "mainnet" is Ethereum <-> Robinhood Chain (real funds). "testnet" is
    Ethereum Sepolia <-> Robinhood Chain Testnet -- a genuinely separate
    L1 chain and separate bridge contract deployment, used to dry-run the
    full deposit/withdraw/claim cycle (including the ~7-day challenge
    period) with worthless test ETH before trusting this with real funds.
    """

    def __init__(self, l1_chain_id, l1_rpc_url, l2_chain_id, l2_rpc_url, delayed_inbox, outbox):
        self.l1_chain_id = l1_chain_id
        self.l1_rpc_url = l1_rpc_url
        self.l2_chain_id = l2_chain_id
        self.l2_rpc_url = l2_rpc_url
        self.delayed_inbox = delayed_inbox
        self.outbox = outbox


_NETWORKS = {
    "mainnet": _NetworkConfig(ETHEREUM_CHAIN_ID, ETHEREUM_RPC_URL, ROBINHOOD_EVM_CHAIN_ID, ROBINHOOD_RPC_URL, ROBINHOOD_BRIDGE_DELAYED_INBOX, ROBINHOOD_BRIDGE_OUTBOX),
    "testnet": _NetworkConfig(SEPOLIA_CHAIN_ID, SEPOLIA_RPC_URL, ROBINHOOD_TESTNET_EVM_CHAIN_ID, ROBINHOOD_TESTNET_RPC_URL, ROBINHOOD_TESTNET_BRIDGE_DELAYED_INBOX, ROBINHOOD_TESTNET_BRIDGE_OUTBOX),
}


def _get_network(network: str) -> _NetworkConfig:
    try:
        return _NETWORKS[network]
    except KeyError:
        raise BridgeError(f"Unknown network {network!r}; expected 'mainnet' or 'testnet'.")


def _pad32(hexstr: str) -> str:
    return hexstr[2:].lower().rjust(64, "0")


def _addr_param(address: str) -> str:
    return _pad32(address)


async def _rpc_call(rpc_url: str, method: str, params: list | None = None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    async with aiohttp.ClientSession() as session:
        async with session.post(rpc_url, json=payload, timeout=_RPC_TIMEOUT) as resp:
            if resp.status != 200:
                raise BridgeError(f"RPC HTTP {resp.status} from {rpc_url}")
            data = await resp.json(content_type=None)
            if data.get("error") is not None:
                raise BridgeError(str(data["error"]))
            return data.get("result")


async def _sign_send(rpc_url: str, chain_id: int, private_key: bytes, tx: dict) -> tuple[str, dict | None, str]:
    """Parameterized broadcast/confirm helper (mirrors
    robinhood_swap._sign_send) so it works for both the L1 (Ethereum) and
    L2 (Robinhood Chain) legs of a bridge operation. Returns
    (tx_hash, receipt_or_none, status) where status is "confirmed" /
    "failed" (on-chain revert) / "unknown" (no receipt observed within
    the window -- NOT safe to assume success or retry)."""
    acct = Account.from_key(private_key)
    tx = dict(tx)
    tx["from"] = acct.address
    tx["chainId"] = chain_id
    tx["nonce"] = int(await _rpc_call(rpc_url, "eth_getTransactionCount", [acct.address, "pending"]))
    if "gas" not in tx:
        tx["gas"] = int(await _rpc_call(rpc_url, "eth_estimateGas", [{k: v for k, v in tx.items() if k != "nonce"}]), 16)
    if "gasPrice" not in tx and "maxFeePerGas" not in tx:
        tx["gasPrice"] = int(await _rpc_call(rpc_url, "eth_gasPrice"), 16)
    signed = acct.sign_transaction(tx)
    raw = "0x" + signed.raw_transaction.hex()
    h = await _rpc_call(rpc_url, "eth_sendRawTransaction", [raw])
    deadline = time.monotonic() + 60
    receipt = None
    while time.monotonic() < deadline:
        try:
            receipt = await _rpc_call(rpc_url, "eth_getTransactionReceipt", [h])
        except BridgeError as exc:
            logger.warning(f"Receipt poll error for {h}: {exc}")
            receipt = None
        if receipt:
            break
        await asyncio.sleep(2)
    if not receipt:
        logger.warning(f"Bridge tx {h} broadcast but confirmation not observed within 60s; status unknown.")
        return h, None, "unknown"
    if int(receipt.get("status", "0x0"), 16) != 1:
        return h, receipt, "failed"
    return h, receipt, "confirmed"


async def deposit_eth_to_robinhood(user_id: int, amount_eth: float, network: str = "mainnet") -> dict:
    """Calls Inbox.depositEth(destAddr) on L1 (Ethereum, or Sepolia for
    network="testnet"). Only step needed for a deposit -- no separate
    claim required."""
    net = _get_network(network)
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Robinhood Chain wallet."}
    if amount_eth <= 0:
        return {"ok": False, "reason": "Amount must be greater than 0."}

    secret = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    try:
        data = "0x" + SELECTOR_DEPOSIT_ETH[2:] + _addr_param(wallet.public_key)
        tx = {"to": net.delayed_inbox, "data": data, "value": int(amount_eth * 10**18)}
        tx_hash, receipt, status = await _sign_send(net.l1_rpc_url, net.l1_chain_id, secret, tx)
        if status != "confirmed":
            return {"ok": False, "reason": f"Deposit transaction did not confirm (status={status}).", "l1_tx_hash": tx_hash}
        return {
            "ok": True,
            "network": network,
            "l1_tx_hash": tx_hash,
            "eta_minutes": 10,
            "note": "Deposit confirmed on L1. Funds typically appear on Robinhood Chain within ~10 minutes.",
        }
    except Exception as e:
        logger.error(f"deposit_eth_to_robinhood failed for user={user_id} network={network}: {e}")
        return {"ok": False, "reason": str(e)}
    finally:
        del secret


def _extract_l2_to_l1_position(receipt: dict) -> str | None:
    """Pulls the indexed `position` field (3rd indexed topic) straight off
    the L2ToL1Tx log -- no data decoding needed since position is indexed."""
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if topics and topics[0].lower() == L2_TO_L1_TX_TOPIC.lower() and len(topics) >= 4:
            return str(int(topics[3], 16))
    return None


async def initiate_withdrawal_to_ethereum(user_id: int, amount_eth: float, network: str = "mainnet") -> dict:
    """Calls ArbSys.withdrawEth(destAddr) on Robinhood Chain L2 (or
    Robinhood Chain Testnet for network="testnet"). Burns the ETH on L2
    and queues the L2-to-L1 message. Records a BridgeWithdrawal row for
    claim_withdrawal() to use later -- this step alone does NOT move
    funds to L1 yet."""
    net = _get_network(network)
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Robinhood Chain wallet."}
    if amount_eth <= 0:
        return {"ok": False, "reason": "Amount must be greater than 0."}

    secret = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    try:
        data = "0x" + SELECTOR_WITHDRAW_ETH[2:] + _addr_param(wallet.public_key)
        tx = {"to": ARBSYS_PRECOMPILE, "data": data, "value": int(amount_eth * 10**18)}
        tx_hash, receipt, status = await _sign_send(net.l2_rpc_url, net.l2_chain_id, secret, tx)
        if status != "confirmed" or receipt is None:
            return {"ok": False, "reason": f"Withdrawal-initiate transaction did not confirm (status={status}).", "l2_tx_hash": tx_hash}

        position = _extract_l2_to_l1_position(receipt)
        if position is None:
            logger.error(f"withdrawEth confirmed ({tx_hash}) but no L2ToL1Tx log found; needs manual follow-up.")
            return {
                "ok": False,
                "reason": "Withdrawal transaction confirmed but its L2-to-L1 message could not be identified. "
                          "Do not retry -- check the transaction on the explorer before proceeding.",
                "l2_tx_hash": tx_hash,
            }

        l2_block_number = int(receipt["blockNumber"], 16)
        l2_block = await _rpc_call(net.l2_rpc_url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
        l2_block_timestamp = int(l2_block["timestamp"], 16)
        claimable_after = datetime.now(timezone.utc) + timedelta(days=BRIDGE_WITHDRAWAL_CHALLENGE_DAYS)

        async with AsyncSessionLocal() as session:
            withdrawal = BridgeWithdrawal(
                user_id=user_id,
                amount_eth=amount_eth,
                destination_address=wallet.public_key,
                network=network,
                l2_tx_hash=tx_hash,
                l2_to_l1_position=position,
                l2_block_number=l2_block_number,
                l2_block_timestamp=l2_block_timestamp,
                claimable_after=claimable_after,
            )
            session.add(withdrawal)
            await session.commit()
            await session.refresh(withdrawal)

        return {
            "ok": True,
            "network": network,
            "l2_tx_hash": tx_hash,
            "withdrawal_id": withdrawal.id,
            "claimable_after": claimable_after.isoformat(),
            "note": f"Withdrawal initiated. It becomes claimable on L1 in ~{BRIDGE_WITHDRAWAL_CHALLENGE_DAYS} days.",
        }
    except Exception as e:
        logger.error(f"initiate_withdrawal_to_ethereum failed for user={user_id} network={network}: {e}")
        return {"ok": False, "reason": str(e)}
    finally:
        del secret


async def get_pending_withdrawals(user_id: int) -> list[BridgeWithdrawal]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BridgeWithdrawal).where(BridgeWithdrawal.user_id == user_id, BridgeWithdrawal.claimed.is_(False))
        )
        return list(result.scalars().all())


async def get_claimable_withdrawals(user_id: int) -> list[BridgeWithdrawal]:
    now = datetime.now(timezone.utc)
    return [w for w in await get_pending_withdrawals(user_id) if w.claimable_after.replace(tzinfo=timezone.utc) <= now]


async def _construct_outbox_proof(net: "_NetworkConfig", size: int, leaf: int) -> dict:
    data = "0x" + SELECTOR_CONSTRUCT_OUTBOX_PROOF[2:] + hex(size)[2:].rjust(64, "0") + hex(leaf)[2:].rjust(64, "0")
    result = await _rpc_call(net.l2_rpc_url, "eth_call", [{"to": NODE_INTERFACE_PRECOMPILE, "data": data}, "latest"])
    raw = bytes.fromhex(result[2:])
    proof_offset = int.from_bytes(raw[64:96], "big")
    proof_len = int.from_bytes(raw[proof_offset:proof_offset + 32], "big")
    proof = []
    for i in range(proof_len):
        start = proof_offset + 32 + i * 32
        proof.append("0x" + raw[start:start + 32].hex())
    return {"proof": proof}


async def _send_merkle_tree_state(net: "_NetworkConfig") -> int:
    result = await _rpc_call(net.l2_rpc_url, "eth_call", [{"to": ARBSYS_PRECOMPILE, "data": SELECTOR_SEND_MERKLE_TREE_STATE}, "latest"])
    raw = bytes.fromhex(result[2:])
    return int.from_bytes(raw[0:32], "big")


async def claim_withdrawal(user_id: int, withdrawal_id: int) -> dict:
    """Step 3: builds the outbox Merkle proof via NodeInterface and
    submits Outbox.executeTransaction on L1 to release the ETH. Only
    valid once claimable_after has passed -- calling early simply fails
    the L1 transaction (Arbitrum's Outbox enforces the challenge period
    itself). Network (mainnet/testnet) is read from the withdrawal row
    itself, set when it was initiated -- a withdrawal always claims on
    the same network it started on."""
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Robinhood Chain wallet."}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BridgeWithdrawal).where(BridgeWithdrawal.id == withdrawal_id, BridgeWithdrawal.user_id == user_id)
        )
        withdrawal = result.scalar_one_or_none()

    if not withdrawal:
        return {"ok": False, "reason": "Withdrawal not found."}
    if withdrawal.claimed:
        return {"ok": False, "reason": "This withdrawal has already been claimed.", "l1_claim_tx_hash": withdrawal.l1_claim_tx_hash}
    if withdrawal.claimable_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
        return {"ok": False, "reason": f"Not claimable yet -- available after {withdrawal.claimable_after.isoformat()}."}

    net = _get_network(withdrawal.network or "mainnet")
    secret = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    try:
        leaf = int(withdrawal.l2_to_l1_position)
        size = await _send_merkle_tree_state(net)
        if leaf >= size:
            return {"ok": False, "reason": "Outbox tree does not yet include this withdrawal's leaf -- try again shortly."}

        proof_data = await _construct_outbox_proof(net, size, leaf)

        num_head_words = 9
        head = [
            hex(num_head_words * 32)[2:].rjust(64, "0"),                     # offset to proof[]
            hex(leaf)[2:].rjust(64, "0"),                                    # index
            _addr_param(withdrawal.destination_address),                     # l2Sender
            _addr_param(withdrawal.destination_address),                     # to
            hex(withdrawal.l2_block_number)[2:].rjust(64, "0"),               # l2Block
            "0" * 64,                                                        # l1Block -- Arbitrum always emits 0 here; must match exactly
            hex(withdrawal.l2_block_timestamp)[2:].rjust(64, "0"),            # l2Timestamp -- exact on-chain value
            hex(int(withdrawal.amount_eth * 10**18))[2:].rjust(64, "0"),      # value
            hex(num_head_words * 32 + 32 + len(proof_data["proof"]) * 32)[2:].rjust(64, "0"),  # offset to `data`
        ]
        tail = [hex(len(proof_data["proof"]))[2:].rjust(64, "0")]
        tail.extend(p[2:] for p in proof_data["proof"])
        tail.append("0" * 64)

        calldata = "0x" + SELECTOR_EXECUTE_TRANSACTION[2:] + "".join(head) + "".join(tail)
        tx = {"to": net.outbox, "data": calldata, "value": 0}
        tx_hash, receipt, status = await _sign_send(net.l1_rpc_url, net.l1_chain_id, secret, tx)

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(BridgeWithdrawal).where(BridgeWithdrawal.id == withdrawal_id))
            row = result.scalar_one()
            if status == "confirmed":
                row.claimed = True
                row.l1_claim_tx_hash = tx_hash
                row.claimed_at = datetime.now(timezone.utc)
            else:
                row.claim_failure_reason = f"status={status}"
            await session.commit()

        if status != "confirmed":
            return {"ok": False, "reason": f"Claim transaction did not confirm (status={status}).", "l1_tx_hash": tx_hash}
        return {"ok": True, "l1_claim_tx_hash": tx_hash}
    except Exception as e:
        logger.error(f"claim_withdrawal failed for user={user_id}, withdrawal_id={withdrawal_id}: {e}")
        return {"ok": False, "reason": str(e)}
    finally:
        del secret
