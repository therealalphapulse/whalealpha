"""Unit tests for integrations/chain_scanner.py's pure block parser
(`_extract_fee_payer_candidates`) — dict-in, set-out, no DB/network/RPC.
"""

from __future__ import annotations

from whale_alpha.integrations.chain_scanner import SWAP_PROGRAM_IDS, _extract_fee_payer_candidates

TRADER = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
OTHER_TRADER = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
UNRELATED_PROGRAM = "11111111111111111111111111111111"


def _tx(account_keys: list[str], *, err: object = None) -> dict:
    return {
        "meta": {"err": err},
        "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in account_keys]}},
    }


def _block(transactions: list[dict]) -> dict:
    return {"transactions": transactions}


def test_extracts_fee_payer_from_a_jupiter_swap_transaction():
    block = _block([_tx([TRADER, JUPITER, UNRELATED_PROGRAM])])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == {TRADER}


def test_extracts_fee_payer_when_program_is_reached_via_a_cpi_not_the_top_level_call():
    # e.g. Jupiter routing into Raydium in the same tx — both program ids
    # appear in accountKeys either way, regardless of instruction nesting.
    block = _block([_tx([TRADER, JUPITER, RAYDIUM_V4])])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == {TRADER}


def test_ignores_transactions_that_never_touch_a_swap_program():
    block = _block([_tx([TRADER, UNRELATED_PROGRAM])])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == set()


def test_ignores_failed_transactions():
    block = _block([_tx([TRADER, JUPITER], err={"InstructionError": [0, "Custom"]})])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == set()


def test_deduplicates_the_same_fee_payer_across_multiple_transactions():
    block = _block([_tx([TRADER, JUPITER]), _tx([TRADER, RAYDIUM_V4])])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == {TRADER}


def test_collects_multiple_distinct_traders_in_one_block():
    block = _block([_tx([TRADER, JUPITER]), _tx([OTHER_TRADER, RAYDIUM_V4])])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == {TRADER, OTHER_TRADER}


def test_respects_max_wallets_per_block_cap():
    transactions = [_tx([f"wallet{i}" + "1" * 30, JUPITER]) for i in range(10)]
    block = _block(transactions)
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=3)
    assert len(found) <= 3


def test_never_treats_a_program_id_itself_as_a_trader():
    # Malformed/unexpected shape where accountKeys[0] happens to be a
    # program id — must not be reported as a "trader" fee payer.
    block = _block([_tx([JUPITER, TRADER])])
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == set()


def test_handles_plain_string_account_keys_not_just_pubkey_dicts():
    block = {
        "transactions": [
            {
                "meta": {"err": None},
                "transaction": {"message": {"accountKeys": [TRADER, JUPITER]}},
            }
        ]
    }
    found = _extract_fee_payer_candidates(block, set(SWAP_PROGRAM_IDS), max_wallets=200)
    assert found == {TRADER}


def test_empty_block_returns_no_candidates():
    assert _extract_fee_payer_candidates(_block([]), set(SWAP_PROGRAM_IDS), max_wallets=200) == set()
