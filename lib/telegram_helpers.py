"""Direct Telegram Bot API calls via httpx (no aiogram — serverless-friendly)."""
import os
import re
from urllib.parse import urlsplit

import httpx

from lib.config import TELEGRAM_BOT_TOKEN, VERCEL_URL
from lib.i18n import t as _i18n_t

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

# Allow only DNS-style hostnames (letters, digits, dots, hyphens). Rejects
# anything that could ride in via a malformed VERCEL_URL — userinfo, paths,
# query strings, embedded creds.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(?::\d+)?$")


def _resolve_host() -> str:
    """Parse VERCEL_URL into a clean host:port. Trips on anything weird.

    Accepts both `example.vercel.app` and `https://example.vercel.app/`. Strips
    trailing slashes; rejects userinfo, paths, queries, fragments, or non-HTTPS
    schemes so we can safely interpolate into f"https://{host}/...".
    """
    raw = (VERCEL_URL or "").strip()
    if not raw:
        return ""
    # Add a scheme if missing so urlsplit populates netloc instead of path.
    candidate = raw if "://" in raw else f"https://{raw}"
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ("https", ""):
        return ""
    if parts.username or parts.password or parts.path.strip("/") or parts.query or parts.fragment:
        return ""
    host = parts.netloc.rstrip("/")
    if not host or not _HOST_RE.match(host):
        return ""
    return host


def _build_miniapp_url(path: str, locale: str = "en") -> str:
    """Absolute HTTPS URL for a Mini App page, with ?v= cache-buster + ?lang= locale.

    Vercel auto-injects VERCEL_GIT_COMMIT_SHA. Telegram's iOS WebView keys
    its HTML cache by URL, so a new SHA per deploy forces a cache miss.
    The ?lang= query param lets the Mini App's server-side renderer pick
    UA vs EN labels per user (F-2b Chunk 7).
    """
    host = _resolve_host() or "invalid.invalid"
    base = f"https://{host}{path}"
    sha = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:8]
    qs = []
    if sha:
        qs.append(f"v={sha}")
    qs.append(f"lang={locale}")
    return f"{base}?{'&'.join(qs)}"


def _dashboard_url(locale: str = "en") -> str:
    """Absolute HTTPS URL for the miniapp dashboard."""
    return _build_miniapp_url("/api/dashboard", locale=locale)


def _scan_url(locale: str = "en") -> str:
    """Absolute HTTPS URL for the F-8 barcode scanner Mini App page."""
    return _build_miniapp_url("/api/scan", locale=locale)


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = httpx.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_photo(
    chat_id: int,
    photo_bytes: bytes,
    caption: str | None = None,
    filename: str = "photo.png",
    reply_markup: dict | None = None,
) -> dict:
    """Telegram sendPhoto via multipart upload (used by F-12 recap cards).

    ``photo_bytes`` is the raw PNG/JPEG payload. We upload as a multipart
    form so we never need to round-trip through Telegram's file_id cache.
    """
    files = {
        "photo": (filename, photo_bytes, "image/png"),
    }
    data: dict = {"chat_id": str(chat_id), "parse_mode": "HTML"}
    if caption is not None:
        data["caption"] = caption[:1024]  # Telegram caption hard limit
    if reply_markup is not None:
        import json as _json
        data["reply_markup"] = _json.dumps(reply_markup)
    try:
        resp = httpx.post(f"{BASE_URL}/sendPhoto", data=data, files=files, timeout=20)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def answer_callback_query(callback_query_id: str, text: str | None = None) -> dict:
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = httpx.post(f"{BASE_URL}/answerCallbackQuery", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def edit_message_text(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> dict:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = httpx.post(f"{BASE_URL}/editMessageText", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_chat_action(chat_id: int, action: str = "typing") -> dict:
    """Show a transient chat indicator (e.g. 'typing…') — expires after ~5 s."""
    try:
        resp = httpx.post(
            f"{BASE_URL}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict) -> dict:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": reply_markup,
    }
    try:
        resp = httpx.post(f"{BASE_URL}/editMessageReplyMarkup", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_file_bytes(file_id: str) -> bytes:
    """Fetch the binary contents of a Telegram-hosted file."""
    meta = httpx.get(f"{BASE_URL}/getFile", params={"file_id": file_id}, timeout=10).json()
    if not meta.get("ok"):
        raise RuntimeError(f"getFile failed: {meta}")
    file_path = meta["result"]["file_path"]
    resp = httpx.get(f"{FILE_URL}/{file_path}", timeout=30)
    resp.raise_for_status()
    return resp.content


def nudge_optout_keyboard(locale: str = "en") -> dict:
    """Single button on the inactivity-nudge message — one-tap opt-out."""
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("inline_button.nudge_off", locale=locale),
              "callback_data": "nudge:off"}],
        ]
    }


