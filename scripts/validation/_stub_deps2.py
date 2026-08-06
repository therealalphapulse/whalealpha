"""Additional minimal stand-ins so wallet_discovery_source.py's REAL,
unmodified functions can be imported and exercised directly — only the
third-party/heavy-config edges are faked (registered directly in
sys.modules); `whale_alpha`, `whale_alpha.integrations`, `whale_alpha.utils`
are all real, trivial packages and are left to import normally from disk.
"""
import types, sys

config_mod = types.ModuleType("whale_alpha.config")
class Env:
    pass
config_mod.Env = Env
sys.modules["whale_alpha.config"] = config_mod

solana_mod = types.ModuleType("solana")
solana_rpc_mod = types.ModuleType("solana.rpc")
solana_rpc_async_api_mod = types.ModuleType("solana.rpc.async_api")
class AsyncClient:
    pass
solana_rpc_async_api_mod.AsyncClient = AsyncClient
sys.modules["solana"] = solana_mod
sys.modules["solana.rpc"] = solana_rpc_mod
sys.modules["solana.rpc.async_api"] = solana_rpc_async_api_mod

sc_mod = types.ModuleType("whale_alpha.integrations.solana_connection")
async def get_token_largest_accounts(*a, **kw): return []
async def get_wallet_first_activity_slot(*a, **kw): return None
async def get_wallet_recent_transactions(*a, **kw): return []
sc_mod.get_token_largest_accounts = get_token_largest_accounts
sc_mod.get_wallet_first_activity_slot = get_wallet_first_activity_slot
sc_mod.get_wallet_recent_transactions = get_wallet_recent_transactions
sys.modules["whale_alpha.integrations.solana_connection"] = sc_mod
