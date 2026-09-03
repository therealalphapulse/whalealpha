import re
import logging
from collections import defaultdict

from providers.marketdata.dexscreener import get_trending_tokens
from providers.marketdata.geckoterminal import get_trending_tokens as get_gt_trending_tokens

logger = logging.getLogger("AlphaPulse.Narrative")

# Narrative keywords (expand as needed)
# Broadened to cover the current memecoin ecosystem, not just the original
# 7 buckets. classify_token() below is unchanged — it just iterates
# whatever's in this dict, so new categories plug in automatically for
# both the /narratives report and the pump-radar scoring bonus.
NARRATIVE_KEYWORDS = {
    "AI": [
        "ai", "gpt", "agent", "agentic", "neural", "brain", "intelligence",
        "deep", "tensor", "compute", "inference", "singularity", "chat",
        "llm", "model", "train", "predict", "cortex", "synapse", "mind",
        "robot", "automaton", "sentient", "machine", "algo"
    ],
    "MEME_AI": [
        "pepe", "goat", "truth", "terminal", "fart", "ai16z", "zerebro",
        "ai", "agent", "meme", "clanker", "aixbt"
    ],
    "MEME_ANIMAL": [
        "pepe", "dog", "cat", "frog", "woof", "bonk", "shib", "doge",
        "floki", "samoyed", "corgi", "puppy", "wif", "hat", "duck",
        "penguin", "pengu", "hamster", "monkey", "ape", "chimp",
        "gorilla", "bear", "bull", "panda", "koala", "sloth",
        "capybara", "otter", "shrimp", "whale", "turtle", "snake",
        "lizard", "wolf", "fox", "pig", "cow", "chicken", "rat",
        "mouse", "squirrel", "elephant", "tiger", "lion", "bird",
        "owl", "goose", "moo", "deng", "cheems"
    ],
    "MEME_CULTURE": [
        "meme", "moon", "mars", "diamond", "hands", "wen", "based",
        "chad", "gigachad", "wojak", "virgin", "npc", "cope", "seethe",
        "copium", "ratio", "ngmi", "wagmi", "sigma", "rizz", "skibidi",
        "ohio", "fanum", "gyat", "cringe", "sus", "amogus", "brainrot"
    ],
    "POLITICS": [
        "trump", "maga", "biden", "kamala", "harris", "vance", "elon",
        "musk", "doge", "potus", "president", "senate", "congress",
        "election", "vote", "republican", "democrat", "libertarian",
        "politician", "government", "tariff", "policy", "patriot",
        "liberty", "freedom", "capitol"
    ],
    "CELEBRITY_POPCULTURE": [
        "kanye", "drake", "taylor", "swift", "kardashian", "mrbeast",
        "pewdiepie", "influencer", "streamer", "viral", "tiktok",
        "youtuber", "celebrity", "hollywood", "rapper", "singer",
        "actor", "icon", "star"
    ],
    "SPORTS": [
        "nba", "nfl", "soccer", "football", "basketball", "olympic",
        "worldcup", "fifa", "superbowl", "athlete", "champion", "goal",
        "score", "playoff", "boxing", "ufc", "mma", "sport", "team",
        "league", "coach", "stadium"
    ],
    "SPACE": [
        "space", "moon", "mars", "rocket", "nasa", "astronaut",
        "galaxy", "cosmos", "orbit", "satellite", "alien", "ufo",
        "star", "comet", "nebula", "planet", "lunar", "cosmic"
    ],
    "DEFI": [
        "swap", "lend", "borrow", "stake", "yield", "farm", "pool",
        "vault", "bridge", "liquid", "trade", "perps", "dydx",
        "gmx", "uni", "cake", "sushi", "curve", "balancer"
    ],
    "INFRA_DEPIN": [
        "bridge", "cross", "chain", "rollup", "layer", "oracle", "rpc",
        "node", "validator", "staking", "restaking", "avs", "eigen",
        "depin", "iot", "wireless", "storage", "gpu", "sensor",
        "hotspot", "compute", "decentralized"
    ],
    "GAMING": [
        "game", "play", "guild", "gamer", "rpg", "metaverse", "world",
        "land", "hero", "sword", "quest", "adventure", "arena", "battle",
        "esports", "loot", "level", "boss", "dungeon"
    ],
    "RWA": [
        "real", "asset", "treasury", "bond", "property", "commodity",
        "gold", "silver", "oil", "carbon", "credit", "invoice"
    ],
    "SOLANA_NATIVE": [
        "solana", "sol", "bonk", "wif", "popcat", "mew", "book",
        "jupiter", "raydium", "pumpfun", "pump", "phantom", "backpack",
        "jito", "tensor"
    ],
    "FOOD_DRINK": [
        "pizza", "burger", "taco", "coffee", "beer", "wine", "sushi",
        "ramen", "banana", "apple", "cookie", "cake", "candy", "donut",
        "snack", "boba", "milk", "juice", "soda"
    ],
    "ANIME_KPOP": [
        "anime", "manga", "waifu", "senpai", "kawaii", "otaku", "kpop",
        "idol", "chibi", "sensei"
    ],
    "HISTORY_MYTH": [
        "zeus", "thor", "odin", "pharaoh", "pyramid", "ancient",
        "legend", "myth", "dragon", "phoenix", "titan", "olympus",
        "viking", "samurai", "ninja", "knight", "warrior", "gladiator"
    ],
}