def meal_type_keyboard(locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": _i18n_t("meal_type.breakfast_btn", locale=locale), "callback_data": "meal_type:breakfast"},
                {"text": _i18n_t("meal_type.lunch_btn",     locale=locale), "callback_data": "meal_type:lunch"},
            ],
            [
                {"text": _i18n_t("meal_type.dinner_btn",    locale=locale), "callback_data": "meal_type:dinner"},
                {"text": _i18n_t("meal_type.snack_btn",     locale=locale), "callback_data": "meal_type:snack"},
            ],
            [
                {"text": _i18n_t("inline_button.cancel",    locale=locale), "callback_data": "meal_type:cancel"},
            ],
        ]
    }


def moderation_keyboard(locale: str = "en") -> dict:
    """Preview keyboard shown after meal analysis, before save.

    2026-05: added ⭐ Save as favorite as a one-tap "accept + favorite"
    shortcut. Previously the user had to accept the meal, then tap a
    separate ⭐ button on the confirmation message — now that the
    confirmation no longer carries inline buttons (consolidated into
    a 2-message flow), pre-marking favorites must happen here.
    """
    return {
        "inline_keyboard": [
            [
                {"text": _i18n_t("inline_button.accept",       locale=locale), "callback_data": "mod:accept"},
                {"text": _i18n_t("inline_button.recalc",       locale=locale), "callback_data": "mod:recalc"},
            ],
            [
                {"text": _i18n_t("inline_button.fav_add",      locale=locale), "callback_data": "mod:accept_fav"},
            ],
            [
                {"text": _i18n_t("inline_button.manual_entry", locale=locale), "callback_data": "mod:manual"},
            ],
            [
                {"text": _i18n_t("inline_button.cancel",       locale=locale), "callback_data": "mod:cancel"},
            ],
        ]
    }


def cancel_only_keyboard(locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("inline_button.cancel", locale=locale), "callback_data": "mod:cancel"}],
        ]
    }


def suggest_followup_keyboard(locale: str = "en") -> dict:
    """F-11: shown under each /suggest_meal result.

    Lets the user re-run with their own ingredients ("fridge mode"),
    request a variation, or ⭐ save the recipe to their personal library
    (browsable via /recipes).
    """
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("suggest.from_my_pantry",   locale=locale), "callback_data": "suggest:fridge"}],
            [{"text": _i18n_t("suggest.different_version", locale=locale), "callback_data": "suggest:variation"}],
            [{"text": _i18n_t("suggest.save_recipe",      locale=locale), "callback_data": "suggest:save"}],
        ]
    }


def ai_menu_keyboard(locale: str = "en") -> dict:
    """Combined AI-helper chooser. Opened by tapping the merged
    Ask-AI reply button or by typing /ai. Each branch routes to an
    existing handler; no new flows."""
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("ai_menu.choice_ask",     locale=locale), "callback_data": "ai:ask"}],
            [{"text": _i18n_t("ai_menu.choice_suggest", locale=locale), "callback_data": "ai:suggest"}],
            [{"text": _i18n_t("ai_menu.choice_fridge",  locale=locale), "callback_data": "ai:fridge"}],
            [{"text": _i18n_t("ai_menu.choice_cancel",  locale=locale), "callback_data": "ai:cancel"}],
        ]
    }


