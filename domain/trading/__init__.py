"""Trading package compatibility helpers."""

# Keep the legacy paper-monitor import compatible with the current PnL
# renderer. The renderer is visual-only; this helper only selects the mascot
# folder tier used by the paper monitor and does not alter trade calculations.
from . import pnl_image as _pnl_image


def reaction_tier_for_pnl(pnl_pct: float = 0.0) -> str:
    try:
        value = float(pnl_pct or 0.0)
    except (TypeError, ValueError):
        value = 0.0

    if value >= 100.0:
        tier = "shocked"
    elif value >= 25.0:
        tier = "excited"
    elif value > 0.0:
        tier = "happy"
    elif value == 0.0:
        tier = "neutral"
    elif value > -25.0:
        tier = "worried"
    else:
        tier = "crying"

    # paper_monitor.py historically imports this symbol from pnl_image.py.
    # Expose the compatibility symbol there without modifying the renderer.
    _pnl_image.reaction_tier_for_pnl = reaction_tier_for_pnl
    return tier


# Install the symbol immediately so `from domain.trading.pnl_image import
# reaction_tier_for_pnl` succeeds during worker startup.
_pnl_image.reaction_tier_for_pnl = reaction_tier_for_pnl
