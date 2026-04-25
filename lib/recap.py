"""Shareable weekly recap PNG cards (F-12).

Two layers:

1. ``compute_weekly_stats(...)`` — pure aggregation over recent meals,
   weight history, and the streak row. Pure dict-in / dict-out, easy to test.
2. ``render_recap_png(stats)``    — Pillow-rendered 1080×1350 (Instagram
   Story) PNG. Returns raw PNG bytes ready for Telegram ``sendPhoto``.

The renderer doesn't ship a TTF — it falls back to ``ImageFont.load_default
(size=N)`` (Pillow 10.1+) when no system font is available. The result is
a clean monospaced look on Vercel's runtime; we can layer in a custom
font later without changing callers.
"""
from __future__ import annotations

import io
import os
from collections import Counter
from datetime import date, timedelta
from typing import Optional

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from PIL import Image, ImageDraw, ImageFont


# Bot URL embedded in the recap card's QR code.
_BOT_URL = "https://t.me/kuswise_bot"
# QR target size on the canvas (square). Width includes a few px of white
# padding so the code scans cleanly against the dark background.
_QR_PX = 280


# Bundled font (Apache-2.0, ships in the repo at assets/fonts/). Noto Sans
# has full Cyrillic coverage so labels like "СЕРІЯ" / "ЗМІНА ВАГИ" actually
# render glyphs instead of tofu boxes on Vercel's bare runtime.
_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "fonts",
)
_BUNDLED_BOLD    = os.path.join(_FONT_DIR, "NotoSans-Bold.ttf")
_BUNDLED_REGULAR = os.path.join(_FONT_DIR, "NotoSans-Regular.ttf")


# Canvas — Instagram Story format (9:16, fits Telegram preview without crop).
W, H = 1080, 1350

# Theme — KusWise brand palette. Dark navy → vibrant orange accent.
_BG_TOP    = (24, 28, 40)
_BG_BOTTOM = (15, 18, 28)
_FG        = (240, 240, 245)
_DIM       = (160, 165, 180)
_ACCENT    = (255, 138, 30)