def plan_pantry_keyboard(locale: str = "en") -> dict:
    """F-10: shown after /plan — let user proceed without typing pantry items."""
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("plan_btn.no_pantry",   locale=locale), "callback_data": "plan:nopantry"}],
            [{"text": _i18n_t("inline_button.cancel", locale=locale), "callback_data": "plan:cancel"}],
        ]
    }


def plan_day_keyboard(plan_id: int, day_idx: int, day: dict, locale: str = "en") -> dict:
    """F-10: per-day inline buttons — one Log button per slot + day navigator.

    Slot buttons fit Telegram's ~64-char limit by trimming the dish name
    and appending kcal. Callback data: ``plan:log:<plan_id>:<day_idx>:<slot>``.
    """
    kcal_unit = _i18n_t("macro.calories_short", locale=locale)
    emoji = {"breakfast": "🥣", "lunch": "🍱", "dinner": "🍽️", "snack": "🍎"}
    rows = []
    for slot_key in ("breakfast", "lunch", "dinner", "snack"):
        slot = day["slots"].get(slot_key)
        if not slot:
            continue
        kcal = int(round(float(slot.get("calories") or 0)))
        name = (slot.get("name") or "")[:28]
        rows.append([{
            "text": _i18n_t("plan_btn.log_entry", locale=locale, emoji=emoji[slot_key], name=name, kcal=kcal, kcal_unit=kcal_unit),
            "callback_data": f"plan:log:{plan_id}:{day_idx}:{slot_key}",
        }])
    nav_row = []
    if day_idx > 0:
        nav_row.append({"text": _i18n_t("plan_btn.day_back", locale=locale, n=day_idx),
                        "callback_data": f"plan:view:{plan_id}:{day_idx - 1}"})
    if day_idx < 2:
        nav_row.append({"text": _i18n_t("plan_btn.day_forward", locale=locale, n=day_idx + 2),
                        "callback_data": f"plan:view:{plan_id}:{day_idx + 1}"})
    if nav_row:
        rows.append(nav_row)
    rows.append([{"text": _i18n_t("inline_button.close", locale=locale), "callback_data": "plan:cancel"}])
    return {"inline_keyboard": rows}


def menu_log_keyboard(dishes: list[dict], locale: str = "en") -> dict:
    """F-9: one button per OCR'd dish that triggers the standard log flow.

    Telegram inline-button labels are limited to ~64 chars. We trim each
    name and append the kcal so the user can compare at a glance.
    """
    kcal_unit = _i18n_t("macro.calories_short", locale=locale)
    rows = []
    for i, d in enumerate(dishes[:25]):
        name = (d.get("name") or "")[:32]
        kcal = int(round(float(d.get("calories") or 0)))
        rows.append([{
            "text": _i18n_t("menu_btn.log_entry", locale=locale, name=name, kcal=kcal, kcal_unit=kcal_unit),
            "callback_data": f"menu:log:{i}",
        }])
    rows.append([{"text": _i18n_t("inline_button.close_menu", locale=locale), "callback_data": "menu:cancel"}])
    return {"inline_keyboard": rows}


def scanner_inline_keyboard(locale: str = "en") -> dict:
    """F-8: launch buttons for the barcode scanner.

    Telegram requires the ``inline_keyboard`` (NOT reply ``keyboard``) form
    of ``web_app`` to deliver signed ``initData`` to our scanner page.

    The manual-entry button is a **non-Mini-App fallback** for devices /
    permission setups where the camera path fails (older iOS Telegram,
    denied camera permission, browsers that block ``getUserMedia`` in
    third-party WebViews, etc.) — it just sets an FSM flag and prompts
    the user to type the digits.
    """
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("inline_button.open_scanner",     locale=locale), "web_app": {"url": _scan_url(locale=locale)}}],
            [{"text": _i18n_t("inline_button.scan_manual_entry", locale=locale), "callback_data": "barcode:manual"}],
            [{"text": _i18n_t("inline_button.scan_menu_ocr",     locale=locale), "callback_data": "barcode:menu_ocr"}],
            [{"text": _i18n_t("inline_button.cancel",            locale=locale), "callback_data": "barcode:cancel"}],
        ]
    }


