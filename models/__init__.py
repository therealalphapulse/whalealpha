"""
models/__init__.py

NEW in v4. v3 had no __init__.py in this package at all — `import
models` alone registered zero tables against SQLAlchemy's
Base.metadata unless some other, already-imported module happened to
import individual model submodules first. In v3's actual startup
order this was harmless (every command router imports its own model
dependencies before main() ever calls init_db()), but it made `import
models` a misleading, order-dependent statement wherever it appeared
— including, critically, inside a standalone Alembic env.py (v4,
infra/db/migrations/env.py), which imports nothing else first and
would otherwise generate migrations against an incomplete schema.

This file makes `import models` reliably register every table,
regardless of what else has or hasn't been imported yet.
"""

from models.admin_activity_log import AdminActivityLog  # noqa: F401
from models.admin_role import AdminRole  # noqa: F401
from models.alert import Alert  # noqa: F401
from models.daily_trade_archive import DailyTradeArchive  # noqa: F401
from models.kol_subscription import KolAlertSubscription  # noqa: F401
from models.kol_wallet import KolWallet  # noqa: F401
from models.paper_autobuy_filter import PaperAutoBuyFilter  # noqa: F401
from models.paper_dca_fill import PaperDcaFill  # noqa: F401
from models.paper_dca_settings import PaperDCASettings  # noqa: F401
from models.paper_pnl_event import PaperPnlEvent  # noqa: F401
from models.paper_portfolio import PaperPortfolio  # noqa: F401
from models.paper_settings import PaperSettings  # noqa: F401
from models.paper_trade import PaperTrade  # noqa: F401
from models.payment_method import PaymentMethod  # noqa: F401
from models.portfolio import PortfolioPosition  # noqa: F401
from models.premium_membership import PremiumMembership  # noqa: F401
from models.premium_payment import PremiumPayment  # noqa: F401
from models.premium_plan import PremiumPlan  # noqa: F401
from models.premium_signal import PremiumSignal  # noqa: F401
from models.premium_wallet import PremiumWallet  # noqa: F401
from models.premium_wallet_trade import PremiumWalletTrade  # noqa: F401
from models.pump_alerted_token import PumpAlertedToken  # noqa: F401
from models.pump_subscription import PumpAlertSubscription  # noqa: F401
from models.real_autobuy_filter import RealAutoBuyFilter  # noqa: F401
from models.real_dca_fill import RealDCAFill  # noqa: F401
from models.real_dca_schedule import RealDCASchedule  # noqa: F401
from models.real_exit_rule import RealExitRule  # noqa: F401
from models.real_limit_order import RealLimitOrder  # noqa: F401
from models.real_trade import RealTrade  # noqa: F401
from models.real_wallet import RealWallet  # noqa: F401
from models.signal_event import Milestone, SignalEvent  # noqa: F401
from models.signal_milestone import SignalMilestone  # noqa: F401
from models.signal_token import SignalToken  # noqa: F401
from models.system_flag import SystemFlag  # noqa: F401
from models.tracked_wallet import TrackedWallet  # noqa: F401
from models.user import User  # noqa: F401
from models.wallet_withdrawal import WalletWithdrawal  # noqa: F401
from models.watchlist import Watchlist  # noqa: F401

__all__ = [
    "AdminActivityLog",
    "AdminRole",
    "Alert",
    "DailyTradeArchive",
    "KolAlertSubscription",
    "KolWallet",
    "PaperAutoBuyFilter",
    "PaperDcaFill",
    "PaperDCASettings",
    "PaperPnlEvent",
    "PaperPortfolio",
    "PaperSettings",
    "PaperTrade",
    "PaymentMethod",
    "PortfolioPosition",
    "PremiumMembership",
    "PremiumPayment",
    "PremiumPlan",
    "PremiumSignal",
    "PremiumWallet",
    "PremiumWalletTrade",
    "PumpAlertedToken",
    "PumpAlertSubscription",
    "RealAutoBuyFilter",
    "RealDCAFill",
    "RealDCASchedule",
    "RealExitRule",
    "RealLimitOrder",
    "RealTrade",
    "RealWallet",
    "Milestone",
    "SignalEvent",
    "SignalMilestone",
    "SignalToken",
    "SystemFlag",
    "TrackedWallet",
    "User",
    "WalletWithdrawal",
    "Watchlist",
]
