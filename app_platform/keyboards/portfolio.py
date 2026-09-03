from aiogram.types import InlineKeyboardButton


def portfolio_hub_row(active: str = "real") -> list[InlineKeyboardButton]:
    """
    Returns a single keyboard row used to switch between the Real Portfolio
    and Paper Portfolio views. `active` marks which view is currently shown
    ("real" or "paper") so the current tab is visually indicated.

    Used by bot/commands/portfolio.py and bot/commands/paper_trading.py to
    keep navigation between the two portfolio views consistent.
    """
    real_label = "✅ 💼 Real Portfolio" if active == "real" else "💼 Real Portfolio"
    paper_label = "✅ 📝 Paper Portfolio" if active == "paper" else "📝 Paper Portfolio"

    return [
        InlineKeyboardButton(text=real_label, callback_data="portfolio_hub:real"),
        InlineKeyboardButton(text=paper_label, callback_data="portfolio_hub:paper"),
    ]