def alternates_keyboard(candidates: list[dict], locale: str = "en") -> dict:
    """F-6: 1-3 numbered candidate buttons + manual + cancel.

    Each button label fits Telegram's ~64-char inline-button limit; we trim
    long candidate names. Callback data is ``pick:0/1/2``.
    """
    digits = ("1⃣", "2⃣", "3⃣")  # 1️⃣ 2️⃣ 3️⃣
    kcal_unit = _i18n_t("macro.calories_short", locale=locale)
    rows = []
    for i, cand in enumerate(candidates[:3]):
        name = (cand.get("name") or "")[:32]
        kcal = int(round(float(cand.get("calories") or 0)))
        rows.append([{
            "text": _i18n_t("alternates.candidate", locale=locale, digit=digits[i], name=name, kcal=kcal, kcal_unit=kcal_unit),
            "callback_data": f"pick:{i}",
        }])
    rows.append([{"text": _i18n_t("inline_button.manual_entry", locale=locale), "callback_data": "mod:manual"}])
    rows.append([{"text": _i18n_t("inline_button.cancel",       locale=locale), "callback_data": "mod:cancel"}])
    return {"inline_keyboard": rows}


def meals_list_keyboard(meals: list[dict], locale: str = "en") -> dict:
    """Build inline keyboard with Delete/Edit buttons for each meal."""
    rows = []
    for i, m in enumerate(meals, 1):
        meal_id = m["id"]
        rows.append([
            {"text": _i18n_t("inline_button.delete_n", locale=locale, n=i), "callback_data": f"meal_del:{meal_id}"},
            {"text": _i18n_t("inline_button.edit_n",   locale=locale, n=i), "callback_data": f"meal_edit:{meal_id}"},
        ])
    return {"inline_keyboard": rows}


def main_menu_keyboard(locale: str = "en") -> dict:
    """Persistent reply keyboard shown below the input field.

    All buttons are plain-text — tapping sends the label as a message and
    webhook.py routes it.

    Note: 🔢 Barcode (UA: Штрих Код) and 📋 Scan menu (UA: Скануй Меню) are  # noqa: i18n
    intentionally NOT on this keyboard. Their commands (``/scan`` and
    ``/menu``) still work, and the button labels remain in
    ``menu_button_labels()`` + the webhook dispatcher so users with a stale
    keyboard cached on their phone don't tap into a void. Removed from the
    visible UI to reduce clutter — most users use photo / text / voice for
    meal entry.
    """
    from lib.formatters import btn_label
    return {
        "keyboard": [
            [{"text": btn_label("ask", locale=locale)},     {"text": btn_label("fav", locale=locale)}],
            [{"text": btn_label("water", locale=locale)},   {"text": btn_label("meals", locale=locale)}],
            [{"text": btn_label("profile", locale=locale)}, {"text": btn_label("scanner", locale=locale)}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


# --- Favorites + Recent ---

def _truncate(text: str, n: int = 34) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n - 1] + "…"


def recent_meals_keyboard(meals: list[dict], variant: str = "recent", locale: str = "en") -> dict:
    """Inline keyboard: one row per meal with re-log + (for favorites) unstar button."""
    kcal_unit = _i18n_t("macro.calories_short", locale=locale)
    rows = []
    for m in meals:
        mid = m["id"]
        desc = _truncate(m.get("description") or "—", 28)
        cal = round(m.get("calories") or 0)
        label = _i18n_t("recent_meals.entry", locale=locale, desc=desc, cal=cal, kcal_unit=kcal_unit)
        row = [{"text": label, "callback_data": f"relog:{mid}"}]
        if variant == "fav":
            row.append({"text": "✖", "callback_data": f"fav:{mid}:0"})
        rows.append(row)
    if not rows:
        rows.append([{"text": "—", "callback_data": "noop"}])
    return {"inline_keyboard": rows}


def meal_logged_actions_keyboard(meal_id: int, is_fav: bool = False, locale: str = "en") -> dict:
    star_key = "inline_button.fav_added" if is_fav else "inline_button.fav_add"
    fav_value = 0 if is_fav else 1
    star = {"text": _i18n_t(star_key, locale=locale), "callback_data": f"fav:{meal_id}:{fav_value}"}
    return {
        "inline_keyboard": [
            [star,
             {"text": _i18n_t("inline_button.edit",   locale=locale), "callback_data": f"meal_edit:{meal_id}"},
             {"text": _i18n_t("inline_button.delete", locale=locale), "callback_data": f"meal_del:{meal_id}"}],
        ]
    }


def undo_relog_keyboard(meal_id: int, locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("inline_button.undo", locale=locale), "callback_data": f"undo:{meal_id}"}],
        ]
    }


