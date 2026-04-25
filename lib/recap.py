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

from PIL import Image, ImageDraw, ImageFont


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

    # --- Days logged + average kcal ---
    days_with_meals: set[str] = set()
    cal_per_day: dict[str, float] = {}
    food_counter: Counter[str] = Counter()
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
        desc = (m.get("description") or "").strip()
        if desc:
            # Light normalization so "Курка з рисом" and "курка з рисом " group.
            food_counter[desc.lower()[:60]] += 1

    days_logged = len(days_with_meals)
    avg_kcal = round(sum(cal_per_day.values()) / days_logged) if days_logged else 0

    # --- Top food: most-frequent description, tie-break by alphabetic ---
    top_food = None
    top_food_count = 0
    if food_counter:
        winners = food_counter.most_common(1)
        top_food = winners[0][0]
        top_food_count = winners[0][1]
        # Restore display capitalization from the most recent matching meal.
        for m in meals_last_7d or []:
            d = (m.get("description") or "").strip()
            if d.lower()[:60] == top_food:
                top_food = d
                break

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
        "top_food":       top_food,
        "top_food_count": int(top_food_count),
        "weight_delta":   weight_delta,
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

    # ----- Big stats -----
    # Streak
    streak = stats.get("streak", 0)
    streak_label = (
        "1 день поспіль" if streak == 1
        else f"{streak} дні поспіль" if 2 <= streak <= 4
        else f"{streak} днів поспіль"
    )
    draw.text((pad, y), "СЕРІЯ", font=label_font, fill=_ACCENT)
    draw.text((pad, y + 40), str(streak), font=big_font, fill=_FG)
    draw.text((pad, y + 170), streak_label, font=sub_font, fill=_DIM)
    y += 230

    # Avg kcal
    draw.text((pad, y), "СЕРЕДНЬО ККАЛ/ДЕНЬ", font=label_font, fill=_ACCENT)
    draw.text((pad, y + 40), f"{stats.get('avg_kcal', 0):,}".replace(",", " "),
              font=big_font, fill=_FG)
    days = stats.get("days_logged", 0)
    days_label = (
        "за 1 залогований день" if days == 1
        else f"за {days} залоговані дні" if 2 <= days <= 4
        else f"за {days} залогованих днів" if days else "ще нічого не логували"
    )
    draw.text((pad, y + 170), days_label, font=sub_font, fill=_DIM)
    y += 230

    # Weight delta
    delta = stats.get("weight_delta")
    if delta is not None:
        # Use ASCII "-" rather than the typographic U+2212 MINUS so all
        # bundled fonts can render it. Same for "±" we just write "≈".
        sign = "+" if delta > 0 else "-" if delta < 0 else "~"
        delta_str = f"{sign}{abs(delta):.1f} кг"
        draw.text((pad, y), "ЗМІНА ВАГИ", font=label_font, fill=_ACCENT)
        draw.text((pad, y + 40), delta_str, font=big_font, fill=_FG)
        y += 200

    # Top food
    top = stats.get("top_food")
    if top:
        draw.text((pad, y), "ЧАСТО ЇЛИ", font=label_font, fill=_ACCENT)
        # Crop long dish names so they fit on one line.
        display = top if len(top) <= 26 else top[:25] + "…"
        draw.text((pad, y + 40), display, font=name_font, fill=_FG)
        cnt = stats.get("top_food_count", 0)
        if cnt:
            cnt_label = "1 раз" if cnt == 1 else f"{cnt} рази" if 2 <= cnt <= 4 else f"{cnt} разів"
            draw.text((pad, y + 90), cnt_label, font=sub_font, fill=_DIM)

    # Footer watermark
    foot_text = "@kuswise_bot · твій трекер їжі"
    draw.text((pad, H - pad - 30), foot_text, font=foot_font, fill=_DIM)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
