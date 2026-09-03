from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def token_actions_keyboard(
    contract: str,
    pair_url: str | None = None,
    website_url: str | None = None,
    twitter_url: str | None = None,
    telegram_url: str | None = None,
) -> InlineKeyboardMarkup:
    keyboard = []

    first_row = [
        # Opens the in-app Real Wallet quick-buy presets (rwbuy:menu:<contract>
        # in bot/commands/real_wallet.py) instead of sending the user out to
        # jup.ag — same "tap once, pick an amount" flow as Trojan. Falls back
        # gracefully to the wallet-setup prompt if they have no Real Wallet
        # connected yet; nothing about /realwallet itself changes.
        InlineKeyboardButton(text="⚡ Buy", callback_data=f"rwbuy:menu:{contract}"),
    ]

    if pair_url:
        first_row.append(
            InlineKeyboardButton(text="📊 Chart", url=pair_url)
        )

    keyboard.append(first_row)

    keyboard.append([
        InlineKeyboardButton(
            text="👁 Track",
            callback_data=f"autoscan:track:{contract}"
        ),
        InlineKeyboardButton(
            text="📝 Paper Buy",
            callback_data=f"paper_buy:{contract}"
        ),
    ])

    keyboard.append([
        InlineKeyboardButton(
            text="🔒 Security",
            callback_data=f"autoscan:security:{contract}"
        ),
        InlineKeyboardButton(
            text="🔁 Refresh",
            callback_data=f"autoscan:refresh:{contract}"
        ),
    ])

    # Social/project links — only shown when the token actually exposes
    # them (DexScreener info.websites/socials). Never fabricated.
    social_row = []
    if website_url:
        social_row.append(InlineKeyboardButton(text="🌐 Website", url=website_url))
    if twitter_url:
        social_row.append(InlineKeyboardButton(text="🐦 Twitter", url=twitter_url))
    if telegram_url:
        social_row.append(InlineKeyboardButton(text="✈️ Telegram", url=telegram_url))

    if social_row:
        keyboard.append(social_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