def _make_qr_image(url: str, target_px: int = _QR_PX) -> Image.Image:
    """Render a QR code as a square PIL Image with white padding so it
    scans reliably against the dark recap canvas.

    ERROR_CORRECT_M (~15% damage tolerance) is enough headroom for the
    PNG to survive Telegram's image re-compression on share.
    """
    qr = qrcode.QRCode(
        version=None,                 # auto-pick the smallest version that fits
        error_correction=ERROR_CORRECT_M,
        box_size=10,                  # final size scaled by .resize() below
        border=2,                     # white quiet zone, in modules
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    # Square resize. NEAREST keeps the modules crisp (no anti-alias smearing).
    return img.resize((target_px, target_px), Image.NEAREST)


def _try_truetype(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return a font of the requested pixel size with Cyrillic coverage.

    Tries: bundled Noto Sans (always present in the repo) → common system
    fonts → Pillow's scaled default. The bundled Noto path is the only
    one that's reliable on Vercel's runtime, so it goes first.
    """
    candidates = [
        _BUNDLED_BOLD if bold else _BUNDLED_REGULAR,
        # Defensive fallbacks — almost certainly absent on Vercel but free.
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # macOS dev box:
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def compute_weekly_stats(
    meals_last_7d: list[dict],
    weight_history_recent: list[dict],
    streak_row: Optional[dict],
    end_date: Optional[date] = None,
) -> dict:
    """Aggregate one user's last-7-days into a render-ready dict.

    Inputs are intentionally already-fetched lists (no DB conn) so this
    function is trivially testable.

    ``meals_last_7d`` items expect at least: ``date`` (YYYY-MM-DD),
    ``description``, ``calories`` (float). Extra fields are ignored.

    ``weight_history_recent`` items expect: ``weight_kg`` (float),
    ``recorded_at`` (datetime or YYYY-MM-DD string). Newest-first or
    oldest-first — both work, we sort.

    Returns:
        {
          "end_date":      "2026-04-25",
          "days_logged":   5,
          "avg_kcal":      1820,
          "streak":        12,
          "top_food":      "Курка з рисом",
          "top_food_count": 4,
          "weight_delta":  -0.6,    # kg, None if < 2 weight points
        }
    """
    if end_date is None:
        end_date = date.today()
    start_date = end_date - timedelta(days=6)

    # --- Days logged, average kcal, and macro totals (for distribution) ---
    days_with_meals: set[str] = set()
    cal_per_day: dict[str, float] = {}
    total_protein_g = 0.0
    total_carbs_g   = 0.0
    total_fat_g     = 0.0
    for m in meals_last_7d or []:
        d = (m.get("date") or "")[:10]
        if not d:
            continue
        # Stay within the 7-day window even if the caller over-fetched.
        try:
            d_obj = date.fromisoformat(d)
        except ValueError:
            continue
        if d_obj < start_date or d_obj > end_date:
            continue
        days_with_meals.add(d)
        cal_per_day[d] = cal_per_day.get(d, 0.0) + float(m.get("calories") or 0)
        try:
            total_protein_g += float(m.get("protein_g") or 0)
            total_carbs_g   += float(m.get("carbs_g")   or 0)
            total_fat_g     += float(m.get("fat_g")     or 0)
        except (TypeError, ValueError):
            continue

    days_logged = len(days_with_meals)
    avg_kcal = round(sum(cal_per_day.values()) / days_logged) if days_logged else 0

    # --- Macro distribution as % of total kcal (4/4/9) ---
    p_kcal = total_protein_g * 4
    c_kcal = total_carbs_g   * 4
    f_kcal = total_fat_g     * 9
    macro_kcal = p_kcal + c_kcal + f_kcal
    if macro_kcal > 0:
        protein_pct = round(p_kcal / macro_kcal * 100)
        carbs_pct   = round(c_kcal / macro_kcal * 100)
        # Force the three values to sum to exactly 100 (round-trip safety).
        fat_pct = 100 - protein_pct - carbs_pct
    else:
        protein_pct = carbs_pct = fat_pct = None

    # --- Weight delta over the window (newest in window − oldest in window) ---
    def _as_date(row) -> Optional[date]:
        ts = row.get("recorded_at") or row.get("created_at")
        if ts is None:
            return None
        if hasattr(ts, "date"):
            try: return ts.date()
            except Exception: return None
        if isinstance(ts, date):
            return ts
        if isinstance(ts, str):
            try: return date.fromisoformat(ts[:10])
            except ValueError: return None
        return None

    in_window = []
    for r in weight_history_recent or []:
        d = _as_date(r)
        if d is None:
            continue
        if d < start_date or d > end_date:
            continue
        if r.get("weight_kg") is None:
            continue
        in_window.append((d, float(r["weight_kg"])))
    weight_delta: Optional[float] = None
    if len(in_window) >= 2:
        in_window.sort(key=lambda t: t[0])
        weight_delta = round(in_window[-1][1] - in_window[0][1], 2)

    return {
        "end_date":       end_date.isoformat(),
        "days_logged":    days_logged,
        "avg_kcal":       int(avg_kcal),
        "streak":         int((streak_row or {}).get("current_streak") or 0),
        "weight_delta":   weight_delta,
        "protein_pct":    protein_pct,  # None when no logged meals
        "carbs_pct":      carbs_pct,
        "fat_pct":        fat_pct,
    }


def render_recap_png(stats: dict, first_name: Optional[str] = None) -> bytes:
    """Render the recap card. Returns PNG bytes (no temp file).

    The layout deliberately uses LARGE numbers on a dark canvas so the
    card reads on a phone screen at thumb-distance. Brand watermark sits
    in the bottom-right; subtle, not advertising.
    """
    img = Image.new("RGB", (W, H), _BG_TOP)
    draw = ImageDraw.Draw(img)

    # Vertical gradient — cheap two-stop linear blend.
    for y in range(H):
        t = y / max(1, H - 1)
        r = int(_BG_TOP[0] * (1 - t) + _BG_BOTTOM[0] * t)
        g = int(_BG_TOP[1] * (1 - t) + _BG_BOTTOM[1] * t)
        b = int(_BG_TOP[2] * (1 - t) + _BG_BOTTOM[2] * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Header — brand strip at top
    head_font = _try_truetype(48)
    name_font = _try_truetype(36)
    big_font  = _try_truetype(120)
    label_font = _try_truetype(34)
    sub_font  = _try_truetype(28)
    foot_font = _try_truetype(28)

    pad = 60
    y = pad

    # Brand
    draw.text((pad, y), "KusWise", font=head_font, fill=_ACCENT)
    y += 70

    # User line
    if first_name:
        draw.text((pad, y), f"Тиждень — {first_name}", font=name_font, fill=_DIM)
    else:
        draw.text((pad, y), "Тиждень", font=name_font, fill=_DIM)
    y += 90

    # Note: bundled fallback fonts don't include emoji glyphs, so labels
    # are emoji-free here. The big numbers carry the visual hierarchy.
    #
    # Vertical budget on a 1080×1350 canvas:
    #   header (~220) + 4 stat blocks (~190 each) + bottom QR/footer (~330)
    # = 1310. Section spacing kept tight to leave room for the QR tile.

    big_font_mid = _try_truetype(96)   # slightly smaller "big" so 4 stats fit
    macro_font   = _try_truetype(60)   # macro line is one row, can be modest
    section_dy   = 190

    # Streak
    streak = stats.get("streak", 0)
    streak_label = (
        "1 день поспіль" if streak == 1
        else f"{streak} дні поспіль" if 2 <= streak <= 4
        else f"{streak} днів поспіль"
    )
    draw.text((pad, y), "СЕРІЯ", font=label_font, fill=_ACCENT)
    draw.text((pad, y + 36), str(streak), font=big_font_mid, fill=_FG)
    draw.text((pad, y + 140), streak_label, font=sub_font, fill=_DIM)
    y += section_dy

    # Avg kcal
    draw.text((pad, y), "СЕРЕДНЬО ККАЛ/ДЕНЬ", font=label_font, fill=_ACCENT)
    draw.text((pad, y + 36), f"{stats.get('avg_kcal', 0):,}".replace(",", " "),
              font=big_font_mid, fill=_FG)
    days = stats.get("days_logged", 0)
    days_label = (
        "за 1 залогований день" if days == 1
        else f"за {days} залоговані дні" if 2 <= days <= 4
        else f"за {days} залогованих днів" if days else "ще нічого не логували"
    )
    draw.text((pad, y + 140), days_label, font=sub_font, fill=_DIM)
    y += section_dy

    # Weight delta
    delta = stats.get("weight_delta")
    if delta is not None:
        sign = "+" if delta > 0 else "-" if delta < 0 else "~"
        delta_str = f"{sign}{abs(delta):.1f} кг"
        draw.text((pad, y), "ЗМІНА ВАГИ", font=label_font, fill=_ACCENT)
        draw.text((pad, y + 36), delta_str, font=big_font_mid, fill=_FG)
        y += section_dy

    # Macro distribution — % of total kcal (4/4/9). Shown only when there's
    # actually macro data, otherwise we just skip and leave whitespace for
    # the QR tile below.
    p_pct = stats.get("protein_pct")
    c_pct = stats.get("carbs_pct")
    f_pct = stats.get("fat_pct")
    if p_pct is not None and c_pct is not None and f_pct is not None:
        draw.text((pad, y), "СПІВВІДНОШЕННЯ МАКРО", font=label_font, fill=_ACCENT)
        macros_str = f"Б {p_pct}%  В {c_pct}%  Ж {f_pct}%"
        draw.text((pad, y + 36), macros_str, font=macro_font, fill=_FG)
        draw.text((pad, y + 110), "за весь логований тиждень", font=sub_font, fill=_DIM)

    # ----- Bottom row: QR (right) + footer text (left) -----
    qr_size = _QR_PX
    # QR rounded-corner backdrop: white tile with 14px padding on each side.
    qr_pad = 14
    tile_size = qr_size + qr_pad * 2
    tile_x = W - pad - tile_size
    tile_y = H - pad - tile_size
    # White rounded tile so the QR has high contrast even when Telegram
    # crunches the JPEG quality on share.
    draw.rounded_rectangle(
        [(tile_x, tile_y), (tile_x + tile_size, tile_y + tile_size)],
        radius=20, fill="white",
    )
    try:
        qr_img = _make_qr_image(_BOT_URL, target_px=qr_size)
        img.paste(qr_img, (tile_x + qr_pad, tile_y + qr_pad))
    except Exception:
        # Renderer must never crash on QR failure — drop it silently and
        # the white tile becomes a small blank square. Worst case is ugly,
        # not broken.
        pass

    # "Скан → @kuswise_bot" line right under the QR tile in white.
    qr_caption = "Скан → @kuswise_bot"
    cap_y = tile_y + tile_size + 8
    cap_bbox = draw.textbbox((0, 0), qr_caption, font=sub_font)
    cap_w = cap_bbox[2] - cap_bbox[0]
    cap_x = tile_x + (tile_size - cap_w) // 2
    if cap_y + 30 < H - pad:  # only show caption if it fits without overlap
        draw.text((cap_x, cap_y), qr_caption, font=sub_font, fill=_DIM)

    # Footer watermark on the left, vertically centered against the QR tile.
    foot_text = "бот який знає кожний кусь"
    foot_y = tile_y + tile_size // 2 - 16
    draw.text((pad, foot_y), foot_text, font=foot_font, fill=_DIM)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
