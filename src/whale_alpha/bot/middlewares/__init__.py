"""aiogram middlewares: rate limiting and RBAC.

Note: this package previously also held a stale, superseded copy of the
`create_bot` bootstrap function (missing RedisStorage-backed FSM, the
wallet/manual-trading/alerts routers, and the httpx client threading that
the real bootstrap in `whale_alpha.bot` has). It was never imported by
anything — `main.py` always used `whale_alpha.bot.create_bot` — but it was
left in place and risked being imported by mistake later, so it's been
removed. See `whale_alpha/bot/__init__.py` for the real bootstrap.
"""

from __future__ import annotations
