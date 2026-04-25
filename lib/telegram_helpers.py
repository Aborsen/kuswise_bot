"""Direct Telegram Bot API calls via httpx (no aiogram — serverless-friendly)."""
import os
import re
from urllib.parse import urlsplit

import httpx

from lib.config import TELEGRAM_BOT_TOKEN, VERCEL_URL

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


def _dashboard_url() -> str:
    """Absolute HTTPS URL for the miniapp dashboard.

    Appends ?v=<short SHA> when running on Vercel (VERCEL_GIT_COMMIT_SHA is
    auto-injected). Telegram's iOS WebView keys its HTML cache by URL, so a
    new SHA per deploy forces a cache miss and the user sees fresh HTML.
    """
    host = _resolve_host()
    if not host:
        # Fall back to a clearly broken-but-safe placeholder rather than
        # producing https:///api/dashboard, which Telegram would reject.
        host = "invalid.invalid"
    base = f"https://{host}/api/dashboard"
    sha = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:8]
    return f"{base}?v={sha}" if sha else base


def _scan_url() -> str:
    """Absolute HTTPS URL for the F-8 barcode scanner Mini App page."""
    host = _resolve_host() or "invalid.invalid"
    base = f"https://{host}/api/scan"
    sha = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:8]
    return f"{base}?v={sha}" if sha else base


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


def meal_type_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🍳 Сніданок", "callback_data": "meal_type:breakfast"},
                {"text": "🥗 Обід", "callback_data": "meal_type:lunch"},
            ],
            [
                {"text": "🍽️ Вечеря", "callback_data": "meal_type:dinner"},
                {"text": "🍎 Перекус", "callback_data": "meal_type:snack"},
            ],
            [
                {"text": "❌ Скасувати", "callback_data": "meal_type:cancel"},
            ],
        ]
    }


def moderation_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Прийняти", "callback_data": "mod:accept"},
                {"text": "🔄 Перерахувати", "callback_data": "mod:recalc"},
            ],
            [
                {"text": "✏️ Ввести вручну", "callback_data": "mod:manual"},
            ],
            [
                {"text": "❌ Скасувати", "callback_data": "mod:cancel"},
            ],
        ]
    }


def cancel_only_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "❌ Скасувати", "callback_data": "mod:cancel"}],
        ]
    }


def suggest_followup_keyboard() -> dict:
    """F-11: shown under each /suggest_meal result.

    Lets the user re-run with their own ingredients ("fridge mode") or
    request a variation. We avoid a generic "swap X→Y" parsing step
    (would require post-processing the model's output) — a "different
    version" hint goes a long way for free.
    """
    return {
        "inline_keyboard": [
            [{"text": "🛒 З моїх продуктів", "callback_data": "suggest:fridge"}],
            [{"text": "🔄 Інша версія",      "callback_data": "suggest:variation"}],
        ]
    }


def plan_pantry_keyboard() -> dict:
    """F-10: shown after /plan — let user proceed without typing pantry items."""
    return {
        "inline_keyboard": [
            [{"text": "🚀 Без списку — згенерувати", "callback_data": "plan:nopantry"}],
            [{"text": "❌ Скасувати",                 "callback_data": "plan:cancel"}],
        ]
    }


def plan_day_keyboard(plan_id: int, day_idx: int, day: dict) -> dict:
    """F-10: per-day inline buttons — one Log button per slot + day navigator.

    Slot buttons fit Telegram's ~64-char limit by trimming the dish name
    and appending kcal. Callback data: ``plan:log:<plan_id>:<day_idx>:<slot>``.
    """
    emoji = {"breakfast": "🥣", "lunch": "🍱", "dinner": "🍽️", "snack": "🍎"}
    rows = []
    for slot_key in ("breakfast", "lunch", "dinner", "snack"):
        slot = day["slots"].get(slot_key)
        if not slot:
            continue
        kcal = int(round(float(slot.get("calories") or 0)))
        name = (slot.get("name") or "")[:28]
        rows.append([{
            "text": f"➕ {emoji[slot_key]} {name} · {kcal} ккал",
            "callback_data": f"plan:log:{plan_id}:{day_idx}:{slot_key}",
        }])
    nav_row = []
    if day_idx > 0:
        nav_row.append({"text": "← День " + str(day_idx),
                        "callback_data": f"plan:view:{plan_id}:{day_idx - 1}"})
    if day_idx < 2:
        nav_row.append({"text": "День " + str(day_idx + 2) + " →",
                        "callback_data": f"plan:view:{plan_id}:{day_idx + 1}"})
    if nav_row:
        rows.append(nav_row)
    rows.append([{"text": "❌ Закрити", "callback_data": "plan:cancel"}])
    return {"inline_keyboard": rows}