def classify_token(name: str, symbol: str) -> list[str]:
    """
    Classify a token into one or more narratives based on name/symbol.
    Returns list of matching narrative names.
    """
    text = (name + " " + symbol).lower()
    matches = []

    for narrative, keywords in NARRATIVE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                matches.append(narrative)
                break  # One match per narrative is enough

    if not matches:
        matches.append("OTHER")

    return matches


async def scan_narratives() -> dict:
    """
    Scan trending tokens and classify them by narrative.
    Returns:
    {
        "AI": {"count": 5, "total_volume": 1_000_000, "total_liquidity": 500_000, "tokens": [...]},
        "MEME_ANIMAL": {...},
        ...
    }
    """
    # Try GeckoTerminal first, DexScreener as fallback
    tokens = await get_gt_trending_tokens()
    source = "GeckoTerminal"
    
    if not tokens:
        tokens = await get_trending_tokens()
        source = "DexScreener (fallback)"

    if not tokens:
        logger.warning("No trending tokens available for narrative scan")
        return {}

    narratives = defaultdict(lambda: {
        "count": 0,
        "total_volume": 0.0,
        "total_liquidity": 0.0,
        "tokens": []
    })

    for token in tokens:
        name = token.get("name", "Unknown")
        symbol = token.get("symbol", "???")
        volume = token.get("volume_24h", 0) or 0
        liquidity = token.get("liquidity", 0) or 0

        try:
            volume = float(volume)
        except (ValueError, TypeError):
            volume = 0.0
        try:
            liquidity = float(liquidity)
        except (ValueError, TypeError):
            liquidity = 0.0

        matched_narratives = classify_token(name, symbol)

        for narrative in matched_narratives:
            narratives[narrative]["count"] += 1
            narratives[narrative]["total_volume"] += volume
            narratives[narrative]["total_liquidity"] += liquidity
            narratives[narrative]["tokens"].append({
                "name": name,
                "symbol": symbol,
                "volume": volume,
                "liquidity": liquidity,
                "contract": token.get("contract", ""),
            })

    # Sort narratives by total volume (highest first)
    sorted_narratives = dict(
        sorted(narratives.items(), key=lambda x: x[1]["total_volume"], reverse=True)
    )

    return sorted_narratives


def format_narrative_report(narratives: dict) -> str:
    """Format narrative scan results into a Telegram message."""
    if not narratives:
        return "⚠️ No narrative data available right now."

    text = "📊 <b>Narrative Scanner</b>\n"
    text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "🔍 <i>Classified by AI/NLP keyword analysis</i>\n\n"

    for narrative, data in narratives.items():
        vol_display = format_number(data["total_volume"])
        liq_display = format_number(data["total_liquidity"])

        # Emoji based on narrative
        emoji_map = {
            "AI": "🤖",
            "MEME_AI": "🤖😂",
            "MEME_ANIMAL": "🐸",
            "MEME_CULTURE": "😂",
            "POLITICS": "🗳️",
            "CELEBRITY_POPCULTURE": "🌟",
            "SPORTS": "🏆",
            "SPACE": "🚀",
            "DEFI": "🏦",
            "INFRA_DEPIN": "🔗",
            "GAMING": "🎮",
            "RWA": "🏛️",
            "SOLANA_NATIVE": "◎",
            "FOOD_DRINK": "🍕",
            "ANIME_KPOP": "🎌",
            "HISTORY_MYTH": "⚔️",
            "OTHER": "📦",
        }
        emoji = emoji_map.get(narrative, "📊")

        text += (
            f"{emoji} <b>{narrative}</b>\n"
            f"   📈 Tokens: <b>{data['count']}</b>\n"
            f"   💰 Volume: <b>{vol_display}</b>\n"
            f"   💧 Liquidity: <b>{liq_display}</b>\n\n"
        )

        # Show top 3 tokens in this narrative
        top_tokens = sorted(data["tokens"], key=lambda x: x["volume"], reverse=True)[:3]
        for token in top_tokens:
            text += f"   → {token['name']} ({token['symbol']})\n"

        text += "\n"

    text += "━━━━━━━━━━━━━━━━━━━━━\n"
    text += "⚡ Powered by AlphaPulse"

    return text


def format_number(value: float) -> str:
    """Format large numbers into readable strings."""
    try:
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        elif value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        elif value >= 1_000:
            return f"${value / 1_000:.2f}K"
        else:
            return f"${value:.2f}"
    except (ValueError, TypeError):
        return "N/A"
