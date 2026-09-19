"""/bridge -- move ETH between Ethereum and Robinhood Chain via the
canonical Arbitrum bridge. See domain/trading/real/robinhood_bridge.py
for the underlying contract calls; this file is just the Telegram UI."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from domain.trading.real.robinhood_wallet import get_real_wallet
from domain.trading.real.robinhood_bridge import (
    deposit_eth_to_robinhood,
    initiate_withdrawal_to_ethereum,
    claim_withdrawal,
    get_pending_withdrawals,
)
from config.settings import BRIDGE_WITHDRAWAL_CHALLENGE_DAYS

logger = logging.getLogger("WhaleAlpha.BridgeCommand")
router = Router(name="bridge")

HELP_TEXT = (
    "\U0001f309 <b>Bridge (Ethereum \u2194 Robinhood Chain)</b>\n"
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
    "<code>/bridge deposit &lt;amount&gt;</code>\n"
    "Move ETH from Ethereum \u2192 Robinhood Chain. Confirms in ~10 minutes.\n\n"
    "<code>/bridge withdraw &lt;amount&gt;</code>\n"
    f"Start moving ETH from Robinhood Chain \u2192 Ethereum. Takes ~{BRIDGE_WITHDRAWAL_CHALLENGE_DAYS} days "
    "(Arbitrum's fraud-proof challenge period) before it can be claimed.\n\n"
    "<code>/bridge claim &lt;id&gt;</code>\n"
    "Finish a withdrawal once it's past its challenge period.\n\n"
    "<code>/bridge status</code>\n"
    "See pending/claimable withdrawals.\n\n"
    "\u26a0\ufe0f Bridge transactions are irreversible. This uses Arbitrum's official bridge contracts, "
    "not a custom bridge.\n\n"
    "Add <code>testnet</code> to any command (e.g. <code>/bridge deposit 0.01 testnet</code>) to dry-run the full "
    "flow on Robinhood Chain Testnet / Ethereum Sepolia with worthless test ETH first -- "
    "get funded at faucet.testnet.chain.robinhood.com."
)


def _require_amount(command: CommandObject) -> float | None:
    if not command.args:
        return None
    try:
        amount = float(command.args.strip().split()[0])
    except ValueError:
        return None
    return amount if amount > 0 else None


def _pop_testnet_flag(args: list[str]) -> tuple[list[str], str]:
    """`testnet` can appear anywhere after the subcommand, e.g.
    `/bridge deposit 0.01 testnet`. Returns (remaining_args, network)."""
    network = "mainnet"
    remaining = []
    for a in args:
        if a.lower() == "testnet":
            network = "testnet"
        else:
            remaining.append(a)
    return remaining, network


@router.message(Command("bridge"))
async def cmd_bridge(message: Message, command: CommandObject):
    user_id = message.from_user.id
    raw_args = (command.args or "").strip().split()
    sub = raw_args[0].lower() if raw_args else ""
    args, network = _pop_testnet_flag(raw_args[1:])
    net_label = " (testnet)" if network == "testnet" else ""

    wallet = await get_real_wallet(user_id)
    if not wallet and sub in ("deposit", "withdraw", "claim", "status"):
        await message.answer("You need a Robinhood Chain wallet first -- run /realwallet to set one up.")
        return

    if sub == "deposit":
        amount = float(args[0]) if args else None
        if not amount or amount <= 0:
            await message.answer(
                "Usage: <code>/bridge deposit &lt;amount&gt; [testnet]</code>\n"
                "e.g. <code>/bridge deposit 0.05</code> or <code>/bridge deposit 0.05 testnet</code> to dry-run on "
                "Robinhood Chain Testnet / Sepolia with worthless test ETH."
            )
            return
        status_msg = await message.answer(f"\u23f3 Depositing {amount} ETH to Robinhood Chain{net_label}...")
        result = await deposit_eth_to_robinhood(user_id, amount, network=network)
        if result["ok"]:
            await status_msg.edit_text(
                f"\u2705 Deposit confirmed{net_label}.\n<code>{result['l1_tx_hash']}</code>\n\n{result['note']}"
            )
        else:
            await status_msg.edit_text(f"\u274c Deposit failed: {result['reason']}")
        return

    if sub == "withdraw":
        amount = float(args[0]) if args else None
        if not amount or amount <= 0:
            await message.answer(
                "Usage: <code>/bridge withdraw &lt;amount&gt; [testnet]</code> "
                "(add <code>testnet</code> to dry-run on Robinhood Chain Testnet)"
            )
            return
        status_msg = await message.answer(f"\u23f3 Initiating withdrawal of {amount} ETH from Robinhood Chain{net_label}...")
        result = await initiate_withdrawal_to_ethereum(user_id, amount, network=network)
        if result["ok"]:
            await status_msg.edit_text(
                f"\u2705 Withdrawal initiated{net_label} (id <code>{result['withdrawal_id']}</code>).\n"
                f"<code>{result['l2_tx_hash']}</code>\n\n{result['note']}\n\n"
                f"Run <code>/bridge claim {result['withdrawal_id']}</code> once it's claimable."
            )
        else:
            await status_msg.edit_text(f"\u274c Withdrawal failed: {result['reason']}")
        return

    if sub == "claim":
        if not args or not args[0].isdigit():
            await message.answer("Usage: <code>/bridge claim &lt;id&gt;</code> -- see <code>/bridge status</code> for ids.")
            return
        withdrawal_id = int(args[0])
        status_msg = await message.answer(f"\u23f3 Claiming withdrawal {withdrawal_id}...")
        result = await claim_withdrawal(user_id, withdrawal_id)
        if result["ok"]:
            await status_msg.edit_text(f"\u2705 Claimed.\n<code>{result['l1_claim_tx_hash']}</code>")
        else:
            await status_msg.edit_text(f"\u274c Claim failed: {result['reason']}")
        return

    if sub == "status":
        pending = await get_pending_withdrawals(user_id)
        if not pending:
            await message.answer("No pending withdrawals.")
            return
        lines = ["\U0001f4cb <b>Pending withdrawals</b>\n"]
        for w in pending:
            net_tag = " [testnet]" if w.network == "testnet" else ""
            lines.append(
                f"\u2022 id <code>{w.id}</code>{net_tag}: {w.amount_eth} ETH \u2014 claimable after {w.claimable_after.isoformat()}"
            )
        await message.answer("\n".join(lines))
        return

    await message.answer(HELP_TEXT)