def menu_log_keyboard(dishes: list[dict]) -> dict:
    """F-9: one button per OCR'd dish that triggers the standard log flow.

    Telegram inline-button labels are limited to ~64 chars. We trim each
    name and append the kcal so the user can compare at a glance.
    """
    rows = []
    for i, d in enumerate(dishes[:25]):
        name = (d.get("name") or "")[:32]
        kcal = int(round(float(d.get("calories") or 0)))
        rows.append([{
            "text": f"➕ {name} · {kcal} ккал",
            "callback_data": f"menu:log:{i}",
        }])
    rows.append([{"text": "❌ Закрити меню", "callback_data": "menu:cancel"}])
    return {"inline_keyboard": rows}


def scanner_inline_keyboard() -> dict:
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
            [{"text": "📷 Відкрити сканер", "web_app": {"url": _scan_url()}}],
            [{"text": "✏️ Ввести цифрами", "callback_data": "barcode:manual"}],
            [{"text": "❌ Скасувати",       "callback_data": "barcode:cancel"}],
        ]
    }


def alternates_keyboard(candidates: list[dict]) -> dict:
    """F-6: 1-3 numbered candidate buttons + manual + cancel.

    Each button label fits Telegram's ~64-char inline-button limit; we trim
    long candidate names. Callback data is ``pick:0/1/2``.
    """
    digits = ("1⃣", "2⃣", "3⃣")  # 1️⃣ 2️⃣ 3️⃣
    rows = []
    for i, cand in enumerate(candidates[:3]):
        name = (cand.get("name") or "")[:32]
        kcal = int(round(float(cand.get("calories") or 0)))
        rows.append([{
            "text": f"{digits[i]} {name} ({kcal} ккал)",
            "callback_data": f"pick:{i}",
        }])
    rows.append([{"text": "✏️ Ввести вручну", "callback_data": "mod:manual"}])
    rows.append([{"text": "❌ Скасувати",     "callback_data": "mod:cancel"}])
    return {"inline_keyboard": rows}


def meals_list_keyboard(meals: list[dict]) -> dict:
    """Build inline keyboard with Delete/Edit buttons for each meal."""
    rows = []
    for i, m in enumerate(meals, 1):
        meal_id = m["id"]
        rows.append([
            {"text": f"🗑 Видалити {i}", "callback_data": f"meal_del:{meal_id}"},
            {"text": f"✏️ Змінити {i}", "callback_data": f"meal_edit:{meal_id}"},
        ])
    return {"inline_keyboard": rows}


