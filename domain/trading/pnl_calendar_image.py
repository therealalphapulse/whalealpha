import asyncio
import calendar as cal_module
import io
import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("AlphaPulse.PnlCalendarImage")

WIDTH = 900
PAD = 40
HEADER_H = 150
CELL_GAP = 8
DOW_ROW_H = 40

BG_TOP = (10, 14, 22)
BG_BOTTOM = (16, 20, 30)
CARD_COLOR = (19, 24, 34)
CARD_BORDER = (40, 47, 61)
CELL_BG = (24, 29, 40)
CELL_BORDER = (38, 44, 56)

TEXT_PRIMARY = (237, 242, 247)
TEXT_SECONDARY = (146, 155, 170)
TEXT_MUTED = (94, 102, 116)

GREEN = (46, 204, 113)
GREEN_BG = (18, 42, 32)
GREEN_BORDER = (35, 92, 68)
RED = (240, 82, 82)
RED_BG = (48, 24, 28)
RED_BORDER = (100, 45, 50)
ACCENT = (94, 158, 255)

_FONT_PATHS = {
    "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
}
_FONT_CACHE: dict = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    try:
        font = ImageFont.truetype(_FONT_PATHS["bold" if bold else "regular"], size)
    except Exception:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _vertical_gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    base = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return base


def _text_centered(draw, cx, y, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def _fmt_amount(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.2f}"


def _draw_calendar(cal: dict, title: str = "Trading Calendar") -> Image.Image:
    year, month = cal["year"], cal["month"]
    days_in_month = cal["days_in_month"]
    daily = cal["daily_pnl"]

    first_weekday_mon0 = cal_module.monthrange(year, month)[0]  # Monday=0
    first_weekday_sun0 = (first_weekday_mon0 + 1) % 7  # convert to Sunday=0
    num_weeks = -(-(first_weekday_sun0 + days_in_month) // 7)  # ceil div

    cell_w = (WIDTH - PAD * 2 - CELL_GAP * 6) / 7
    grid_top = HEADER_H + DOW_ROW_H + PAD
    cell_h = 78
    height = int(grid_top + num_weeks * (cell_h + CELL_GAP) + PAD + 70)

    img = _vertical_gradient(WIDTH, height, BG_TOP, BG_BOTTOM)
    draw = ImageDraw.Draw(img)

    margin = 20
    draw.rounded_rectangle([margin, margin, WIDTH - margin, height - margin],
                            radius=26, fill=CARD_COLOR, outline=CARD_BORDER, width=2)

    # Header
    draw.text((PAD, 40), "AlphaPulse", font=_font(26, bold=True), fill=TEXT_PRIMARY)
    brand_w = draw.textbbox((0, 0), "AlphaPulse ", font=_font(26, bold=True))[2]
    draw.text((PAD + brand_w, 47), title.upper(), font=_font(13, bold=True), fill=ACCENT)

    month_label = f"{cal_module.month_name[month]} {year}"
    _text_centered(draw, WIDTH / 2, 80, month_label, _font(32, bold=True), TEXT_PRIMARY)

    total = cal.get("total_pnl", 0.0)
    total_color = GREEN if total >= 0 else RED
    summary = f"{_fmt_amount(total)}  •  {cal.get('green_days', 0)} green / {cal.get('red_days', 0)} red days"
    _text_centered(draw, WIDTH / 2, 118, summary, _font(16, bold=True), total_color)

    draw.line([(PAD, HEADER_H), (WIDTH - PAD, HEADER_H)], fill=CARD_BORDER, width=2)

    # Day-of-week row
    dow_labels = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    for i, label in enumerate(dow_labels):
        x = PAD + i * (cell_w + CELL_GAP)
        _text_centered(draw, x + cell_w / 2, HEADER_H + 12, label, _font(13, bold=True), TEXT_MUTED)

    # Day cells
    day = 1
    for week in range(num_weeks):
        for dow in range(7):
            idx = week * 7 + dow
            if idx < first_weekday_sun0 or day > days_in_month:
                continue

            x = PAD + dow * (cell_w + CELL_GAP)
            y = grid_top + week * (cell_h + CELL_GAP)

            pnl = daily.get(day)
            if pnl is None:
                bg, border = CELL_BG, CELL_BORDER
            elif pnl >= 0:
                bg, border = GREEN_BG, GREEN_BORDER
            else:
                bg, border = RED_BG, RED_BORDER

            draw.rounded_rectangle([x, y, x + cell_w, y + cell_h], radius=12, fill=bg, outline=border, width=2)
            draw.text((x + 10, y + 8), str(day), font=_font(15, bold=True), fill=TEXT_SECONDARY)

            if pnl is not None:
                amt_color = GREEN if pnl >= 0 else RED
                amt_text = _fmt_amount(pnl)
                afont = _font(14, bold=True)
                abbox = draw.textbbox((0, 0), amt_text, font=afont)
                aw = abbox[2] - abbox[0]
                aw_max = cell_w - 16
                if aw > aw_max:
                    afont = _font(12, bold=True)
                draw.text((x + 10, y + cell_h - 28), amt_text, font=afont, fill=amt_color)

            day += 1

    footer_y = height - 50
    draw.line([(PAD, footer_y), (WIDTH - PAD, footer_y)], fill=CARD_BORDER, width=2)
    draw.text((PAD, footer_y + 16), "Simulated results \u2022 Not financial advice", font=_font(12), fill=TEXT_MUTED)
    footer_text = "AlphaPulse Bot"
    ffont = _font(12, bold=True)
    fw = draw.textbbox((0, 0), footer_text, font=ffont)[2]
    draw.text((WIDTH - PAD - fw, footer_y + 16), footer_text, font=ffont, fill=TEXT_MUTED)

    return img


def _render_to_bytes(cal: dict, title: str) -> bytes:
    img = _draw_calendar(cal, title=title)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()


async def generate_calendar_image(cal: dict, title: str = "Trading Calendar") -> bytes | None:
    """
    Renders the redesigned PnL Calendar as a branded PNG card (dark theme,
    rounded day cells colored green/red by realized PnL — matching the
    reference calendar's look while carrying AlphaPulse branding).
    `cal` is the dict returned by services.paper_engine.get_pnl_calendar.
    Returns PNG bytes, or None on failure (fails gracefully; caller should
    fall back to the existing text/button calendar view).
    """
    try:
        return await asyncio.to_thread(_render_to_bytes, cal, title)
    except Exception as e:
        logger.error(f"PnL calendar image generation failed: {e}")
        return None