# --- Water ---

def water_keyboard(locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "+200", "callback_data": "water:add:200"},
                {"text": "+250", "callback_data": "water:add:250"},
                {"text": "+300", "callback_data": "water:add:300"},
                {"text": "+500", "callback_data": "water:add:500"},
                {"text": "+750", "callback_data": "water:add:750"},
            ],
            [
                {"text": _i18n_t("water_btn.undo_last", locale=locale), "callback_data": "water:undo"},
                {"text": _i18n_t("water_btn.goal",      locale=locale), "callback_data": "water:goal"},
            ],
        ]
    }


def water_goal_keyboard(back_action: str = "water:back", locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": _i18n_t("water_btn.goal_1_5_l", locale=locale), "callback_data": "water:goal:set:1500"},
                {"text": _i18n_t("water_btn.goal_2_0_l", locale=locale), "callback_data": "water:goal:set:2000"},
                {"text": _i18n_t("water_btn.goal_2_5_l", locale=locale), "callback_data": "water:goal:set:2500"},
                {"text": _i18n_t("water_btn.goal_3_0_l", locale=locale), "callback_data": "water:goal:set:3000"},
            ],
            [{"text": _i18n_t("water_btn.custom", locale=locale), "callback_data": "prof:water:custom"}],
            [{"text": _i18n_t("water_btn.back",   locale=locale), "callback_data": back_action}],
        ]
    }


# --- Onboarding keyboards ---

def sex_keyboard(locale: str = "en") -> dict:
    return {
        "inline_keyboard": [[
            {"text": _i18n_t("sex_keyboard.male", locale=locale), "callback_data": "onb:sex:male"},
            {"text": _i18n_t("sex_keyboard.female", locale=locale), "callback_data": "onb:sex:female"},
        ]]
    }


def gym_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "0", "callback_data": "onb:gym:0"},
                {"text": "1–2", "callback_data": "onb:gym:1-2"},
                {"text": "3–4", "callback_data": "onb:gym:3-4"},
            ],
            [
                {"text": "5–6", "callback_data": "onb:gym:5-6"},
                {"text": "7", "callback_data": "onb:gym:7"},
            ],
        ]
    }


def goal_keyboard(locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("goal_keyboard.lose", locale=locale), "callback_data": "onb:goal:lose"}],
            [{"text": _i18n_t("goal_keyboard.maintain", locale=locale), "callback_data": "onb:goal:maintain"}],
            [{"text": _i18n_t("goal_keyboard.gain", locale=locale), "callback_data": "onb:goal:gain"}],
        ]
    }


def confirm_calories_keyboard(locale: str = "en") -> dict:
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("confirm_calories.accept", locale=locale), "callback_data": "onb:cal:accept"}],
            [{"text": _i18n_t("confirm_calories.custom", locale=locale), "callback_data": "onb:cal:custom"}],
        ]
    }