def main_menu_keyboard() -> dict:
    """Persistent reply keyboard shown below the input field.

    All buttons are plain-text — tapping sends the label as a message and
    webhook.py routes it.

    Note: 🔢 Штрих Код and 📋 Скануй Меню are intentionally NOT on this
    keyboard. Their commands (``/scan`` and ``/menu``) still work, and the
    button labels remain in ``MENU_BUTTON_LABELS`` + the webhook dispatcher
    so users with a stale keyboard cached on their phone don't tap into a
    void. Removed from the visible UI to reduce clutter — most users use
    photo / text / voice for meal entry.
    """
    from lib.formatters import (
        BTN_ASK, BTN_FAV, BTN_WATER, BTN_MEALS, BTN_SUGGEST, BTN_PROFILE,
    )
    return {
        "keyboard": [
            [{"text": BTN_ASK},     {"text": BTN_FAV}],
            [{"text": BTN_WATER},   {"text": BTN_MEALS}],
            [{"text": BTN_SUGGEST}, {"text": BTN_PROFILE}],
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


def recent_meals_keyboard(meals: list[dict], variant: str = "recent") -> dict:
    """Inline keyboard: one row per meal with re-log + (for favorites) unstar button."""
    rows = []
    for m in meals:
        mid = m["id"]
        desc = _truncate(m.get("description") or "—", 28)
        cal = round(m.get("calories") or 0)
        label = f"🔁 {desc} · {cal} ккал"
        row = [{"text": label, "callback_data": f"relog:{mid}"}]
        if variant == "fav":
            row.append({"text": "✖", "callback_data": f"fav:{mid}:0"})
        rows.append(row)
    if not rows:
        rows.append([{"text": "—", "callback_data": "noop"}])
    return {"inline_keyboard": rows}


def meal_logged_actions_keyboard(meal_id: int, is_fav: bool = False) -> dict:
    star = {"text": "✅ В улюблених", "callback_data": f"fav:{meal_id}:0"} if is_fav \
        else {"text": "⭐ В улюблені", "callback_data": f"fav:{meal_id}:1"}
    return {
        "inline_keyboard": [
            [star,
             {"text": "✏️ Виправити", "callback_data": f"meal_edit:{meal_id}"},
             {"text": "🗑 Скасувати", "callback_data": f"meal_del:{meal_id}"}],
        ]
    }


def undo_relog_keyboard(meal_id: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "↩️ Скасувати", "callback_data": f"undo:{meal_id}"}],
        ]
    }


# --- Water ---

def water_keyboard() -> dict:
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
                {"text": "↩️ Відкотити останнє", "callback_data": "water:undo"},
                {"text": "🎯 Ціль", "callback_data": "water:goal"},
            ],
        ]
    }


def water_goal_keyboard(back_action: str = "water:back") -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "1.5 л", "callback_data": "water:goal:set:1500"},
                {"text": "2.0 л", "callback_data": "water:goal:set:2000"},
                {"text": "2.5 л", "callback_data": "water:goal:set:2500"},
                {"text": "3.0 л", "callback_data": "water:goal:set:3000"},
            ],
            [{"text": "✏️ Своє значення", "callback_data": "prof:water:custom"}],
            [{"text": "⬅️ Назад", "callback_data": back_action}],
        ]
    }


# --- Onboarding keyboards ---

def sex_keyboard() -> dict:
    return {
        "inline_keyboard": [[
            {"text": "👨 Чоловіча", "callback_data": "onb:sex:male"},
            {"text": "👩 Жіноча", "callback_data": "onb:sex:female"},
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


def goal_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔥 Схуднути", "callback_data": "onb:goal:lose"}],
            [{"text": "⚖️ Підтримувати вагу", "callback_data": "onb:goal:maintain"}],
            [{"text": "💪 Набрати м'язи", "callback_data": "onb:goal:gain"}],
        ]
    }


def confirm_calories_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Прийняти цю цифру", "callback_data": "onb:cal:accept"}],
            [{"text": "✏️ Ввести свою", "callback_data": "onb:cal:custom"}],
        ]
    }


def language_keyboard() -> dict:
    """EN / UK language picker (F-2)."""
    return {
        "inline_keyboard": [
            [
                {"text": "🇬🇧 English",     "callback_data": "lang:set:en"},
                {"text": "🇺🇦 Українська", "callback_data": "lang:set:uk"},
            ],
        ]
    }


def health_menu_keyboard() -> dict:
    """Top-level /health menu (F-1)."""
    return {
        "inline_keyboard": [
            [{"text": "🥜 Алергени",        "callback_data": "h:set:allergens"}],
            [{"text": "🩺 Хронічні стани",  "callback_data": "h:set:conditions"}],
            [{"text": "🧹 Очистити все",    "callback_data": "h:clear"}],
        ]
    }


