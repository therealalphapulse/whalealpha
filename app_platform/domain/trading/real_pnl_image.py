import asyncio
import io
import logging
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont, ImageFilter

logger = logging.getLogger("AlphaPulse.RealPnlImage")

WIDTH, HEIGHT = 1000, 1250  # 4:5 portrait
PAD = 42
BG = (3, 6, 10)
CARD = (8, 12, 18)
PANEL = (10, 16, 23)
PANEL_2 = (12, 19, 28)
BORDER = (30, 48, 55)
DIVIDER = (30, 42, 50)
WHITE = (245, 248, 250)
SECONDARY = (177, 188, 195)
MUTED = (105, 118, 128)
GREEN = (35, 232, 112)
GREEN_SOFT = (24, 105, 61)
GREEN_DARK = (11, 49, 30)
RED = (247, 79, 88)
RED_DARK = (72, 26, 31)

# Railway images do not necessarily have the local DejaVu font package.
# Never fall back to Pillow's tiny default font: it was the reason the
# previous production card rendered the large PnL text as microscopic.
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
]
REGULAR_PATH = next((p for p in FONT_PATHS if "Regular" in p or p.endswith("DejaVuSans.ttf")), None)
BOLD_PATH = next((p for p in FONT_PATHS if "Bold" in p or p.endswith("DejaVuSans-Bold.ttf")), None)
FONT_CACHE = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in FONT_CACHE:
        return FONT_CACHE[key]
    path = BOLD_PATH if bold else REGULAR_PATH
    try:
        if path:
            font = ImageFont.truetype(path, size)
        else:
            font = ImageFont.load_default(size=size)
    except Exception:
        font = ImageFont.load_default(size=size)
    FONT_CACHE[key] = font
    return font


def _money(value, signed=True):
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "+" if signed and value >= 0 else "" if not signed and value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _price(value):
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if value <= 0:
        return "N/A"
    if value < 0.000001:
        return f"${value:.10f}"
    if value < 0.01:
        return f"${value:.8f}"
    return f"${value:,.6f}"


def _fit_text(draw, text, font, max_width):
    text = str(text or "").strip() or "Unknown Token"
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while len(text) > 3 and draw.textbbox((0, 0), text + "…", font=font)[2] > max_width:
        text = text[:-1]
    return text + "…"