def language_keyboard() -> dict:
    """EN / UK language picker (F-2). Used by ``/language`` command."""
    return {
        "inline_keyboard": [
            [
                {"text": "🇬🇧 English",     "callback_data": "lang:set:en"},
                {"text": "🇺🇦 Українська", "callback_data": "lang:set:uk"},  # noqa: i18n
            ],
        ]
    }


def lang_confirm_keyboard(detected: str) -> dict:
    """F-2b onboarding step zero: confirm auto-detected language or override.

    DEPRECATED 2026-05: no longer attached to fresh-user `/start` — the
    confirmation step was removed because 39% of new users bounced
    there. Kept intact for back-compat with any cached keyboards in
    stuck users' chat history; the same ``onb:lang:`` handler still
    advances them correctly.

    The "primary" button matches what we auto-detected, so one tap
    continues the flow. The other button switches. Callback prefix
    ``onb:lang:`` routes through the existing ``handle_onboarding_callback``
    dispatcher (callback fires before the profile is complete).
    """
    if detected == "uk":
        return {
            "inline_keyboard": [
                [{"text": "✅ Продовжити українською", "callback_data": "onb:lang:uk"}],  # noqa: i18n
                [{"text": "🇬🇧 Switch to English",      "callback_data": "onb:lang:en"}],
            ]
        }
    return {
        "inline_keyboard": [
            [{"text": "✅ Continue in English",     "callback_data": "onb:lang:en"}],
            [{"text": "🇺🇦 Перейти на українську", "callback_data": "onb:lang:uk"}],  # noqa: i18n
        ]
    }


def health_menu_keyboard(locale: str = "en") -> dict:
    """Top-level /health menu (F-1)."""
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("health_menu.allergens",  locale=locale), "callback_data": "h:set:allergens"}],
            [{"text": _i18n_t("health_menu.conditions", locale=locale), "callback_data": "h:set:conditions"}],
            [{"text": _i18n_t("health_menu.clear",      locale=locale), "callback_data": "h:clear"}],
        ]
    }


def tz_keyboard(prefix: str = "tz:set", locale: str = "en") -> dict:
    """Six timezone presets + 'Other' (free-text). The same keyboard is used
    by onboarding (prefix=onb:tz) and by /timezone (prefix=tz:set).
    Keep this list in sync with ``lib.datehelpers.TZ_PRESETS``."""
    return {
        "inline_keyboard": [
            [
                {"text": _i18n_t("tz_keyboard.kyiv", locale=locale),          "callback_data": f"{prefix}:Europe/Kyiv"},
                {"text": _i18n_t("tz_keyboard.london", locale=locale),        "callback_data": f"{prefix}:Europe/London"},
            ],
            [
                {"text": _i18n_t("tz_keyboard.berlin_warsaw", locale=locale), "callback_data": f"{prefix}:Europe/Berlin"},
                {"text": _i18n_t("tz_keyboard.new_york", locale=locale),      "callback_data": f"{prefix}:America/New_York"},
            ],
            [
                {"text": _i18n_t("tz_keyboard.los_angeles", locale=locale),   "callback_data": f"{prefix}:America/Los_Angeles"},
                {"text": _i18n_t("tz_keyboard.dubai", locale=locale),         "callback_data": f"{prefix}:Asia/Dubai"},
            ],
            [
                {"text": _i18n_t("tz_keyboard.other", locale=locale),         "callback_data": f"{prefix}:custom"},
            ],
        ]
    }