def tz_keyboard(prefix: str = "tz:set") -> dict:
    """Six timezone presets + 'Other' (free-text). The same keyboard is used
    by onboarding (prefix=onb:tz) and by /timezone (prefix=tz:set).
    Keep this list in sync with ``lib.datehelpers.TZ_PRESETS``."""
    return {
        "inline_keyboard": [
            [
                {"text": "🇺🇦 Київ",                "callback_data": f"{prefix}:Europe/Kyiv"},
                {"text": "🇬🇧 Лондон",              "callback_data": f"{prefix}:Europe/London"},
            ],
            [
                {"text": "🇩🇪 Берлін / Варшава",     "callback_data": f"{prefix}:Europe/Berlin"},
                {"text": "🇺🇸 Нью-Йорк",            "callback_data": f"{prefix}:America/New_York"},
            ],
            [
                {"text": "🇺🇸 Лос-Анджелес",        "callback_data": f"{prefix}:America/Los_Angeles"},
                {"text": "🇦🇪 Дубай",                "callback_data": f"{prefix}:Asia/Dubai"},
            ],
            [
                {"text": "✏️ Інша зона (вкажу вручну)", "callback_data": f"{prefix}:custom"},
            ],
        ]
    }


def profile_edit_keyboard() -> dict:
    """Quick-edit actions shown under the /profile message."""
    return {
        "inline_keyboard": [
            [
                {"text": "⚖️ Змінити вагу", "callback_data": "prof:weight"},
                {"text": "🎯 Змінити мету", "callback_data": "prof:goal"},
            ],
            [
                {"text": "🏁 Цільова вага", "callback_data": "prof:target_weight"},
                {"text": "📈 Тижнева ціль", "callback_data": "prof:weekly_delta"},
            ],
            [
                {"text": "💧 Ціль води", "callback_data": "prof:water"},
                {"text": "✏️ Все спочатку", "callback_data": "onb:restart"},
            ],
        ]
    }


def goals_edit_keyboard(has_target: bool, has_delta: bool) -> dict:
    """Inline edit buttons shown under the /goals message."""
    target_label = "🏁 Змінити ціль" if has_target else "🏁 Поставити ціль"
    delta_label  = "📈 Змінити темп"  if has_delta  else "📈 Поставити темп"
    return {
        "inline_keyboard": [
            [
                {"text": target_label, "callback_data": "prof:target_weight"},
                {"text": delta_label,  "callback_data": "prof:weekly_delta"},
            ],
            [
                {"text": "⚖️ Записати вагу", "callback_data": "prof:weight"},
            ],
        ]
    }


def profile_goal_keyboard() -> dict:
    """Inline goal picker used by the /profile → 🎯 Змінити мету flow."""
    return {
        "inline_keyboard": [
            [{"text": "🔥 Схуднути",           "callback_data": "prof:goal:lose"}],
            [{"text": "⚖️ Підтримувати вагу",  "callback_data": "prof:goal:maintain"}],
            [{"text": "💪 Набрати м'язи",      "callback_data": "prof:goal:gain"}],
        ]
    }


def dashboard_inline_keyboard() -> dict:
    """Inline keyboard with a single web_app button — this launch mode DOES
    provide signed initData, unlike the KeyboardButton.web_app mode.
    """
    return {
        "inline_keyboard": [[
            {"text": "📱 Відкрити Dashboard", "web_app": {"url": _dashboard_url()}}
        ]]
    }


def set_chat_menu_button(chat_id: int | None = None) -> dict:
    """Register a persistent Mini App button as the bot's chat menu button
    (the icon left of the input area). When chat_id is given, applies to that
    specific chat (the global-default setChatMenuButton call sometimes doesn't
    take effect in Telegram, so we call this per-user on /start).
    """
    payload: dict = {
        "menu_button": {
            "type": "web_app",
            "text": "📱 Dashboard",
            "web_app": {"url": _dashboard_url()},
        }
    }
    if chat_id is not None:
        payload["chat_id"] = chat_id
    try:
        resp = httpx.post(f"{BASE_URL}/setChatMenuButton", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_my_commands(commands: list[dict], language_code: str | None = None) -> dict:
    """Register the bot's native command menu (the blue 'Menu' button)."""
    payload: dict = {"commands": commands}
    if language_code:
        payload["language_code"] = language_code
    try:
        resp = httpx.post(f"{BASE_URL}/setMyCommands", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
