"""
Shared between 0002_add_user_foreign_keys.py and
0003_validate_user_foreign_keys.py so the two migrations can't drift out
of sync with each other — the constraint names validated in 0003 must be
exactly the ones created in 0002.

Table names below are the actual `__tablename__` values read directly
from models/*.py (not guessed) — confirmed by grepping every model file's
__tablename__ during v4 authoring. No live database was available to
cross-check this against, so this is "matches the source code" verified,
not "matches a live deployed schema" verified; run the orphan-check step
in infra/db/migrations/README.md regardless, which will surface a
mismatch immediately (as a Postgres "relation does not exist" error) if
a deployed database has ever diverged from what models/*.py declares.
"""

# (table, column, constraint_name)
FK_TARGETS = [
    ("admin_activity_logs", "admin_user_id", "fk_admin_activity_logs_admin_user_id"),
    ("admin_activity_logs", "target_user_id", "fk_admin_activity_logs_target_user_id"),
    ("admin_roles", "user_id", "fk_admin_roles_user_id"),
    ("alerts", "user_id", "fk_alerts_user_id"),
    ("daily_trade_archives", "user_id", "fk_daily_trade_archives_user_id"),
    ("kol_alert_subscriptions", "user_id", "fk_kol_alert_subscriptions_user_id"),
    ("paper_autobuy_filters", "user_id", "fk_paper_autobuy_filters_user_id"),
    ("paper_dca_fills", "user_id", "fk_paper_dca_fills_user_id"),
    ("paper_dca_settings", "user_id", "fk_paper_dca_settings_user_id"),
    ("paper_pnl_events", "user_id", "fk_paper_pnl_events_user_id"),
    ("paper_portfolios", "user_id", "fk_paper_portfolios_user_id"),
    ("paper_settings", "user_id", "fk_paper_settings_user_id"),
    ("paper_trades", "user_id", "fk_paper_trades_user_id"),
    ("portfolio_positions", "user_id", "fk_portfolio_positions_user_id"),
    ("premium_memberships", "user_id", "fk_premium_memberships_user_id"),
    ("premium_payments", "user_id", "fk_premium_payments_user_id"),
    ("pump_alert_subscriptions", "user_id", "fk_pump_alert_subscriptions_user_id"),
    ("real_autobuy_filters", "user_id", "fk_real_autobuy_filters_user_id"),
    ("real_dca_schedules", "user_id", "fk_real_dca_schedules_user_id"),
    ("real_exit_rules", "user_id", "fk_real_exit_rules_user_id"),
    ("real_limit_orders", "user_id", "fk_real_limit_orders_user_id"),
    ("real_trades", "user_id", "fk_real_trades_user_id"),
    ("real_wallets", "user_id", "fk_real_wallets_user_id"),
    ("tracked_wallets", "user_id", "fk_tracked_wallets_user_id"),
    ("wallet_withdrawals", "user_id", "fk_wallet_withdrawals_user_id"),
    ("watchlists", "user_id", "fk_watchlists_user_id"),
]
