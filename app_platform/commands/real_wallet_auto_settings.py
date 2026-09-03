"""Legacy per-field Auto-Buy Settings entry points.

Superseded by the unified Auto-Buy Filters panel in
app_platform/commands/real_wallet.py, where every setting (amount,
TP, SL, daily limit, and quality filters) shares one edit flow via
the "rw:auto_filter_edit:<field>" callback and always returns the
user to the updated settings card. This router is kept registered
(see app_platform/gateway/app.py) but intentionally defines no
handlers, since app_platform/keyboards/real_wallet.py no longer
emits the old "rw:auto_amount_usdt" / "rw:auto_tp" / "rw:auto_sl" /
"rw:auto_daily_limit" callback data.
"""

from aiogram import Router

router = Router(name="real_wallet_auto_settings")
