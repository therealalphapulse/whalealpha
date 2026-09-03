"""Runtime compatibility patch for live Helius getProgramAccountsV2 shapes."""

from __future__ import annotations

import logging

logger = logging.getLogger("AlphaPulse.Holders")


def install() -> None:
    """Normalize documented Helius V2 response variants before holder parsing."""
    from domain.intelligence import holders
    from providers.rpc.helius_request_manager import helius_manager, PRIORITY_LOW

    original = holders._fetch_via_program_accounts_v2
    if getattr(original, "_alphapulse_v2_normalized", False):
        return

    token_program_id = holders.TOKEN_PROGRAM_ID
    page_limit = holders._V2_PAGE_LIMIT
    max_pages = holders._V2_MAX_PAGES
    max_accounts = holders.MAX_HOLDER_ACCOUNTS

    async def normalized(contract_address: str, priority: int = PRIORITY_LOW) -> list[dict] | None:
        all_accounts: list[dict] = []
        pagination_key: str | None = None

        for page in range(max_pages):
            params: dict = {
                "encoding": "jsonParsed",
                "filters": [
                    {"dataSize": 165},
                    {"memcmp": {"offset": 0, "bytes": contract_address}},
                ],
                "limit": page_limit,
            }
            if pagination_key:
                params["paginationKey"] = pagination_key

            payload = {
                "jsonrpc": "2.0",
                "id": "alphapulse-holder-data-v2",
                "method": "getProgramAccountsV2",
                "params": [token_program_id, params],
            }

            data = await helius_manager.request_json(
                "POST",
                "solana-json-rpc:getProgramAccountsV2",
                json_body=payload,
                priority=priority,
                timeout=20,
                context=f"holder_accounts_v2:{contract_address}:page{page}",
            )

            if not isinstance(data, dict) or data.get("error"):
                return None if page == 0 else all_accounts

            result = data.get("result")
            page_accounts = None
            next_key = None

            # Helius documents result={accounts, paginationKey}. Accept the
            # context-wrapped result.value form too, because some RPC gateway
            # layers/proxies wrap JSON-RPC responses with context even when
            # the caller did not request it. This is normalization only; it
            # does not loosen any holder safety rule.
            if isinstance(result, list):
                page_accounts = result
            elif isinstance(result, dict):
                page_accounts = result.get("accounts")
                next_key = result.get("paginationKey")
                if not isinstance(page_accounts, list):
                    value = result.get("value")
                    if isinstance(value, dict):
                        page_accounts = value.get("accounts")
                        next_key = value.get("paginationKey") or result.get("paginationKey")

            if not isinstance(page_accounts, list):
                logger.warning(
                    "[HolderDiag] %s: Helius V2 returned an unrecognized result shape "
                    "on page %d; falling back to legacy holder retrieval",
                    contract_address[:8], page,
                )
                return None if page == 0 else all_accounts

            if page == 0 and not page_accounts:
                return None

            all_accounts.extend(x for x in page_accounts if isinstance(x, dict))
            pagination_key = next_key

            if not pagination_key or not page_accounts:
                break
            if len(all_accounts) >= max_accounts * 4:
                break

        logger.info(
            "[HolderDiag] %s: normalized Helius getProgramAccountsV2 response; "
            "%d raw account entries available",
            contract_address[:8],
            len(all_accounts),
        )
        return all_accounts

    normalized._alphapulse_v2_normalized = True
    holders._fetch_via_program_accounts_v2 = normalized
    logger.info("[HolderDiag] Helius V2 compatibility adapter installed")