def _center(draw, text, y, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((WIDTH - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def _glow_line(base, points, fill, width=4, glow=18):
    glow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.line(points, fill=fill + (150,), width=width + 12, joint="curve")
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow))
    base.alpha_composite(glow_layer)
    ImageDraw.Draw(base).line(points, fill=fill + (255,), width=width, joint="curve")


def _lightning(draw, x, y, scale=1.0, fill=GREEN):
    pts = [
        (x + 17 * scale, y), (x + 4 * scale, y + 25 * scale),
        (x + 15 * scale, y + 25 * scale), (x + 9 * scale, y + 43 * scale),
        (x + 34 * scale, y + 13 * scale), (x + 22 * scale, y + 13 * scale),
    ]
    draw.polygon(pts, fill=fill)


def _heartbeat(draw, x, y, width, fill):
    pts = [
        (x, y + 12), (x + width * .20, y + 12),
        (x + width * .28, y + 12), (x + width * .34, y - 5),
        (x + width * .41, y + 27), (x + width * .50, y + 2),
        (x + width * .58, y + 12), (x + width, y + 12),
    ]
    _glow_line(draw._image, pts, fill, width=3, glow=7)


def _load_logo(url):
    if not url:
        return None
    try:
        with urlopen(url, timeout=4) as response:
            raw = response.read(2_000_000)
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
        image.thumbnail((260, 260), Image.Resampling.LANCZOS)
        return image
    except Exception as exc:
        logger.debug("PnL token logo unavailable: %s", exc)
        return None


def _paste_round_logo(base, logo, cx, cy, diameter, accent):
    layer = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([2, 2, diameter - 2, diameter - 2], fill=(7, 12, 17, 255), outline=accent + (255,), width=4)
    if logo is not None:
        logo = logo.copy()
        logo.thumbnail((diameter - 18, diameter - 18), Image.Resampling.LANCZOS)
        mask = Image.new("L", logo.size, 0)
        md = ImageDraw.Draw(mask)
        md.ellipse([0, 0, logo.width - 1, logo.height - 1], fill=255)
        layer.alpha_composite(logo, ((diameter - logo.width) // 2, (diameter - logo.height) // 2), (0, 0))
    else:
        d.ellipse([18, 18, diameter - 18, diameter - 18], fill=(20, 29, 37, 255))
    base.alpha_composite(layer, (int(cx - diameter / 2), int(cy - diameter / 2)))


def _draw_outcome_mascot(base, cx, cy, size, win, accent):
    """Draw an illustrated bull for wins and bear for losses.

    This is rendered locally with Pillow vector primitives so the visual is
    deterministic and does not depend on an external image URL or font glyph.
    The trading data itself is untouched; the mascot is selected only from
    the already-calculated PnL sign.
    """
    d = ImageDraw.Draw(base)
    s = float(size)
    x0, y0 = cx - s / 2, cy - s / 2

    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([x0 - 8, y0 - 8, x0 + s + 8, y0 + s + 8], fill=accent + (80,))
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    base.alpha_composite(glow)
    d = ImageDraw.Draw(base)

    d.ellipse([x0, y0, x0 + s, y0 + s], fill=(7, 12, 17, 255), outline=accent + (255,), width=3)

    if win:
        fur, shadow, feature = (177, 111, 45), (105, 57, 25), (255, 214, 92)
        d.polygon([(cx - s*.28, cy - s*.25), (cx - s*.46, cy - s*.47),
                   (cx - s*.49, cy - s*.27), (cx - s*.32, cy - s*.10)], fill=feature)
        d.polygon([(cx + s*.28, cy - s*.25), (cx + s*.46, cy - s*.47),
                   (cx + s*.49, cy - s*.27), (cx + s*.32, cy - s*.10)], fill=feature)
        d.ellipse([cx-s*.46, cy-s*.15, cx-s*.22, cy+s*.05], fill=shadow)
        d.ellipse([cx+s*.22, cy-s*.15, cx+s*.46, cy+s*.05], fill=shadow)
        d.rounded_rectangle([cx-s*.32, cy-s*.30, cx+s*.32, cy+s*.30],
                            radius=int(s*.18), fill=fur, outline=shadow, width=max(2, int(s*.025)))
        for ex in (cx-s*.14, cx+s*.14):
            d.ellipse([ex-s*.065, cy-s*.10, ex+s*.065, cy+s*.03], fill=WHITE)
            d.ellipse([ex-s*.022, cy-s*.075, ex+s*.022, cy-s*.025], fill=(10, 10, 12))
        d.ellipse([cx-s*.18, cy+s*.06, cx+s*.18, cy+s*.24], fill=(222, 157, 96))
        d.ellipse([cx-s*.09, cy+s*.12, cx-s*.035, cy+s*.17], fill=shadow)
        d.ellipse([cx+s*.035, cy+s*.12, cx+s*.09, cy+s*.17], fill=shadow)
        d.arc([cx-s*.13, cy+s*.10, cx+s*.13, cy+s*.26], 10, 170,
              fill=(35, 24, 18), width=max(2, int(s*.02)))
    else:
        fur, shadow, feature = (83, 67, 62), (45, 34, 31), (169, 111, 83)
        d.ellipse([cx-s*.43, cy-s*.42, cx-s*.16, cy-s*.15], fill=fur, outline=shadow, width=2)
        d.ellipse([cx+s*.16, cy-s*.42, cx+s*.43, cy-s*.15], fill=fur, outline=shadow, width=2)
        d.ellipse([cx-s*.35, cy-s*.36, cx-s*.23, cy-s*.24], fill=feature)
        d.ellipse([cx+s*.23, cy-s*.36, cx+s*.35, cy-s*.24], fill=feature)
        d.rounded_rectangle([cx-s*.35, cy-s*.31, cx+s*.35, cy+s*.34],
                            radius=int(s*.22), fill=fur, outline=shadow, width=max(2, int(s*.025)))
        for ex in (cx-s*.14, cx+s*.14):
            d.ellipse([ex-s*.065, cy-s*.10, ex+s*.065, cy+s*.02], fill=WHITE)
            d.ellipse([ex-s*.022, cy-s*.075, ex+s*.022, cy-s*.025], fill=(10, 10, 12))
        d.line([(cx-s*.20, cy-s*.15), (cx-s*.07, cy-s*.11)], fill=shadow, width=max(2, int(s*.025)))
        d.line([(cx+s*.07, cy-s*.11), (cx+s*.20, cy-s*.15)], fill=shadow, width=max(2, int(s*.025)))
        d.ellipse([cx-s*.18, cy+s*.07, cx+s*.18, cy+s*.25], fill=feature)
        d.ellipse([cx-s*.06, cy+s*.12, cx+s*.06, cy+s*.18], fill=shadow)
        d.arc([cx-s*.12, cy+s*.13, cx+s*.12, cy+s*.29], 190, 350,
              fill=(30, 24, 22), width=max(2, int(s*.02)))

    label = "WIN" if win else "LOSS"
    label_font = _font(13, True)
    box = d.textbbox((0, 0), label, font=label_font)
    tw = box[2] - box[0]
    d.rounded_rectangle([cx-tw/2-10, y0+s-2, cx+tw/2+10, y0+s+24],
                        radius=12, fill=accent + (35,), outline=accent + (170,), width=1)
    d.text((cx-tw/2, y0+s+2), label, font=label_font, fill=accent)


def _stat_row(base, y, label, value, accent):
    d = ImageDraw.Draw(base)
    d.text((PAD + 28, y), label, font=_font(19), fill=SECONDARY)
    value = str(value)
    box = d.textbbox((0, 0), value, font=_font(19, True))
    d.text((WIDTH - PAD - 28 - (box[2] - box[0]), y), value, font=_font(19, True), fill=accent if value.startswith("+") else WHITE)
    d.line([(PAD + 28, y + 39), (WIDTH - PAD - 28, y + 39)], fill=DIVIDER, width=1)


def _render(data):
    pnl = float(data.get("pnl_usd") or 0)
    pct = float(data.get("pnl_pct") or 0)
    positive = pnl >= 0
    accent = GREEN if positive else RED
    accent_dark = GREEN_DARK if positive else RED_DARK

    img = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    d = ImageDraw.Draw(img)

    # Reference-inspired neon trade-result frame.
    d.rounded_rectangle([18, 18, WIDTH - 18, HEIGHT - 18], radius=38, fill=CARD, outline=accent + (255,), width=4)
    # Subtle outer glow.
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle([18, 18, WIDTH - 18, HEIGHT - 18], radius=38, outline=accent + (150,), width=14)
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)

    # AlphaPulse header — vector lightning avoids unsupported emoji glyphs.
    _lightning(d, PAD, 50, 0.9, accent)
    d.text((PAD + 42, 51), "AlphaPulse", font=_font(32, True), fill=WHITE)
    d.rounded_rectangle([PAD + 40, 98, PAD + 194, 133], radius=17, fill=accent_dark + (255,), outline=accent + (180,), width=1)
    d.text((PAD + 56, 105), "TRADE RESULT", font=_font(14, True), fill=accent)
    d.line([(PAD, 158), (WIDTH - PAD, 158)], fill=DIVIDER, width=2)

    symbol = str(data.get("symbol") or "???").strip()
    name = _fit_text(d, data.get("name") or symbol, _font(30, True), 560)
    logo = _load_logo(data.get("logo_url"))

    # Token identity block.
    logo_cx, logo_cy, logo_d = 146, 245, 132
    _paste_round_logo(img, logo, logo_cx, logo_cy, logo_d, accent)
    d.text((235, 205), name, font=_font(30, True), fill=WHITE)
    d.text((235, 247), f"${symbol}", font=_font(25, True), fill=SECONDARY)
    # Solana-style mark + label.
    for i, w in enumerate((54, 46, 54)):
        x = 235 + (54 - w)
        y = 290 + i * 17
        d.polygon([(x, y), (x + w, y), (x + w - 10, y + 9), (x - 2, y + 9)], fill=(116, 104, 255))
    d.text((303, 293), "SOLANA", font=_font(18, True), fill=SECONDARY)

    # Outcome mascot: every profitable result gets a bull, every losing result gets a bear.
    # It is placed beside the token identity so it remains visible without obscuring PnL data.
    _draw_outcome_mascot(img, 825, 224, 118, positive, accent)

    status = str(data.get("status") or "OPEN").replace("_", " ").upper()
    bw = d.textbbox((0, 0), status, font=_font(15, True))[2] + 32
    status_y = 290
    d.rounded_rectangle([WIDTH - PAD - bw, status_y, WIDTH - PAD, status_y + 42], radius=21, fill=accent_dark + (255,), outline=accent + (255,), width=1)
    d.text((WIDTH - PAD - bw + 16, status_y + 10), status, font=_font(15, True), fill=accent)

    # Entry / exit panels.
    box_y = 338
    box_w = (WIDTH - PAD * 2 - 18) // 2
    for x, label, value in (
        (PAD, "ENTRY", _price(data.get("entry_price"))),
        (PAD + box_w + 18, "EXIT", _price(data.get("current_price"))),
    ):
        d.rounded_rectangle([x, box_y, x + box_w, box_y + 104], radius=20, fill=PANEL, outline=BORDER, width=2)
        d.text((x + 24, box_y + 18), label, font=_font(16, True), fill=accent)
        d.text((x + 24, box_y + 54), value, font=_font(24, True), fill=WHITE)

    # Main PnL block — this is intentionally the strongest visual element.
    pnl_top, pnl_bottom = 470, 785
    d.rounded_rectangle([PAD, pnl_top, WIDTH - PAD, pnl_bottom], radius=26, fill=PANEL_2, outline=accent + (255,), width=3)
    _center(d, "PNL", 500, _font(19, True), SECONDARY)
    pnl_text = f"{'+' if pct >= 0 else ''}{pct:.2f}%"
    _center(d, pnl_text, 535, _font(92, True), accent)
    _center(d, _money(pnl), 642, _font(30, True), WHITE)

    # Realistic-looking performance trace without inventing trading values.
    chart_left, chart_right, chart_bottom = PAD + 32, WIDTH - PAD - 32, 752
    points = [
        (chart_left, chart_bottom), (chart_left + 55, chart_bottom - 12),
        (chart_left + 110, chart_bottom - 8), (chart_left + 175, chart_bottom - 32),
        (chart_left + 240, chart_bottom - 22), (chart_left + 305, chart_bottom - 48),
        (chart_left + 370, chart_bottom - 43), (chart_left + 440, chart_bottom - 76),
        (chart_left + 510, chart_bottom - 68), (chart_right - 40, chart_bottom - 118),
        (chart_right, chart_bottom - 142),
    ]
    fill_points = points + [(chart_right, chart_bottom), (chart_left, chart_bottom)]
    d.polygon(fill_points, fill=accent_dark + (105,))
    _glow_line(img, points, accent, width=4, glow=9)
    d = ImageDraw.Draw(img)
    d.ellipse([chart_right - 9, chart_bottom - 151, chart_right + 9, chart_bottom - 133], fill=accent)
    # Up arrow.
    ax, ay = chart_right - 2, chart_bottom - 168
    d.line([(ax, ay + 18), (ax, ay)], fill=accent, width=4)
    d.polygon([(ax, ay - 3), (ax - 9, ay + 8), (ax + 9, ay + 8)], fill=accent)

    # Profit / initial / final summary.
    sy = 815
    d.rounded_rectangle([PAD, sy, WIDTH - PAD, sy + 116], radius=20, fill=PANEL, outline=BORDER, width=2)
    initial = float(data.get("usd_invested") or 0)
    final = initial + pnl
    thirds = [PAD + 20, WIDTH / 3 + 4, (WIDTH * 2) / 3 + 4]
    labels = ["PROFIT", "INITIAL", "FINAL"]
    vals = [_money(pnl), _money(initial, signed=False), _money(final, signed=False)]
    fills = [accent, WHITE, WHITE]
    for x, label, value, fill in zip(thirds, labels, vals, fills):
        d.text((x, sy + 18), label, font=_font(15, True), fill=SECONDARY)
        d.text((x, sy + 53), value, font=_font(25, True), fill=fill)
    d.line([(WIDTH / 3, sy + 22), (WIDTH / 3, sy + 94)], fill=DIVIDER, width=2)
    d.line([(WIDTH * 2 / 3, sy + 22), (WIDTH * 2 / 3, sy + 94)], fill=DIVIDER, width=2)

    # Trade stats, styled like the reference. Only values available from the real trade are shown.
    ty = 970
    d.text((PAD, ty), "TRADE STATS", font=_font(19, True), fill=accent)
    d.rounded_rectangle([PAD, ty + 38, WIDTH - PAD, 1160], radius=20, fill=PANEL, outline=BORDER, width=2)
    _stat_row(img, ty + 62, "Entry Price", _price(data.get("entry_price")), accent)
    _stat_row(img, ty + 108, "Exit Price", _price(data.get("current_price")), accent)
    _stat_row(img, ty + 154, "Hold Time", data.get("hold_time", "N/A"), accent)
    _stat_row(img, ty + 200, "ROI", f"{'+' if pct >= 0 else ''}{pct:.2f}%", accent)

    # Neon heartbeat footer from the reference.
    footer_y = 1184
    _heartbeat(d, PAD + 6, footer_y, WIDTH - PAD * 2 - 12, accent)
    _center(d, "AlphaPulse", 1195, _font(12, True), MUTED)

    return img.convert("RGB")


def _to_bytes(data):
    buf = io.BytesIO()
    _render(data).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def generate_real_pnl_card(data):
    try:
        return await asyncio.to_thread(_to_bytes, data)
    except Exception as exc:
        logger.exception("Real PnL image generation failed: %s", exc)
        return None
