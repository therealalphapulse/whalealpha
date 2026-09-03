import asyncio
import io
import logging
import math
import os
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger("AlphaPulse.PnlImage")

# Reference composition: tall 4:5 portrait, premium neon trade-result card.
WIDTH, HEIGHT = 1000, 1250
PAD = 54
BG = (2, 5, 3)
PANEL = (4, 10, 6)
PANEL_SOFT = (6, 14, 9)
GREEN = (0, 236, 74)
GREEN_DARK = (8, 65, 30)
GREEN_GLOW = (0, 255, 80)
WHITE = (245, 248, 246)
SECONDARY = (170, 181, 174)
MUTED = (105, 119, 110)
DIVIDER = (20, 54, 31)
RED = (246, 72, 82)

_FONT_PATHS = {
    "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
}
_FONT_CACHE = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(_FONT_PATHS["bold" if bold else "regular"], size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _fit(draw, text, font, max_width):
    text = str(text or "")
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while len(text) > 2 and draw.textbbox((0, 0), text + "…", font=font)[2] > max_width:
        text = text[:-1]
    return text + "…"


def _center(draw, text, y, font, fill=WHITE):
    box = draw.textbbox((0, 0), str(text), font=font)
    x = (WIDTH - (box[2] - box[0])) / 2
    draw.text((x, y), str(text), font=font, fill=fill)


def _fmt_price(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if v <= 0:
        return "N/A"
    if v < 0.000001:
        return f"${v:.10f}"
    if v < 0.01:
        return f"${v:.8f}"
    return f"${v:,.6f}"


def _fmt_money(value, signed=True):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if signed:
        return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    return f"${v:,.2f}"


def _fmt_compact_money(value):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


def _glow(img, box, color=GREEN_GLOW, blur=24, alpha=150):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=24, outline=color + (alpha,), width=5)
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(layer)


def _rounded_panel(draw, box, radius=20, outline=DIVIDER, fill=PANEL, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _load_image(data):
    path = data.get("mascot_path") or data.get("logo_path")
    if path and os.path.exists(path):
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            pass
    url = data.get("logo_url") or data.get("image_url")
    if url:
        try:
            with urlopen(url, timeout=4) as r:
                raw = r.read(2_000_000)
            return Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception as exc:
            logger.debug("PnL logo fetch failed: %s", exc)
    return None


def _paste_logo(img, logo, cx, cy, size):
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fd = ImageDraw.Draw(frame)
    fd.ellipse([4, 4, size - 4, size - 4], fill=(3, 12, 6, 255), outline=GREEN, width=3)
    if logo is not None:
        logo.thumbnail((size - 18, size - 18), Image.Resampling.LANCZOS)
        x = (size - logo.width) // 2
        y = (size - logo.height) // 2
        frame.alpha_composite(logo, (x, y))
    img.alpha_composite(frame, (int(cx - size / 2), int(cy - size / 2)))


def _draw_chart(draw, x0, y0, x1, y1, pnl_pct):
    # Decorative performance visualization; trade numbers remain sourced from data.
    n = 28
    magnitude = max(-0.35, min(0.95, float(pnl_pct or 0) / 220.0))
    pts = []
    for i in range(n):
        t = i / (n - 1)
        wave = math.sin(i * 1.55) * 0.035 + math.sin(i * 0.47) * 0.025
        progress = t * magnitude + wave * (0.35 + t)
        x = x0 + (x1 - x0) * t
        y = y1 - (y1 - y0) * (0.08 + progress)
        pts.append((x, y))
    pts[0] = (x0, y1 - 5)
    pts[-1] = (x1, y1 - max(12, (y1 - y0) * (0.08 + magnitude)))
    # subtle grid
    for gy in range(int(y0), int(y1), 38):
        draw.line([(x0, gy), (x1, gy)], fill=(8, 35, 18), width=1)
    draw.line(pts, fill=GREEN, width=4, joint="curve")
    fill_pts = pts + [(x1, y1), (x0, y1)]
    draw.polygon(fill_pts, fill=(0, 70, 30, 100))
    for i in range(5, n, 2):
        bx = pts[i][0]
        top = pts[i][1] + 12
        draw.rectangle([bx - 5, top, bx + 5, y1], fill=(0, 45, 20, 100))
    ex, ey = pts[-1]
    draw.ellipse([ex - 9, ey - 9, ex + 9, ey + 9], fill=GREEN)
    draw.line([(ex - 8, ey + 7), (ex + 34, ey - 35)], fill=GREEN, width=5)
    draw.polygon([(ex + 34, ey - 35), (ex + 20, ey - 32), (ex + 29, ey - 20)], fill=GREEN)


def _stat_value(data, *keys, default=None):
    for key in keys:
        if data.get(key) is not None:
            return data.get(key)
    return default


def _draw_card(data: dict) -> Image.Image:
    pnl_usd = float(data.get("pnl_usd") or 0)
    pnl_pct = float(data.get("pnl_pct") or 0)
    accent = GREEN if pnl_pct >= 0 else RED
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))

    # Outer neon frame.
    _glow(img, [22, 22, WIDTH - 22, HEIGHT - 22], accent, 30, 180)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([22, 22, WIDTH - 22, HEIGHT - 22], radius=32, fill=BG + (255,), outline=accent, width=3)

    # Brand/header: deliberately clean like the supplied reference.
    draw.text((PAD + 8, 48), "⚡", font=_font(48, True), fill=accent)
    draw.text((PAD + 70, 54), "AlphaPulse", font=_font(36, True), fill=WHITE)
    badge = "REAL TRADE RESULT"
    bw = draw.textbbox((0, 0), badge, font=_font(16, True))[2] + 34
    bx = (WIDTH - bw) / 2
    draw.rounded_rectangle([bx, 112, bx + bw, 150], radius=20, fill=PANEL_SOFT, outline=accent, width=1)
    draw.text((bx + 17, 121), badge, font=_font(16, True), fill=accent)

    # Token identity and logo.
    symbol = str(data.get("symbol") or "???").upper()
    name = _fit(draw, data.get("name") or symbol, _font(42, True), 500)
    logo = _load_image(data)
    _paste_logo(img, logo, 165, 254, 160)
    draw = ImageDraw.Draw(img)
    draw.text((345, 210), f"${symbol}", font=_font(42, True), fill=WHITE)
    draw.text((345, 270), "SOLANA", font=_font(25, True), fill=SECONDARY)
    # small Solana-style mark
    draw.polygon([(345, 307), (365, 297), (397, 297), (377, 307)], fill=(78, 160, 255))
    draw.polygon([(345, 320), (365, 310), (397, 310), (377, 320)], fill=(143, 76, 255))
    status = str(data.get("status") or "OPEN").replace("_", " ").upper()
    sw = draw.textbbox((0, 0), status, font=_font(16, True))[2] + 34
    sx = WIDTH - PAD - sw
    draw.rounded_rectangle([sx, 220, WIDTH - PAD, 264], radius=22, fill=PANEL_SOFT, outline=accent, width=1)
    draw.text((sx + 17, 231), status, font=_font(16, True), fill=accent)

    # Entry / exit prices.
    y = 350
    box_w = (WIDTH - PAD * 2 - 20) // 2
    for x, label, value in [
        (PAD, "ENTRY PRICE", _fmt_price(data.get("entry_price"))),
        (PAD + box_w + 20, "EXIT PRICE", _fmt_price(_stat_value(data, "exit_price", "current_price"))),
    ]:
        _rounded_panel(draw, [x, y, x + box_w, y + 118], radius=20, outline=GREEN_DARK, fill=PANEL, width=2)
        _center_x = x + box_w / 2
        box = draw.textbbox((0, 0), label, font=_font(18, True))
        draw.text((_center_x - (box[2] - box[0]) / 2, y + 24), label, font=_font(18, True), fill=accent)
        value_font = _font(25, True)
        box = draw.textbbox((0, 0), value, font=value_font)
        draw.text((_center_x - (box[2] - box[0]) / 2, y + 67), value, font=value_font, fill=WHITE)

    # PnL: the main visual focus.
    py0, py1 = 494, 858
    _glow(img, [PAD, py0, WIDTH - PAD, py1], accent, 18, 100)
    draw = ImageDraw.Draw(img)
    _rounded_panel(draw, [PAD, py0, WIDTH - PAD, py1], radius=22, outline=accent, fill=PANEL, width=2)
    _center(draw, "PNL", py0 + 24, _font(22, True), accent)
    pct_text = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
    _center(draw, pct_text, py0 + 65, _font(92, True), accent)
    _draw_chart(draw, PAD + 32, py0 + 188, WIDTH - PAD - 32, py1 - 30, pnl_pct)
    _center(draw, "NET P/L", py1 - 29, _font(14, True), MUTED)

    # Profit / initial / final summary.
    sy0, sy1 = 880, 995
    col_w = (WIDTH - PAD * 2 - 20) / 3
    initial = float(data.get("usd_invested") or 0)
    final = initial + pnl_usd
    summary = [("PROFIT", _fmt_money(pnl_usd), accent), ("INITIAL", _fmt_money(initial, False), WHITE), ("FINAL", _fmt_money(final, False), WHITE)]
    for i, (label, value, fill) in enumerate(summary):
        x = PAD + i * (col_w + 10)
        _rounded_panel(draw, [x, sy0, x + col_w, sy1], radius=18, outline=GREEN_DARK, fill=PANEL, width=2)
        box = draw.textbbox((0, 0), label, font=_font(17, True))
        draw.text((x + (col_w - (box[2] - box[0])) / 2, sy0 + 20), label, font=_font(17, True), fill=SECONDARY)
        value_font = _font(25, True)
        box = draw.textbbox((0, 0), value, font=value_font)
        draw.text((x + (col_w - (box[2] - box[0])) / 2, sy0 + 58), value, font=value_font, fill=fill)

    # Trade stats exactly in the reference style, using only supplied data.
    ty0, ty1 = 1017, 1181
    draw.text((PAD, 1005), "TRADE STATS", font=_font(18, True), fill=accent)
    _rounded_panel(draw, [PAD, ty0, WIDTH - PAD, ty1], radius=18, outline=GREEN_DARK, fill=PANEL, width=2)

    entry_mc = _stat_value(data, "entry_market_cap", "entry_mcap")
    exit_mc = _stat_value(data, "exit_market_cap", "exit_mcap")
    hold = _stat_value(data, "hold_time", "hold_time_str")
    rows = [
        ("Entry Market Cap", _fmt_compact_money(entry_mc) if entry_mc is not None else None),
        ("Exit Market Cap", _fmt_compact_money(exit_mc) if exit_mc is not None else None),
        ("Hold Time", str(hold) if hold is not None else None),
        ("Entry Price", _fmt_price(data.get("entry_price"))),
        ("Exit Price", _fmt_price(_stat_value(data, "exit_price", "current_price"))),
        ("ROI", f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"),
    ]
    available = [r for r in rows if r[1] is not None]
    row_h = max(25, (ty1 - ty0 - 12) // max(1, len(available)))
    y = ty0 + 6
    for idx, (label, value) in enumerate(available):
        draw.text((PAD + 28, y + 4), label, font=_font(16), fill=SECONDARY)
        vf = _font(17, True)
        box = draw.textbbox((0, 0), value, font=vf)
        draw.text((WIDTH - PAD - 28 - (box[2] - box[0]), y + 4), value, font=vf, fill=accent if label == "ROI" else WHITE)
        if idx < len(available) - 1:
            draw.line([(PAD + 28, y + row_h - 3), (WIDTH - PAD - 28, y + row_h - 3)], fill=DIVIDER, width=1)
        y += row_h

    # Neon heartbeat footer from the supplied reference.
    fy = 1210
    draw.line([(PAD + 30, fy), (PAD + 340, fy)], fill=GREEN_DARK, width=2)
    draw.line([(WIDTH - PAD - 340, fy), (WIDTH - PAD - 30, fy)], fill=GREEN_DARK, width=2)
    pts = [(WIDTH/2 - 62, fy), (WIDTH/2 - 42, fy), (WIDTH/2 - 30, fy - 28), (WIDTH/2 - 17, fy + 22), (WIDTH/2, fy - 42), (WIDTH/2 + 17, fy + 18), (WIDTH/2 + 30, fy - 16), (WIDTH/2 + 43, fy), (WIDTH/2 + 62, fy)]
    draw.line(pts, fill=accent, width=4, joint="curve")
    draw.ellipse([WIDTH/2 - 70, fy - 4, WIDTH/2 - 62, fy + 4], fill=accent)
    draw.ellipse([WIDTH/2 + 62, fy - 4, WIDTH/2 + 70, fy + 4], fill=accent)

    return img.convert("RGB")


def _render_to_bytes(data: dict) -> bytes:
    img = _draw_card(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()


async def generate_pnl_card_image(data: dict) -> bytes | None:
    """Generate the production AlphaPulse PnL card off the event loop.

    The renderer is intentionally visual-only: it never changes trade data or
    PnL calculations. It accepts the existing card payload and only rearranges
    how those values are presented in the 4:5 reference design.
    """
    try:
        return await asyncio.to_thread(_render_to_bytes, data)
    except Exception as exc:
        logger.exception("PnL card generation failed: %s", exc)
        return None
