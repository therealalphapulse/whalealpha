import asyncio
import html
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from domain.admin.user_service import get_or_create_user
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.real.robinhood_wallet import (
    get_real_wallet,
    create_wallet,
    import_wallet,
    export_wallet_secret,
    disconnect_wallet,
    get_wallet_settings,
    set_wallet_slippage,
    set_wallet_priority_tier,
    get_automation_status,
    set_auto_trading,
    set_auto_kill_switch,
    set_auto_daily_cap,
    WalletImportError,
)
from domain.trading.real.robinhood_swap import get_native_balance, get_mint_decimals, NATIVE_ETH_ADDRESS
from domain.trading.real.evm_multichain import get_multi_chain_snapshot
from domain.trading.real import real_trade_engine
from domain.trading.real import robinhood_withdraw as wallet_withdraw
from domain.trading.real import real_dca_engine
from domain.trading.real import real_automation_engine
from domain.trading.real import real_exit_engine
from domain.trading.real import real_trailing_stop_engine
from domain.trading.real import real_limit_order_engine
from domain.intelligence.robinhood_wallet_portfolio import (
    fetch_wallet_fungible_tokens,
    build_wallet_portfolio_report,
    get_wallet_portfolio_value,
    format_usd,
)
from app_platform.keyboards.real_wallet import (
    real_wallet_onboarding_kb,
    real_wallet_created_kb,
    real_wallet_menu_kb,
    real_wallet_disconnect_confirm_kb,
    real_wallet_export_warning_kb,
    real_wallet_buy_presets_kb,
    real_wallet_positions_list_kb,
    real_wallet_settings_kb,
    real_trade_position_kb,
    real_wallet_back_kb,
    real_wallet_withdraw_asset_kb,
    real_wallet_withdraw_amount_kb,
    real_wallet_withdraw_confirm_kb,
    real_wallet_automation_kb,
    real_wallet_automation_filters_kb,
    real_wallet_dca_list_kb,
    real_wallet_dca_detail_kb,
    real_wallet_dca_cancel_confirm_kb,
    real_wallet_dca_skip_optional_kb,
    real_wallet_exit_menu_kb,
    real_wallet_trailing_menu_kb,
    real_wallet_limit_list_kb,
    real_wallet_limit_detail_kb,
    real_wallet_limit_direction_kb,
    BUY_PRESETS_ETH,
)

logger = logging.getLogger("WhaleAlpha.RealWalletCmd")
router = Router()
