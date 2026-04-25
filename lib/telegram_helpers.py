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
                {"text": "💧 Ціль води", "callback_data": "prof:water"},
            ],
            [
                {"text": "✏️ Все спочатку", "callback_data": "onb:restart"},
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
