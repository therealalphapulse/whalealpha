"""
Backward-compatible Helius RPC facade.

Most callers use the shared MultiRPCManager directly. Holder retrieval has
one Helius-specific response-shape compatibility requirement, so this module
normalizes that shape at the boundary instead of relying on import-time
monkey-patching of the holder module.
"""

from typing import Any

from providers.rpc.multi_rpc_manager import (
    MultiRPCManager,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    PRIORITY_LOW,
    multi_rpc_manager,
)


class _HeliusManagerCompat:
    """Delegate to MultiRPCManager, normalizing only holder V2 responses."""

    def __init__(self, manager: MultiRPCManager):
        self._manager = manager

    async def request_json(self, *args: Any, **kwargs: Any):
        data = await self._manager.request_json(*args, **kwargs)
        context = str(kwargs.get("context", ""))

        # Helius has returned getProgramAccountsV2 payloads in both forms:
        #   result: [{"pubkey": ..., "account": ...}, ...]
        # and the newer documented form:
        #   result: {"accounts": [...], "paginationKey": ...}
        #
        # holders.py consumes the normalized second form. Normalize only the
        # holder V2 context so every other RPC caller keeps the exact shared
        # manager response contract.
        if context.startswith("holder_accounts_v2:") and isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, list):
                data = dict(data)
                data["result"] = {"accounts": result}

        return data

    def __getattr__(self, name: str):
        return getattr(self._manager, name)


# Backward compatibility: callers importing helius_manager continue to use
# the shared manager, with the holder-only response normalization above.
helius_manager = _HeliusManagerCompat(multi_rpc_manager)

__all__ = [
    "MultiRPCManager",
    "PRIORITY_HIGH",
    "PRIORITY_NORMAL",
    "PRIORITY_LOW",
    "multi_rpc_manager",
    "helius_manager",
]