def profile_edit_keyboard(locale: str = "en") -> dict:
    """Quick-edit actions shown under the /profile message.

    2026-05 changes (in order):
      * Added a Language row so users discover the switcher without
        having to know the `/language` typed command exists.
      * Replaced the "Weekly Goal" button with "Timezone" — onboarding
        no longer asks for timezone (defaults to Europe/Kyiv) so we
        need a discoverable post-onboarding switch. Weekly delta
        editing is still reachable via the `/goals` command for the
        small audience that uses it.
    """
    return {
        "inline_keyboard": [
            [
                {"text": _i18n_t("profile_edit.weight", locale=locale), "callback_data": "prof:weight"},
                {"text": _i18n_t("profile_edit.goal",   locale=locale), "callback_data": "prof:goal"},
            ],
            [
                {"text": _i18n_t("profile_edit.target_weight", locale=locale), "callback_data": "prof:target_weight"},
                {"text": _i18n_t("profile_edit.timezone",      locale=locale), "callback_data": "prof:timezone"},
            ],
            [
                {"text": _i18n_t("profile_edit.water_goal", locale=locale), "callback_data": "prof:water"},
                {"text": _i18n_t("profile_edit.language",   locale=locale), "callback_data": "prof:lang"},
            ],
            [
                {"text": _i18n_t("profile_edit.restart",    locale=locale), "callback_data": "onb:restart"},
            ],
        ]
    }


def goals_edit_keyboard(has_target: bool, has_delta: bool, locale: str = "en") -> dict:
    """Inline edit buttons shown under the /goals message."""
    target_label = _i18n_t("goals_edit.change_target" if has_target else "goals_edit.set_target", locale=locale)
    delta_label  = _i18n_t("goals_edit.change_pace"   if has_delta  else "goals_edit.set_pace",   locale=locale)
    return {
        "inline_keyboard": [
            [
                {"text": target_label, "callback_data": "prof:target_weight"},
                {"text": delta_label,  "callback_data": "prof:weekly_delta"},
            ],
            [
                {"text": _i18n_t("goals_edit.log_weight", locale=locale), "callback_data": "prof:weight"},
            ],
        ]
    }


def profile_goal_keyboard(locale: str = "en") -> dict:
    """Inline goal picker used by the /profile → Edit goal flow.

    Reuses the goal_keyboard.* keys (lose/maintain/gain) but with a different
    callback prefix (`prof:goal:` vs onboarding's `onb:goal:`).
    """
    return {
        "inline_keyboard": [
            [{"text": _i18n_t("goal_keyboard.lose",     locale=locale), "callback_data": "prof:goal:lose"}],
            [{"text": _i18n_t("goal_keyboard.maintain", locale=locale), "callback_data": "prof:goal:maintain"}],
            [{"text": _i18n_t("goal_keyboard.gain",     locale=locale), "callback_data": "prof:goal:gain"}],
        ]
    }


def dashboard_inline_keyboard(locale: str = "en") -> dict:
    """Inline keyboard with a single web_app button — this launch mode DOES
    provide signed initData, unlike the KeyboardButton.web_app mode.
    """
    return {
        "inline_keyboard": [[
            {"text": _i18n_t("inline_button.open_dashboard", locale=locale), "web_app": {"url": _dashboard_url(locale=locale)}}
        ]]
    }


def set_chat_menu_button(chat_id: int | None = None, locale: str = "en") -> dict:
    """Register a persistent Mini App button as the bot's chat menu button
    (the icon left of the input area). When chat_id is given, applies to that
    specific chat (the global-default setChatMenuButton call sometimes doesn't
    take effect in Telegram, so we call this per-user on /start).
    """
    payload: dict = {
        "menu_button": {
            "type": "web_app",
            "text": "📱 Dashboard",
            "web_app": {"url": _dashboard_url(locale=locale)},
        }
    }
    if chat_id is not None:
        payload["chat_id"] = chat_id
    try:
        resp = httpx.post(f"{BASE_URL}/setChatMenuButton", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_my_commands(
    commands: list[dict],
    language_code: str | None = None,
    scope: dict | None = None,
) -> dict:
    """Register the bot's native command menu (the blue 'Menu' button).

    Pass ``scope={"type": "chat", "chat_id": <id>}`` to pin a per-user
    menu — Telegram's lookup picks chat-scope over language_code, so this
    overrides whatever the user's client-UI language would have served.
    """
    payload: dict = {"commands": commands}
    if language_code:
        payload["language_code"] = language_code
    if scope:
        payload["scope"] = scope
    try:
        resp = httpx.post(f"{BASE_URL}/setMyCommands", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
