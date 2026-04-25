"""Vercel serverless handler for Telegram webhook updates."""
import hmac
import html as _html
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import (
    WEBHOOK_SECRET,
    RECALC_PROMPT,
    ALLOWED_USER_IDS,
    calorie_target_from_profile,
    macro_gram_targets_from_profile,
    macro_gram_targets,
)
from lib.database import (
    consume_quota,
    get_conn,
    init_db,
    upsert_user,
    save_pending_photo,
    save_pending_text,
    pop_pending_entry,
    cleanup_stale_pending,
    cleanup_stale_analyses,
    save_pending_analysis,
    get_pending_analysis,
    pop_pending_analysis,
    set_awaiting_manual,
    save_meal,
    upsert_daily_log_from_meal,
    get_today_log,
    get_history,
    get_meals_for_day,
    delete_meal,
    recalc_daily_log,
    get_chat_history,
    append_chat_message,
    cleanup_stale_chat,
    get_log_for_date,
    get_profile,
    ensure_profile_row,
    update_profile,
    profile_is_complete,
    reset_onboarding,
    toggle_favorite,
    set_favorite,
    get_meal_by_id,
    get_recent_meals,
    get_favorites,
    clone_meal_for_today,
    add_water,
    remove_last_water_today,
    get_water_today,
    get_water_target,
    set_water_target,
    upsert_water_target_from_profile,
    insert_weight,
    set_awaiting_input,
)
from lib.telegram_helpers import (
    send_message,
    send_photo,
    answer_callback_query,
    get_file_bytes,
    edit_message_text,
    edit_message_reply_markup,
    send_chat_action,
    meal_type_keyboard,
    moderation_keyboard,
    alternates_keyboard,
    scanner_inline_keyboard,
    menu_log_keyboard,
    plan_pantry_keyboard,
    plan_day_keyboard,
    suggest_followup_keyboard,
    meals_list_keyboard,
    main_menu_keyboard,
    dashboard_inline_keyboard,
    set_chat_menu_button,
    sex_keyboard,
    gym_keyboard,
    goal_keyboard,
    confirm_calories_keyboard,
    profile_edit_keyboard,
    profile_goal_keyboard,
    goals_edit_keyboard,
    cancel_only_keyboard,
    recent_meals_keyboard,
    meal_logged_actions_keyboard,
    undo_relog_keyboard,
    water_keyboard,
    water_goal_keyboard,
    tz_keyboard,
    health_menu_keyboard,
    language_keyboard,
)
from lib.openai_vision import (
    analyze_photo,
    analyze_text,
    normalize_candidates,
    is_ambiguous,
    candidate_to_analysis,
    analyze_menu,
)
from lib.openai_voice import transcribe_voice
from lib.openai_nutrition import suggest_meal
from lib.openai_chat import ask_chat
from lib.log import setup_sentry, http_handler, error
from lib.formatters import (
    welcome_message,
    help_message,
    format_today_progress,
    format_streak_summary,
    format_yesterday,
    format_history,
    format_day_detail,
    format_meal_logged,
    format_meal_preview,
    format_alternates_intro,
    format_aliases,
    BARCODE_SCAN_INTRO,
    BARCODE_GRAMS_PROMPT,
    BARCODE_GRAMS_INVALID,
    BARCODE_PENDING_EXPIRED,
    BARCODE_MANUAL_PROMPT,
    BARCODE_MANUAL_INVALID,
    BARCODE_NOT_FOUND,
    BARCODE_LOOKUP_FAILED,
    BARCODE_FOUND_HEADER,
    MENU_PROMPT_INTRO,
    MENU_NO_DISHES,
    MENU_OCR_FAILED,
    MENU_PENDING_EXPIRED,
    format_menu_dishes_intro,
    format_menu_dish_row,
    PLAN_INTRO,
    PLAN_GENERATING,
    PLAN_FAILED,
    PLAN_PANTRY_TOO_LONG,
    PLAN_HEADER_NOTES,
    format_meal_plan_day,
    FRIDGE_PROMPT,
    FRIDGE_TOO_LONG,
    SUGGEST_VARIATION_HINT,
    format_meals_list,
    format_profile,
    format_recommendation,
    PHOTO_PROMPT_MEAL_TYPE,
    TEXT_PROMPT_MEAL_TYPE,
    ANALYZING_WAIT,
    RECALC_WAIT,
    PHOTO_DOWNLOAD_FAILED,
    PHOTO_ANALYSIS_FAILED,
    TEXT_ANALYSIS_FAILED,
    PENDING_EXPIRED,
    MANUAL_INPUT_PROMPT,
    MEAL_DELETED,
    MEAL_EDIT_PROMPT,
    MEAL_NOT_FOUND,
    MEAL_CANCELLED,
    NO_MEALS_TO_MANAGE,
    UNKNOWN_COMMAND,
    SUGGEST_THINKING,
    SUGGEST_FAILED,
    HISTORY_USAGE,
    ASK_PROMPT,
    ASK_THINKING,
    ASK_ERROR,
    ONBOARDING_INTRO,
    ONBOARDING_ASK_AGE,
    ONBOARDING_ASK_SEX,
    ONBOARDING_ASK_WEIGHT,
    ONBOARDING_ASK_HEIGHT,
    ONBOARDING_ASK_GYM,
    ONBOARDING_ASK_GOAL,
    ONBOARDING_INVALID_NUMBER,
    ONBOARDING_AGE_RANGE,
    ONBOARDING_WEIGHT_RANGE,
    ONBOARDING_HEIGHT_RANGE,
    ONBOARDING_CUSTOM_CAL_PROMPT,
    ONBOARDING_CUSTOM_CAL_RANGE,
    ONBOARDING_NEED_BUTTON,
    ONBOARDING_DONE,
    ONBOARDING_REQUIRED,
    BTN_ASK,
    BTN_TODAY,
    BTN_YESTERDAY,
    BTN_MEALS,
    BTN_PROFILE,
    BTN_SUGGEST,
    BTN_DASHBOARD,
    BTN_FAV,
    BTN_WATER,
    BTN_SCAN,
    MENU_BUTTON_LABELS,
    format_water,
    format_meal_list_entry,
    FAV_EMPTY_LIST,
    RECENT_EMPTY_LIST,
    FAV_ADDED,
    FAV_REMOVED,
    RELOG_DONE,
    RELOG_FAILED,
    UNDO_EXPIRED,
    UNDO_DONE,
    WATER_UNDO_EMPTY,
    WATER_GOAL_PROMPT,
    WATER_GOAL_SAVED,
    WEIGHT_CHECKIN_SKIPPED,
    WEIGHT_INPUT_PROMPT,
    WEIGHT_INVALID,
    WEIGHT_NOT_A_NUMBER,
    GOAL_UPDATE_PROMPT,
    GOAL_UPDATED,
    TARGET_WEIGHT_ASK_LOSE,
    TARGET_WEIGHT_ASK_GAIN,
    TARGET_WEIGHT_INVALID,
    TARGET_WEIGHT_LOSE_MISMATCH,
    TARGET_WEIGHT_GAIN_MISMATCH,
    TARGET_WEIGHT_SAVED,
    TARGET_WEIGHT_CLEARED,
    WEEKLY_DELTA_ASK_LOSE,
    WEEKLY_DELTA_ASK_GAIN,
    WEEKLY_DELTA_INVALID,
    WEEKLY_DELTA_WRONG_SIGN,
    WEEKLY_DELTA_SAVED,
    WEEKLY_DELTA_NOT_FOR_MAINTAIN,
    GOALS_NO_PROFILE,
    format_goals,
    format_projection_line,
    ONBOARDING_ASK_TZ,
    ONBOARDING_TZ_CUSTOM_PROMPT,
    ONBOARDING_TZ_INVALID,
    ONBOARDING_TZ_SAVED,
    TIMEZONE_PROMPT,
    TIMEZONE_NOT_ONBOARDED,
    TIMEZONE_SAVED,
    TIMEZONE_CUSTOM_PROMPT,
    TIMEZONE_CANCELLED,
)
from lib.datehelpers import is_valid_tz, now_user, today_str_user
from lib.database import (
    get_health_profile,
    set_health_allergens,
    set_health_conditions,
    clear_health_profile,
    get_streak,
    update_streak_for_meal,
    get_weight_history,
    save_menu_ocr_result,
    get_menu_ocr_result,
    save_meal_plan,
    get_meal_plan,
    get_meals_in_range,
)
from lib import goals as goals_mod
from lib import personalization as personalization_mod
from lib import off as off_mod
from lib import mealplan as mealplan_mod
from lib import recap as recap_mod
from lib.health import (
    ALLERGENS as HEALTH_ALLERGENS,
    CONDITIONS as HEALTH_CONDITIONS,
    addendum_for_profile,
    is_clear_keyword,
    parse_csv as parse_health_csv,
    render_labels as render_health_labels,
)
from lib import i18n as i18n_mod
from lib.formatters import (
    HEALTH_HEADER,
    HEALTH_NOT_ONBOARDED,
    HEALTH_ALLERGENS_PROMPT,
    HEALTH_CONDITIONS_PROMPT,
    HEALTH_SAVED,
    HEALTH_SAVED_WITH_HINTS,
    HEALTH_CLEARED,
    HEALTH_CANCELLED,
    HEALTH_INVALID_ALL,
)


NOT_AUTHORIZED = "🔒 Цей бот зараз недоступний. Спробуй пізніше."


def _is_allowed(user_id: int | None) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


# Per-user daily caps on OpenAI-spending actions. Tuned so a single Telegram
# user costs at most ~$0.50/day in worst-case OpenAI usage. The bot is public
# (no allowlist), so these caps are the primary defense against cost abuse.
DAILY_QUOTAS: dict[str, int] = {
    "meal_analysis": 50,    # photo + text + voice meal logging (GPT-4o vision/text)
    "voice_transcribe": 30, # Whisper transcription (cheap but bandwidth-heavy)
    "ask": 50,              # /ask chat (GPT-4.1-mini)
    "suggest": 20,          # /suggest_meal recipe generator (GPT-4o)
    "menu_ocr": 5,          # F-9: /menu restaurant menu scan (GPT-4o vision, multi-image)
    "plan_generate": 3,     # F-10: /plan 3-day meal plan (GPT-4o JSON, ~1800 tokens)
}

QUOTA_ACTION_LABELS = {
    "meal_analysis": "запис страви",
    "voice_transcribe": "голосове повідомлення",
    "ask": "запит до ШІ",
    "suggest": "ідея страви",
    "menu_ocr": "сканер меню",
    "plan_generate": "3-денний план",
}

QUOTA_REJECT_TEMPLATE = (
    "⏳ На сьогодні твій ліміт для «{action}» вичерпано ({limit}/день). "
    "Спробуй завтра або напиши страву текстом, якщо це фото."
)


def _enforce_quota(conn, chat_id: int, user_id: int, action: str) -> bool:
    """Atomically increment the quota counter; if over limit, send a friendly
    Ukrainian reject message and return False. Returns True when the caller
    is allowed to proceed.

    Errors talking to the DB fall open (return True) so a transient quota-table
    failure doesn't break the bot for legitimate users — the daily limit is a
    cost guardrail, not a security boundary.
    """
    limit = DAILY_QUOTAS.get(action)
    if not limit or not user_id:
        return True
    try:
        new_count = consume_quota(conn, user_id, action)
    except Exception as e:
        print("quota check error:", e, flush=True)
        return True
    if new_count > limit:
        label = QUOTA_ACTION_LABELS.get(action, action)
        send_message(chat_id, QUOTA_REJECT_TEMPLATE.format(action=label, limit=limit))
        return False
    return True


MAX_WEBHOOK_BYTES = 512 * 1024


setup_sentry("webhook")


class handler(BaseHTTPRequestHandler):
    @http_handler("webhook")
    def do_POST(self):
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        # Constant-time comparison to avoid leaking secret bytes via timing.
        # `hmac.compare_digest` requires same-length bytes, hence the encode.
        if not WEBHOOK_SECRET or not hmac.compare_digest(
            secret.encode("utf-8"), WEBHOOK_SECRET.encode("utf-8")
        ):
            self.send_response(403)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length > MAX_WEBHOOK_BYTES:
            self.send_response(413)
            self.end_headers()
            return

        try:
            raw = self.rfile.read(length) if length else b"{}"
            update = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._respond_ok()
            return

        try:
            process_update(update)
        except Exception as exc:
            error("webhook_process_update_failed", exc=exc)

        self._respond_ok()

    def do_GET(self):
        # The webhook only handles POST from Telegram. Don't leak deployment
        # info to unauthenticated probes.
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.end_headers()

    def _respond_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())


# ---------- Calorie target (new weight×goal formula) ----------
#
# We keep collecting age / sex / height / gym_per_week in onboarding because
# they add useful context to AI prompts — but the calorie target itself is
# computed purely from bodyweight and goal per the product spreadsheet.
# See MACRO_PER_KG in lib/config.py.

_VALID_GYM_FREQ = {"0", "1-2", "3-4", "5-6", "7"}
_VALID_GOALS = {"lose", "maintain", "gain"}


# ---------- Main dispatcher ----------

def process_update(update: dict) -> None:
    conn = get_conn()
    try:
        init_db(conn)
        cleanup_stale_pending(conn, minutes=10)
        cleanup_stale_analyses(conn, minutes=10)
        cleanup_stale_chat(conn, minutes=60)

        if "callback_query" in update:
            cb_user_id = update["callback_query"].get("from", {}).get("id")
            if not _is_allowed(cb_user_id):
                _reject_cb(update["callback_query"])
                return
            handle_callback(conn, update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        user = message.get("from", {})
        user_id = user.get("id")
        username = user.get("username") or user.get("first_name")
        first_name = user.get("first_name")

        if not _is_allowed(user_id):
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                send_message(chat_id, NOT_AUTHORIZED)
            return

        if user_id:
            upsert_user(conn, user_id, username)

        chat_id = message["chat"]["id"]

        # Onboarding takes precedence over everything except explicit /start reset.
        profile = get_profile(conn, user_id) if user_id else None
        text = (message.get("text") or "").strip()
        is_start = text.lower().startswith("/start")

        if is_start:
            # /start: always (re)introduce + begin onboarding if incomplete
            handle_start(
                conn, chat_id, user_id, first_name, profile,
                language_code=user.get("language_code"),
            )
            return

        # If onboarding not complete, route every message through onboarding handler.
        if user_id and not profile_is_complete(profile):
            # Allow /help without a profile
            if text.lower().startswith("/help"):
                send_message(chat_id, help_message())
                return
            if message.get("photo"):
                send_message(chat_id, ONBOARDING_REQUIRED)
                return
            handle_onboarding_text(conn, chat_id, user_id, first_name, text, profile)
            return

        # ----- Normal flow (profile complete) -----

        if message.get("voice") or message.get("audio"):
            handle_voice(conn, message)
            return

        # F-9: when the user is in /menu mode, photos go to the OCR path
        # instead of the standard meal-analysis flow.
        if message.get("photo") and profile and profile.get("awaiting_input_type") == "menu_photo":
            handle_menu_photo(conn, message, profile)
            return

        if message.get("photo"):
            handle_photo(conn, message)
            return

        if not text:
            return

        if text == BTN_DASHBOARD:
            send_message(
                chat_id,
                "📱 Натисни кнопку нижче, щоб відкрити Dashboard:",
                reply_markup=dashboard_inline_keyboard(),
            )
            return
        if text == BTN_WATER:
            # Quick-add 250 ml and reply with updated bar + keyboard.
            handle_water_quickadd(conn, chat_id, user_id, amount_ml=250)
            return
        if text in MENU_BUTTON_LABELS:
            mapped = {
                BTN_ASK: "/ask",
                BTN_FAV: "/fav",
                BTN_MEALS: "/meals",
                BTN_PROFILE: "/profile",
                BTN_SUGGEST: "/suggest_meal",
                BTN_SCAN: "/scan",
            }.get(text)
            if mapped:
                handle_command(conn, message, mapped, first_name, profile)
                return

        # Weight check-in / manual weight edit takes priority over everything
        # except the /cancel escape hatch (handled further down).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "weight"
            and text.lower().strip() != "/cancel"
        ):
            handle_weight_input(conn, chat_id, user_id, first_name, text, profile)
            return

        # Manual water-target entry from /profile → 💧 Ціль води → Своє значення.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "water_target"
            and text.lower().strip() != "/cancel"
        ):
            handle_water_target_input(conn, chat_id, user_id, text)
            return

        # Motivation target-weight entry from /profile → 🏁 Цільова вага.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "target_weight"
            and text.lower().strip() != "/cancel"
        ):
            handle_target_weight_input(conn, chat_id, user_id, text, profile)
            return

        # F-5: weekly delta input (kg/week target).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "weekly_delta"
            and text.lower().strip() != "/cancel"
        ):
            handle_weekly_delta_input(conn, chat_id, user_id, text, profile)
            return

        # F-8: custom barcode portion grams input.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "barcode_grams"
            and text.lower().strip() != "/cancel"
        ):
            handle_barcode_grams_input(conn, chat_id, user_id, first_name, text, profile)
            return

        # F-8: manual EAN entry (fallback when camera path fails).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "barcode_manual"
            and text.lower().strip() != "/cancel"
        ):
            handle_barcode_manual_input(conn, chat_id, user_id, text, profile)
            return

        # F-10: pantry items capture for /plan.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "plan_pantry"
            and text.lower().strip() != "/cancel"
        ):
            handle_plan_pantry_input(conn, chat_id, user_id, text, profile)
            return

        # F-11: fridge ingredients capture for /suggest_meal variation.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "fridge_ingredients"
            and text.lower().strip() != "/cancel"
        ):
            handle_fridge_input(conn, chat_id, user_id, text, profile)
            return

        # Free-text IANA timezone from /timezone → ✏️ Інша зона.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "timezone"
        ):
            handle_timezone_input(conn, chat_id, user_id, text)
            return

        # Free-text health profile input (allergens / conditions) from /health.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") in ("health_allergens", "health_conditions")
        ):
            handle_health_input(conn, chat_id, user_id, text, profile["awaiting_input_type"])
            return

        if text.startswith("/"):
            if user_id and text.lower().strip() == "/cancel":
                pending = get_pending_analysis(conn, user_id)
                if pending and pending["awaiting_manual"]:
                    pop_pending_analysis(conn, user_id)
                    pop_pending_entry(conn, user_id)
                    send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
                    return
            handle_command(conn, message, text, first_name, profile)
            return

        reply_to = message.get("reply_to_message") or {}
        if (
            reply_to.get("from", {}).get("is_bot")
            and reply_to.get("text") == ASK_PROMPT
            and user_id
        ):
            handle_ask(conn, user_id, chat_id, text, profile)
            return

        if user_id:
            pending = get_pending_analysis(conn, user_id)
            if pending and pending["awaiting_manual"]:
                handle_manual_text_input(conn, message, text, pending, profile)
                return

        handle_text_entry(conn, message, text)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _reject_cb(cb: dict) -> None:
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if chat_id:
        send_message(chat_id, NOT_AUTHORIZED)
    answer_callback_query(cb["id"], "🔒 Недоступно")


# ---------- Onboarding ----------

def handle_start(
    conn,
    chat_id: int,
    user_id: int,
    first_name: str | None,
    profile: dict | None,
    language_code: str | None = None,
) -> None:
    try:
        set_chat_menu_button(chat_id=chat_id)
    except Exception as e:
        print("set_chat_menu_button error:", e, flush=True)

    if profile_is_complete(profile):
        send_message(chat_id, welcome_message(first_name), reply_markup=main_menu_keyboard())
        return

    # Fresh user or unfinished profile: kick off onboarding.
    profile = ensure_profile_row(conn, user_id)
    reset_onboarding(conn, user_id)
    # Seed lang from Telegram client locale on first /start. Users can override
    # later via /language. We only do this once: if the row already has a
    # non-default lang, the user has chosen — keep their pick.
    if language_code:
        detected = i18n_mod.normalize_lang(language_code)
        update_profile(conn, user_id, lang=detected)
    send_message(chat_id, ONBOARDING_INTRO)
    send_message(chat_id, ONBOARDING_ASK_AGE)


def _parse_int(text: str) -> int | None:
    try:
        return int(text.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _parse_float(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _finalize_onboarding(conn, chat_id: int, user_id: int, first_name: str | None) -> None:
    """Tail of the onboarding flow: estimate water target, send done message.

    Called from both terminal paths (timezone preset tap and custom IANA text)
    once the profile is fully populated and ``onboarding_step`` is ``done``.
    """
    profile = get_profile(conn, user_id) or {}
    cal = profile.get("daily_calorie_target") or 2000
    try:
        w = profile.get("weight_kg")
        water = upsert_water_target_from_profile(conn, user_id, float(w)) if w else None
    except Exception as e:
        error("water_target_upsert_failed", exc=e, user_id=user_id)
        water = None
    done_text = ONBOARDING_DONE.format(name=first_name or "друже")
    if water:
        done_text += (
            f"\n\n🎯 Калорії: <b>{cal} ккал/день</b>\n"
            f"💧 Вода: <b>{water} мл/день</b> (можна змінити в /water)"
        )
    send_message(chat_id, done_text, reply_markup=main_menu_keyboard())


def handle_onboarding_text(conn, chat_id: int, user_id: int, first_name: str | None,
                           text: str, profile: dict | None) -> None:
    if profile is None:
        profile = ensure_profile_row(conn, user_id)
    step = profile.get("onboarding_step") or "awaiting_age"

    if step == "awaiting_age":
        age = _parse_int(text)
        if age is None:
            send_message(chat_id, ONBOARDING_INVALID_NUMBER)
            return
        if not (10 <= age <= 100):
            send_message(chat_id, ONBOARDING_AGE_RANGE)
            return
        update_profile(conn, user_id, age=age, onboarding_step="awaiting_sex")
        send_message(chat_id, ONBOARDING_ASK_SEX, reply_markup=sex_keyboard())

    elif step == "awaiting_sex":
        send_message(chat_id, ONBOARDING_NEED_BUTTON, reply_markup=sex_keyboard())

    elif step == "awaiting_weight":
        w = _parse_float(text)
        if w is None:
            send_message(chat_id, ONBOARDING_INVALID_NUMBER)
            return
        if not (30 <= w <= 300):
            send_message(chat_id, ONBOARDING_WEIGHT_RANGE)
            return
        update_profile(conn, user_id, weight_kg=w, onboarding_step="awaiting_height")
        send_message(chat_id, ONBOARDING_ASK_HEIGHT)

    elif step == "awaiting_height":
        h = _parse_int(text)
        if h is None:
            send_message(chat_id, ONBOARDING_INVALID_NUMBER)
            return
        if not (100 <= h <= 250):
            send_message(chat_id, ONBOARDING_HEIGHT_RANGE)
            return
        update_profile(conn, user_id, height_cm=h, onboarding_step="awaiting_gym")
        send_message(chat_id, ONBOARDING_ASK_GYM, reply_markup=gym_keyboard())

    elif step == "awaiting_gym":
        send_message(chat_id, ONBOARDING_NEED_BUTTON, reply_markup=gym_keyboard())

    elif step == "awaiting_goal":
        send_message(chat_id, ONBOARDING_NEED_BUTTON, reply_markup=goal_keyboard())

    elif step == "awaiting_target_weight":
        tw = _parse_float(text)
        if tw is None:
            send_message(chat_id, WEIGHT_NOT_A_NUMBER)
            return
        if not (30 <= tw <= 300):
            send_message(chat_id, TARGET_WEIGHT_INVALID)
            return
        current_w = profile.get("weight_kg")
        goal = profile.get("goal") or "maintain"
        if current_w is not None:
            if goal == "lose" and tw >= float(current_w):
                send_message(chat_id, TARGET_WEIGHT_LOSE_MISMATCH.format(current=current_w))
                return
            if goal == "gain" and tw <= float(current_w):
                send_message(chat_id, TARGET_WEIGHT_GAIN_MISMATCH.format(current=current_w))
                return
        rec = calorie_target_from_profile(float(current_w or 70), goal)
        update_profile(
            conn, user_id,
            target_weight_kg=float(tw),
            recommended_calorie_target=rec,
            onboarding_step="awaiting_confirm",
        )
        profile_after = get_profile(conn, user_id) or {}
        send_message(chat_id, TARGET_WEIGHT_SAVED.format(target=tw))
        send_message(
            chat_id,
            format_recommendation(profile_after, rec),
            reply_markup=confirm_calories_keyboard(),
        )

    elif step == "awaiting_confirm":
        send_message(chat_id, ONBOARDING_NEED_BUTTON, reply_markup=confirm_calories_keyboard())

    elif step == "awaiting_custom_cal":
        cal = _parse_int(text)
        if cal is None:
            send_message(chat_id, ONBOARDING_INVALID_NUMBER)
            return
        if not (1000 <= cal <= 6000):
            send_message(chat_id, ONBOARDING_CUSTOM_CAL_RANGE)
            return
        update_profile(
            conn, user_id,
            daily_calorie_target=cal,
            onboarding_step="awaiting_tz",
        )
        send_message(chat_id, ONBOARDING_ASK_TZ, reply_markup=tz_keyboard(prefix="onb:tz"))

    elif step == "awaiting_tz":
        # User typed instead of tapping a preset — reshow the keyboard.
        send_message(chat_id, ONBOARDING_NEED_BUTTON, reply_markup=tz_keyboard(prefix="onb:tz"))

    elif step == "awaiting_tz_custom":
        tz_input = text.strip()
        if tz_input.lower() in ("/cancel", "cancel"):
            update_profile(conn, user_id, onboarding_step="awaiting_tz")
            send_message(chat_id, ONBOARDING_ASK_TZ, reply_markup=tz_keyboard(prefix="onb:tz"))
            return
        if not is_valid_tz(tz_input):
            send_message(chat_id, ONBOARDING_TZ_INVALID)
            return
        update_profile(conn, user_id, tz=tz_input, onboarding_step="done")
        send_message(chat_id, ONBOARDING_TZ_SAVED.format(tz=tz_input))
        _finalize_onboarding(conn, chat_id, user_id, first_name)

    else:
        # Unexpected state — restart
        reset_onboarding(conn, user_id)
        send_message(chat_id, ONBOARDING_ASK_AGE)


def handle_onboarding_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    first_name = cb["from"].get("first_name")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    profile = ensure_profile_row(conn, user_id)
    step = profile.get("onboarding_step") or "awaiting_age"

    # Allow restart from the profile screen regardless of step
    if data == "onb:restart":
        answer_callback_query(cb_id, "🔄 Починаємо заново")
        reset_onboarding(conn, user_id)
        send_message(chat_id, ONBOARDING_INTRO)
        send_message(chat_id, ONBOARDING_ASK_AGE)
        return

    if data.startswith("onb:sex:"):
        if step != "awaiting_sex":
            answer_callback_query(cb_id, "Вже відповів(ла) 🙂")
            return
        sex = data.split(":", 2)[2]
        if sex not in ("male", "female"):
            answer_callback_query(cb_id, "Невірна відповідь")
            return
        update_profile(conn, user_id, sex=sex, onboarding_step="awaiting_weight")
        answer_callback_query(cb_id, "Записав")
        send_message(chat_id, ONBOARDING_ASK_WEIGHT)
        return

    if data.startswith("onb:gym:"):
        if step != "awaiting_gym":
            answer_callback_query(cb_id, "Вже відповів(ла) 🙂")
            return
        freq = data.split(":", 2)[2]
        if freq not in _VALID_GYM_FREQ:
            answer_callback_query(cb_id, "Невірна відповідь")
            return
        update_profile(conn, user_id, gym_per_week=freq, onboarding_step="awaiting_goal")
        answer_callback_query(cb_id, "Записав")
        send_message(chat_id, ONBOARDING_ASK_GOAL, reply_markup=goal_keyboard())
        return

    if data.startswith("onb:goal:"):
        if step != "awaiting_goal":
            answer_callback_query(cb_id, "Вже відповів(ла) 🙂")
            return
        goal = data.split(":", 2)[2]
        if goal not in _VALID_GOALS:
            answer_callback_query(cb_id, "Невірна відповідь")
            return
        updated = get_profile(conn, user_id) or {}
        if not updated.get("weight_kg"):
            reset_onboarding(conn, user_id)
            answer_callback_query(cb_id, "Щось пішло не так, починаємо заново")
            send_message(chat_id, ONBOARDING_ASK_AGE)
            return

        # lose / gain → ask the motivation target weight first, then recommendation.
        if goal in ("lose", "gain"):
            update_profile(
                conn, user_id,
                goal=goal,
                target_weight_kg=None,
                onboarding_step="awaiting_target_weight",
            )
            answer_callback_query(cb_id, "Записав")
            prompt = TARGET_WEIGHT_ASK_LOSE if goal == "lose" else TARGET_WEIGHT_ASK_GAIN
            send_message(chat_id, prompt)
            return

        # maintain → straight to the calorie recommendation (no target weight needed).
        rec = calorie_target_from_profile(float(updated["weight_kg"]), goal)
        update_profile(
            conn, user_id,
            goal=goal,
            target_weight_kg=None,
            recommended_calorie_target=rec,
            onboarding_step="awaiting_confirm",
        )
        answer_callback_query(cb_id, "Рахую твою норму…")
        profile_after = get_profile(conn, user_id) or {}
        send_message(
            chat_id,
            format_recommendation(profile_after, rec),
            reply_markup=confirm_calories_keyboard(),
        )
        return

    if data == "onb:cal:accept":
        if step != "awaiting_confirm":
            answer_callback_query(cb_id, "Вже прийнято 🙂")
            return
        profile_after = get_profile(conn, user_id) or {}
        rec = profile_after.get("recommended_calorie_target") or 2000
        update_profile(
            conn, user_id,
            daily_calorie_target=rec,
            onboarding_step="awaiting_tz",
        )
        answer_callback_query(cb_id, "✅ Прийнято")
        send_message(chat_id, ONBOARDING_ASK_TZ, reply_markup=tz_keyboard(prefix="onb:tz"))
        return

    if data.startswith("onb:tz:"):
        if step != "awaiting_tz":
            answer_callback_query(cb_id, "Вже відповів(ла) 🙂")
            return
        tz_value = data.split(":", 2)[2]
        if tz_value == "custom":
            update_profile(conn, user_id, onboarding_step="awaiting_tz_custom")
            answer_callback_query(cb_id, "Чекаю на назву зони")
            send_message(chat_id, ONBOARDING_TZ_CUSTOM_PROMPT)
            return
        if not is_valid_tz(tz_value):
            answer_callback_query(cb_id, "Невідома зона")
            return
        update_profile(conn, user_id, tz=tz_value, onboarding_step="done")
        answer_callback_query(cb_id, "✅ Записав")
        send_message(chat_id, ONBOARDING_TZ_SAVED.format(tz=tz_value))
        _finalize_onboarding(conn, chat_id, user_id, first_name)
        return

    if data == "onb:cal:custom":
        if step != "awaiting_confirm":
            answer_callback_query(cb_id, "Вже прийнято 🙂")
            return
        update_profile(conn, user_id, onboarding_step="awaiting_custom_cal")
        answer_callback_query(cb_id, "Чекаю на число")
        send_message(chat_id, ONBOARDING_CUSTOM_CAL_PROMPT)
        return

    answer_callback_query(cb_id, "Невідома дія")


def handle_timezone_callback(conn, cb: dict, profile: dict) -> None:
    """Handle /timezone keyboard taps (callbacks prefixed `tz:set:`)."""
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        answer_callback_query(cb_id)
        return
    if not data.startswith("tz:set:"):
        answer_callback_query(cb_id, "Невідома дія")
        return
    tz_value = data.split(":", 2)[2]
    if tz_value == "custom":
        set_awaiting_input(conn, user_id, "timezone")
        answer_callback_query(cb_id, "Чекаю на назву зони")
        send_message(chat_id, TIMEZONE_CUSTOM_PROMPT)
        return
    if not is_valid_tz(tz_value):
        answer_callback_query(cb_id, "Невідома зона")
        return
    update_profile(conn, user_id, tz=tz_value)
    set_awaiting_input(conn, user_id, None)
    answer_callback_query(cb_id, "✅ Записав")
    send_message(chat_id, TIMEZONE_SAVED.format(tz=tz_value))


def handle_timezone_input(conn, chat_id: int, user_id: int, text: str) -> None:
    """Free-text IANA timezone input from /timezone → ✏️ Інша зона."""
    cleaned = text.strip()
    if cleaned.lower() in ("/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, TIMEZONE_CANCELLED)
        return
    if not is_valid_tz(cleaned):
        send_message(chat_id, ONBOARDING_TZ_INVALID)
        return
    update_profile(conn, user_id, tz=cleaned)
    set_awaiting_input(conn, user_id, None)
    send_message(chat_id, TIMEZONE_SAVED.format(tz=cleaned))


# ---------- Health profile (F-1) ----------

def _send_health_menu(conn, chat_id: int, user_id: int) -> None:
    h = get_health_profile(conn, user_id) or {"allergens": [], "conditions": []}
    send_message(
        chat_id,
        HEALTH_HEADER.format(
            allergens=render_health_labels(h["allergens"], HEALTH_ALLERGENS),
            conditions=render_health_labels(h["conditions"], HEALTH_CONDITIONS),
        ),
        reply_markup=health_menu_keyboard(),
    )


def handle_health_callback(conn, cb: dict, profile: dict) -> None:
    """Handle /health menu taps (callbacks prefixed `h:`)."""
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        answer_callback_query(cb_id)
        return
    if data == "h:set:allergens":
        set_awaiting_input(conn, user_id, "health_allergens")
        answer_callback_query(cb_id, "Чекаю на список")
        send_message(chat_id, HEALTH_ALLERGENS_PROMPT)
        return
    if data == "h:set:conditions":
        set_awaiting_input(conn, user_id, "health_conditions")
        answer_callback_query(cb_id, "Чекаю на список")
        send_message(chat_id, HEALTH_CONDITIONS_PROMPT)
        return
    if data == "h:clear":
        clear_health_profile(conn, user_id)
        set_awaiting_input(conn, user_id, None)
        answer_callback_query(cb_id, "🧹 Очищено")
        send_message(chat_id, HEALTH_CLEARED)
        _send_health_menu(conn, chat_id, user_id)
        return
    answer_callback_query(cb_id, "Невідома дія")


def handle_language_callback(conn, cb: dict, profile: dict) -> None:
    """Handle /language keyboard taps (callbacks prefixed `lang:set:`)."""
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        answer_callback_query(cb_id)
        return
    if not data.startswith("lang:set:"):
        answer_callback_query(cb_id, "?")
        return
    lang = data.split(":", 2)[2]
    if lang not in i18n_mod.supported_langs():
        answer_callback_query(cb_id, "?")
        return
    update_profile(conn, user_id, lang=lang)
    answer_callback_query(cb_id, "✅")
    label = i18n_mod.t(f"lang_label_{lang}", locale=lang)
    send_message(chat_id, i18n_mod.t("language_saved", locale=lang, lang=label))


def handle_health_input(conn, chat_id: int, user_id: int, text: str, kind: str) -> None:
    """Free-text input for /health → set allergens / conditions.

    ``kind`` is "health_allergens" or "health_conditions" — picked up from
    the user's ``awaiting_input_type``.
    """
    cleaned = text.strip()
    if cleaned.lower() in ("/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, HEALTH_CANCELLED)
        return

    is_allergens = kind == "health_allergens"
    registry = HEALTH_ALLERGENS if is_allergens else HEALTH_CONDITIONS

    if is_clear_keyword(cleaned):
        if is_allergens:
            set_health_allergens(conn, user_id, [])
        else:
            set_health_conditions(conn, user_id, [])
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, HEALTH_CLEARED)
        _send_health_menu(conn, chat_id, user_id)
        return

    canon, unknown = parse_health_csv(cleaned, registry)
    if not canon:
        send_message(chat_id, HEALTH_INVALID_ALL)
        return

    if is_allergens:
        set_health_allergens(conn, user_id, canon)
    else:
        set_health_conditions(conn, user_id, canon)
    set_awaiting_input(conn, user_id, None)

    saved = render_health_labels(canon, registry)
    if unknown:
        send_message(
            chat_id,
            HEALTH_SAVED_WITH_HINTS.format(saved=saved, unknown=", ".join(unknown)),
        )
    else:
        send_message(chat_id, HEALTH_SAVED.format(saved=saved))
    _send_health_menu(conn, chat_id, user_id)


# ---------- Photo / text entry ----------

PHOTO_TOO_LARGE = "📸 Фото завелике — будь ласка, до 5 МБ."

# Hard cap on photo size before we pay to download the file from Telegram and
# ship it to GPT-4o vision. The largest entry in `photo[]` is the highest-
# resolution version Telegram is willing to surface.
MAX_PHOTO_BYTES = 5 * 1024 * 1024


def handle_photo(conn, message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    photos = message["photo"]
    largest = photos[-1] if photos else {}
    file_id = largest.get("file_id")
    if not file_id:
        return
    file_size = int(largest.get("file_size") or 0)
    if file_size > MAX_PHOTO_BYTES:
        send_message(chat_id, PHOTO_TOO_LARGE)
        return
    save_pending_photo(conn, user_id, file_id)
    send_message(chat_id, PHOTO_PROMPT_MEAL_TYPE, reply_markup=meal_type_keyboard())


def handle_text_entry(conn, message: dict, text: str) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    save_pending_text(conn, user_id, text)
    send_message(chat_id, TEXT_PROMPT_MEAL_TYPE, reply_markup=meal_type_keyboard())


# ---------- Voice entry (Whisper) ----------

VOICE_TOO_LONG = "🎙 Задовге повідомлення — будь ласка, до 60 с."
VOICE_EMPTY = "🤔 Не розпізнав їжу. Спробуй ще раз або напиши текстом."
VOICE_ERROR = "😵 Не вийшло розпізнати голос. Спробуй ще раз або напиши текстом."
VOICE_TRANSCRIPT = "🎙 Почув: «{text}»"


def handle_voice(conn, message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    voice = message.get("voice") or message.get("audio") or {}
    file_id = voice.get("file_id")
    file_size = voice.get("file_size") or 0
    if not file_id:
        return

    # Hard cap ~2 MB (≈60–90 s OGG/Opus) to keep Whisper + analysis under Vercel timeout.
    if file_size > 2 * 1024 * 1024:
        send_message(chat_id, VOICE_TOO_LONG)
        return

    if not _enforce_quota(conn, chat_id, user_id, "voice_transcribe"):
        return

    send_chat_action(chat_id, "typing")

    try:
        audio_bytes = get_file_bytes(file_id)
    except Exception as e:
        print("voice getFile error:", e, flush=True)
        send_message(chat_id, VOICE_ERROR)
        return

    try:
        transcript = transcribe_voice(audio_bytes)
    except Exception as e:
        print("whisper error:", e, flush=True)
        send_message(chat_id, VOICE_ERROR)
        return

    if not transcript or len(transcript) < 3:
        send_message(chat_id, VOICE_EMPTY)
        return

    safe = _html.escape(transcript, quote=False)
    send_message(chat_id, VOICE_TRANSCRIPT.format(text=safe))

    # If this voice message is a reply to the /ask prompt, treat transcript as a
    # chat question and route to handle_ask instead of the meal-logging flow.
    reply_to = message.get("reply_to_message") or {}
    if (
        reply_to.get("from", {}).get("is_bot")
        and reply_to.get("text") == ASK_PROMPT
    ):
        profile = get_profile(conn, user_id)
        handle_ask(conn, user_id, chat_id, transcript, profile)
        return

    # Otherwise reuse the existing text-entry flow: saves as pending and asks meal type.
    save_pending_text(conn, user_id, transcript)
    send_message(chat_id, TEXT_PROMPT_MEAL_TYPE, reply_markup=meal_type_keyboard())


# ---------- Callback router ----------

def handle_callback(conn, cb: dict) -> None:
    data = cb.get("data", "")
    if data.startswith("onb:"):
        handle_onboarding_callback(conn, cb)
        return
    # Everything below requires a completed profile.
    user_id = cb["from"]["id"]
    profile = get_profile(conn, user_id)
    if not profile_is_complete(profile):
        answer_callback_query(cb["id"], "Спочатку /start")
        message = cb.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if chat_id:
            send_message(chat_id, ONBOARDING_REQUIRED)
        return

    if data.startswith("meal_type:"):
        handle_meal_type_callback(conn, cb, profile)
    elif data.startswith("mod:"):
        handle_moderation_callback(conn, cb, profile)
    elif data.startswith("pick:"):
        handle_alternates_pick(conn, cb, profile)
    elif data.startswith("barcode:"):
        handle_barcode_callback(conn, cb, profile)
    elif data.startswith("menu:"):
        handle_menu_callback(conn, cb, profile)
    elif data.startswith("plan:"):
        handle_plan_callback(conn, cb, profile)
    elif data.startswith("suggest:"):
        handle_suggest_callback(conn, cb, profile)
    elif data.startswith("meal_del:") or data.startswith("meal_edit:"):
        handle_meal_manage_callback(conn, cb)
    elif data.startswith("fav:"):
        handle_fav_callback(conn, cb)
    elif data.startswith("relog:"):
        handle_relog_callback(conn, cb)
    elif data.startswith("undo:"):
        handle_undo_callback(conn, cb)
    elif data.startswith("water:"):
        handle_water_callback(conn, cb)
    elif data.startswith("prof:"):
        handle_profile_edit_callback(conn, cb, profile)
    elif data.startswith("tz:"):
        handle_timezone_callback(conn, cb, profile)
    elif data.startswith("h:"):
        handle_health_callback(conn, cb, profile)
    elif data.startswith("lang:"):
        handle_language_callback(conn, cb, profile)
    elif data == "noop":
        answer_callback_query(cb["id"])
    else:
        answer_callback_query(cb["id"], "Невідома дія")


# ---------- Meal type selection → analyze → show preview ----------

def handle_meal_type_callback(conn, cb: dict, profile: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    meal_type = data.split(":", 1)[1]

    if meal_type == "cancel":
        pop_pending_entry(conn, user_id)  # discard photo/text
        answer_callback_query(cb_id, "Скасовано")
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return

    meal_ua_map = {"breakfast": "сніданок", "lunch": "обід", "dinner": "вечерю", "snack": "перекус"}
    answer_callback_query(cb_id, f"Аналізую твій {meal_ua_map.get(meal_type, meal_type)}…")

    entry = pop_pending_entry(conn, user_id)
    if entry is None:
        send_message(chat_id, PENDING_EXPIRED)
        return
    file_id, text_description = entry

    if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
        return

    send_message(chat_id, ANALYZING_WAIT)

    health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
    personal_ctx = ""
    try:
        personal_ctx = personalization_mod.aliases_prompt_block(conn, user_id)
    except Exception as _px:
        error("personalization_prompt_failed", exc=_px, user_id=user_id)
    analysis, raw = None, ""
    try:
        if file_id:
            try:
                image_bytes = get_file_bytes(file_id)
            except Exception as e:
                print("getFile error:", e, flush=True)
                send_message(chat_id, PHOTO_DOWNLOAD_FAILED)
                return
            analysis, raw = analyze_photo(
                image_bytes,
                health_addendum=health_ctx,
                personalization_addendum=personal_ctx,
            )
        elif text_description:
            analysis, raw = analyze_text(
                text_description,
                health_addendum=health_ctx,
                personalization_addendum=personal_ctx,
            )
        else:
            send_message(chat_id, PENDING_EXPIRED)
            return
    except Exception as e:
        print("analysis error:", e, flush=True)
        send_message(chat_id, TEXT_ANALYSIS_FAILED if text_description else PHOTO_ANALYSIS_FAILED)
        return

    _send_analysis_preview(
        conn, chat_id, user_id, meal_type, analysis,
        photo_file_id=file_id, text_description=text_description, raw=raw,
    )


def _send_analysis_preview(
    conn,
    chat_id: int,
    user_id: int,
    meal_type: str,
    analysis: dict,
    photo_file_id: str | None,
    text_description: str | None,
    raw: str,
) -> None:
    """F-6: dispatch to alternates picker (when ambiguous) or normal preview.

    Single source of truth for "show the user what we got and ask them to
    confirm". Persists pending state in either branch so the moderation /
    pick callbacks can find it.
    """
    candidates = normalize_candidates(analysis)
    if is_ambiguous(candidates):
        save_pending_analysis(
            conn, user_id, meal_type, analysis,
            photo_file_id, text_description, raw, candidates=candidates,
        )
        send_message(
            chat_id,
            format_alternates_intro(meal_type, candidates),
            reply_markup=alternates_keyboard(candidates),
        )
        return
    save_pending_analysis(
        conn, user_id, meal_type, analysis,
        photo_file_id, text_description, raw,
    )
    send_message(
        chat_id,
        format_meal_preview(meal_type, analysis),
        reply_markup=moderation_keyboard(),
    )


# ---------- Moderation: Accept / Recalculate / Manual ----------

def handle_moderation_callback(conn, cb: dict, profile: dict) -> None:
    cb_id = cb["id"]
    action = cb["data"].split(":", 1)[1]
    user_id = cb["from"]["id"]
    first_name = cb["from"].get("first_name")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    if action == "accept":
        answer_callback_query(cb_id, "✅ Записую!")
        pending = pop_pending_analysis(conn, user_id)
        if not pending:
            send_message(chat_id, PENDING_EXPIRED)
            return
        analysis = pending["analysis"]
        meal_id = save_meal(conn, user_id, pending["meal_type"], analysis, pending["photo_file_id"] or "", pending["raw_response"])
        upsert_daily_log_from_meal(conn, user_id, analysis)
        # F-4: bump engagement streak using the user's local "today".
        try:
            update_streak_for_meal(conn, user_id, today_str_user(profile))
        except Exception as _streak_exc:  # never break the meal-save UX
            error("streak_update_failed", exc=_streak_exc, user_id=user_id)
        # F-7: EWMA-update the user's "usual" portion for this dish.
        try:
            personalization_mod.upsert_alias_from_meal(conn, user_id, analysis)
        except Exception as _alias_exc:
            error("upsert_alias_failed", exc=_alias_exc, user_id=user_id)
        today_log = get_today_log(conn, user_id)
        cal_target = profile.get("daily_calorie_target") or 2000
        send_message(
            chat_id,
            format_meal_logged(pending["meal_type"], analysis, today_log, cal_target, first_name),
            reply_markup=meal_logged_actions_keyboard(meal_id, is_fav=False),
        )

    elif action == "recalc":
        answer_callback_query(cb_id, "🔄 Перераховую…")
        pending = get_pending_analysis(conn, user_id)
        if not pending:
            send_message(chat_id, PENDING_EXPIRED)
            return
        if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
            return
        send_message(chat_id, RECALC_WAIT)

        health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
        personal_ctx = ""
        try:
            personal_ctx = personalization_mod.aliases_prompt_block(conn, user_id)
        except Exception as _px:
            error("personalization_prompt_failed", exc=_px, user_id=user_id)
        try:
            if pending["photo_file_id"]:
                image_bytes = get_file_bytes(pending["photo_file_id"])
                analysis, raw = analyze_photo(
                    image_bytes,
                    retry_prompt=RECALC_PROMPT,
                    health_addendum=health_ctx,
                    personalization_addendum=personal_ctx,
                )
            elif pending["text_description"]:
                analysis, raw = analyze_text(
                    pending["text_description"],
                    retry_prompt=RECALC_PROMPT,
                    health_addendum=health_ctx,
                    personalization_addendum=personal_ctx,
                )
            else:
                send_message(chat_id, PENDING_EXPIRED)
                return
        except Exception as e:
            print("recalc error:", e, flush=True)
            send_message(chat_id, PHOTO_ANALYSIS_FAILED)
            return

        # F-7: recalc that produced a *meaningfully different* analysis IS a
        # correction. Skip recording when the model returned the same numbers.
        try:
            original = pending.get("analysis") or {}
            old_kcal = float((original.get("nutrition") or {}).get("calories") or 0)
            new_kcal = float((analysis.get("nutrition")  or {}).get("calories") or 0)
            if original and abs(new_kcal - old_kcal) > max(20.0, 0.05 * old_kcal):
                personalization_mod.record_correction(
                    conn, user_id, source="recalc",
                    original=original, corrected=analysis,
                )
        except Exception as _cx:
            error("record_correction_failed", exc=_cx, user_id=user_id)

        _send_analysis_preview(
            conn, chat_id, user_id, pending["meal_type"], analysis,
            photo_file_id=pending["photo_file_id"],
            text_description=pending["text_description"],
            raw=raw,
        )

    elif action == "manual":
        answer_callback_query(cb_id, "✏️ Чекаю на текст")
        pending = get_pending_analysis(conn, user_id)
        if not pending:
            send_message(chat_id, PENDING_EXPIRED)
            return
        set_awaiting_manual(conn, user_id)
        send_message(chat_id, MANUAL_INPUT_PROMPT, reply_markup=cancel_only_keyboard())

    elif action == "cancel":
        answer_callback_query(cb_id, "Скасовано")
        pop_pending_analysis(conn, user_id)
        pop_pending_entry(conn, user_id)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())


def handle_manual_text_input(conn, message: dict, text: str, pending: dict, profile: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
        return

    send_message(chat_id, ANALYZING_WAIT)

    health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
    personal_ctx = ""
    try:
        personal_ctx = personalization_mod.aliases_prompt_block(conn, user_id)
    except Exception as _px:
        error("personalization_prompt_failed", exc=_px, user_id=user_id)
    try:
        analysis, raw = analyze_text(
            text,
            health_addendum=health_ctx,
            personalization_addendum=personal_ctx,
        )
    except Exception as e:
        print("manual text analysis error:", e, flush=True)
        send_message(chat_id, TEXT_ANALYSIS_FAILED)
        return

    # F-7: a manual text override after a photo IS a correction. Record it
    # for the audit trail + future alias derivation.
    try:
        original = pending.get("analysis") or {}
        if original:  # only record when there was a previous analysis to correct
            personalization_mod.record_correction(
                conn, user_id, source="manual",
                original=original, corrected=analysis,
            )
    except Exception as _cx:
        error("record_correction_failed", exc=_cx, user_id=user_id)

    _send_analysis_preview(
        conn, chat_id, user_id, pending["meal_type"], analysis,
        photo_file_id=pending["photo_file_id"],
        text_description=text,
        raw=raw,
    )


def handle_alternates_pick(conn, cb: dict, profile: dict) -> None:
    """F-6: user tapped one of the 1-3 numbered alternate buttons.

    Behaviour:
      1. Pull the chosen ``top_guesses`` candidate from ``pending.candidates``.
      2. Promote it to a full analysis dict (inheriting ingredients/portion
         from the original whenever the picked candidate is the top guess).
      3. Save it as the new pending analysis WITHOUT the candidates list, so
         subsequent moderation taps see a normal single-result preview.
      4. Send the standard meal preview + moderation keyboard.
    """
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    try:
        idx = int(cb["data"].split(":", 1)[1])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, "Невідомий варіант")
        return

    pending = get_pending_analysis(conn, user_id)
    if not pending:
        answer_callback_query(cb_id)
        send_message(chat_id, PENDING_EXPIRED)
        return

    candidates = pending.get("candidates") or []
    if idx < 0 or idx >= len(candidates):
        answer_callback_query(cb_id, "Варіант недоступний")
        return

    chosen = candidates[idx]
    answer_callback_query(cb_id, f"✓ {chosen.get('name', '')[:40]}")

    new_analysis = candidate_to_analysis(chosen, base=pending.get("analysis"))

    # F-7: picking a non-top alternate is a correction signal — the model's
    # top guess was wrong. idx == 0 means the top stays; skip recording.
    if idx > 0:
        try:
            personalization_mod.record_correction(
                conn, user_id, source="pick_alt",
                original=pending.get("analysis") or {}, corrected=new_analysis,
            )
        except Exception as _cx:
            error("record_correction_failed", exc=_cx, user_id=user_id)

    save_pending_analysis(
        conn, user_id, pending["meal_type"], new_analysis,
        pending.get("photo_file_id"), pending.get("text_description"),
        pending.get("raw_response", ""),
        # Drop candidates so the next "Прийняти" goes through the normal path.
        candidates=None,
    )
    send_message(
        chat_id,
        format_meal_preview(pending["meal_type"], new_analysis),
        reply_markup=moderation_keyboard(),
    )


def _save_barcode_meal(
    conn,
    chat_id: int,
    user_id: int,
    first_name: str | None,
    profile: dict,
    pending: dict,
    grams: float,
) -> None:
    """F-8: turn a pending barcode lookup + chosen grams into a logged meal.

    Reuses the standard save_meal → daily-log → streak → alias pipeline so
    the barcode entry behaves identically to a photo-analyzed meal.
    """
    pseudo = pending.get("analysis") or {}
    if pseudo.get("_pending_kind") != "barcode":
        send_message(chat_id, BARCODE_PENDING_EXPIRED)
        return

    product = {
        "ean":            pseudo.get("ean", ""),
        "name":           pseudo.get("name", ""),
        "brand":          pseudo.get("brand", ""),
        "per_100g":       pseudo.get("per_100g", {}) or {},
        "serving_size_g": pseudo.get("serving_size_g"),
    }
    analysis = off_mod.product_to_analysis(product, grams)

    meal_id = save_meal(
        conn, user_id, pending["meal_type"], analysis,
        photo_file_id="", raw_response=pending.get("raw_response", ""),
    )
    upsert_daily_log_from_meal(conn, user_id, analysis)
    try:
        update_streak_for_meal(conn, user_id, today_str_user(profile))
    except Exception as _streak_exc:
        error("streak_update_failed", exc=_streak_exc, user_id=user_id)
    try:
        personalization_mod.upsert_alias_from_meal(conn, user_id, analysis)
    except Exception as _alias_exc:
        error("upsert_alias_failed", exc=_alias_exc, user_id=user_id)

    pop_pending_analysis(conn, user_id)
    set_awaiting_input(conn, user_id, None)

    today_log = get_today_log(conn, user_id)
    cal_target = profile.get("daily_calorie_target") or 2000
    send_message(
        chat_id,
        format_meal_logged(pending["meal_type"], analysis, today_log, cal_target, first_name),
        reply_markup=meal_logged_actions_keyboard(meal_id, is_fav=False),
    )


def handle_barcode_callback(conn, cb: dict, profile: dict) -> None:
    """F-8: portion-picker buttons under a barcode-found message.

    Callback shapes:
        barcode:g:<int>      — grams (one of the preset chips or serving size)
        barcode:g:custom     — prompt the user to type a number
        barcode:manual       — fallback for when the camera path doesn't work:
                               ask the user to type the EAN digits in chat
        barcode:cancel       — clear pending + go back to main menu
    """
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    first_name = cb["from"].get("first_name")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb["data"]

    if data == "barcode:cancel":
        answer_callback_query(cb_id, "Скасовано")
        pop_pending_analysis(conn, user_id)
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return

    # Manual EAN entry — fallback for devices where the Mini App camera
    # doesn't work (older iOS, denied camera permission, etc.).
    if data == "barcode:manual":
        answer_callback_query(cb_id, "✏️ Чекаю на цифри")
        set_awaiting_input(conn, user_id, "barcode_manual")
        send_message(chat_id, BARCODE_MANUAL_PROMPT)
        return

    if data == "barcode:g:custom":
        answer_callback_query(cb_id, "✏️ Чекаю на грами")
        set_awaiting_input(conn, user_id, "barcode_grams")
        send_message(chat_id, BARCODE_GRAMS_PROMPT)
        return

    if data.startswith("barcode:g:"):
        try:
            grams = int(data.split(":", 2)[2])
        except (ValueError, IndexError):
            answer_callback_query(cb_id, "Неправильна кількість")
            return
        if not (1 <= grams <= 5000):
            answer_callback_query(cb_id, "Неправильна кількість")
            return
        pending = get_pending_analysis(conn, user_id)
        if not pending:
            answer_callback_query(cb_id)
            send_message(chat_id, BARCODE_PENDING_EXPIRED)
            return
        answer_callback_query(cb_id, f"{grams}г · записую")
        _save_barcode_meal(conn, chat_id, user_id, first_name, profile, pending, float(grams))
        return

    answer_callback_query(cb_id, "Невідома дія")


def handle_menu_photo(conn, message: dict, profile: dict) -> None:
    """F-9: user is in /menu mode and sent a photo — run menu OCR.

    For v1 we accept a single photo per /menu invocation. Multi-photo
    (multi-page menus) is deferred — most users will scan a single page
    and the prompt is already capped at 25 dishes.
    """
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    if not _enforce_quota(conn, chat_id, user_id, "menu_ocr"):
        # Clear the awaiting state so the user isn't trapped in menu mode.
        set_awaiting_input(conn, user_id, None)
        return

    photos = message.get("photo") or []
    if not photos:
        send_message(chat_id, MENU_OCR_FAILED)
        return

    # Telegram sends multiple resolutions — pick the largest (last entry).
    largest = photos[-1]
    file_id = largest.get("file_id")
    if not file_id:
        send_message(chat_id, MENU_OCR_FAILED)
        return

    send_message(chat_id, "🔎 Читаю меню… це може зайняти 5-10 секунд.")

    try:
        image_bytes = get_file_bytes(file_id)
    except Exception as e:
        error("menu_photo_download_failed", exc=e, user_id=user_id)
        send_message(chat_id, MENU_OCR_FAILED)
        set_awaiting_input(conn, user_id, None)
        return

    try:
        dishes, _raw = analyze_menu([image_bytes])
    except Exception as e:
        error("menu_analyze_failed", exc=e, user_id=user_id)
        send_message(chat_id, MENU_OCR_FAILED)
        set_awaiting_input(conn, user_id, None)
        return

    set_awaiting_input(conn, user_id, None)

    if not dishes:
        send_message(chat_id, MENU_NO_DISHES, reply_markup=main_menu_keyboard())
        return

    try:
        save_menu_ocr_result(conn, user_id, dishes)
    except Exception as e:
        error("menu_save_result_failed", exc=e, user_id=user_id)
        send_message(chat_id, MENU_OCR_FAILED)
        return

    # Build the results message: header + one line per dish.
    lines = [format_menu_dishes_intro(len(dishes))]
    for d in dishes[:15]:  # cap message length; keyboard buttons go up to 25
        lines.append(format_menu_dish_row(d))
    send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=menu_log_keyboard(dishes),
    )


def handle_menu_callback(conn, cb: dict, profile: dict) -> None:
    """F-9: user tapped one of the menu OCR result buttons.

    ``menu:log:<idx>`` → start the standard meal-confirmation flow with the
    dish name pre-filled (no extra GPT call — we already have macros).
    ``menu:cancel`` → close the menu list.
    """
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    first_name = cb["from"].get("first_name")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb["data"]

    if data == "menu:cancel":
        answer_callback_query(cb_id, "Закрив")
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return

    if not data.startswith("menu:log:"):
        answer_callback_query(cb_id, "Невідома дія")
        return

    try:
        idx = int(data.split(":", 2)[2])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, "Поза діапазоном")
        return

    dishes = get_menu_ocr_result(conn, user_id) or []
    if not dishes or idx < 0 or idx >= len(dishes):
        answer_callback_query(cb_id)
        send_message(chat_id, MENU_PENDING_EXPIRED)
        return

    chosen = dishes[idx]
    answer_callback_query(cb_id, f"➕ {chosen.get('name', '')[:40]}")

    # Promote the menu dish into a standard analysis dict and route through
    # _send_analysis_preview so the user gets the normal accept/recalc
    # moderation card. We pass NO photo_file_id and a synthetic
    # text_description (so /recalc has something sensible to retry against).
    portion_note = chosen.get("portion_note", "")
    analysis = {
        "dish_name":         chosen["name"],
        "description":       chosen["name"],
        "estimated_portion": portion_note or "ресторанна порція",
        "portion_reasoning": f"Меню OCR · впевненість {int(round(chosen.get('confidence', 0) * 100))}%",
        "ingredients":       [],
        "allergen_flags":    [],
        "crohn_flags":       [],
        "nutrition": {
            "calories":  float(chosen.get("calories")  or 0),
            "protein_g": float(chosen.get("protein_g") or 0),
            "carbs_g":   float(chosen.get("carbs_g")   or 0),
            "fat_g":     float(chosen.get("fat_g")     or 0),
            "fiber_g":   0.0,
            "sugar_g":   0.0,
        },
        "glycemic_index":     {"level": "", "note": ""},
        "overall_assessment": "",
        "_source": {"kind": "menu_ocr"},
    }

    meal_type = _meal_type_by_local_hour(profile)
    _send_analysis_preview(
        conn, chat_id, user_id, meal_type, analysis,
        photo_file_id=None,
        text_description=chosen["name"],
        raw="",
    )


# ---------- F-10: 3-day meal plans ----------

def _build_and_send_plan(
    conn,
    chat_id: int,
    user_id: int,
    profile: dict,
    pantry: str,
) -> None:
    """Generate a plan, persist it, send the day-1 message + day keyboard.

    Pulled out so both ``handle_plan_pantry_input`` (typed list) and the
    "Без списку" callback can share the heavy lifting.
    """
    if not _enforce_quota(conn, chat_id, user_id, "plan_generate"):
        set_awaiting_input(conn, user_id, None)
        return

    set_awaiting_input(conn, user_id, None)
    send_message(chat_id, PLAN_GENERATING)

    cal_target = profile.get("daily_calorie_target") or 2000
    weight_kg = profile.get("weight_kg")
    goal = profile.get("goal") or "maintain"
    if weight_kg:
        macros = macro_gram_targets_from_profile(weight_kg, goal)
    else:
        macros = macro_gram_targets(cal_target)

    today_log = get_today_log(conn, user_id) or {}
    remaining = {
        "calories": max(0, cal_target           - int(today_log.get("calories", 0))),
        "protein":  max(0, int(macros["protein"]) - int(today_log.get("protein", 0))),
        "fat":      max(0, int(macros["fat"])     - int(today_log.get("fat", 0))),
        "carbs":    max(0, int(macros["carbs"])   - int(today_log.get("carbs", 0))),
    }
    health_ctx = ""
    try:
        health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
    except Exception as _hx:
        error("plan_health_addendum_failed", exc=_hx, user_id=user_id)

    try:
        plan = mealplan_mod.generate_meal_plan(
            cal_target=cal_target,
            p_target=int(macros["protein"]),
            c_target=int(macros["carbs"]),
            f_target=int(macros["fat"]),
            remaining=remaining,
            goal=goal,
            pantry=pantry,
            health_addendum=health_ctx,
        )
    except Exception as e:
        error("plan_generate_failed", exc=e, user_id=user_id)
        send_message(chat_id, PLAN_FAILED, reply_markup=main_menu_keyboard())
        return

    try:
        plan_id = save_meal_plan(conn, user_id, plan)
    except Exception as e:
        error("plan_save_failed", exc=e, user_id=user_id)
        send_message(chat_id, PLAN_FAILED, reply_markup=main_menu_keyboard())
        return

    _send_plan_day(chat_id, plan_id, plan, day_idx=0)


def _send_plan_day(chat_id: int, plan_id: int, plan: dict, day_idx: int) -> None:
    """Render one day of a saved plan + the per-day inline keyboard."""
    days = plan.get("days") or []
    if day_idx < 0 or day_idx >= len(days):
        return
    day = days[day_idx]
    body = format_meal_plan_day(day, day_idx)
    if day_idx == 0 and plan.get("notes"):
        body = PLAN_HEADER_NOTES.format(notes=plan["notes"]) + body
    send_message(chat_id, body, reply_markup=plan_day_keyboard(plan_id, day_idx, day))


def handle_plan_pantry_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
    profile: dict,
) -> None:
    """Capture optional pantry list and trigger plan generation."""
    cleaned = text.strip()
    if cleaned.lower() in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return
    if len(cleaned) > 200:
        send_message(chat_id, PLAN_PANTRY_TOO_LONG)
        return
    _build_and_send_plan(conn, chat_id, user_id, profile, pantry=cleaned)


def handle_plan_callback(conn, cb: dict, profile: dict) -> None:
    """F-10: callbacks under /plan and per-day messages.

    Shapes:
        plan:nopantry                — "skip pantry list, just generate"
        plan:cancel                  — close
        plan:view:<plan_id>:<day>    — paginate to a different day
        plan:log:<plan_id>:<day>:<slot> — add a slot's meal to today's log
    """
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    first_name = cb["from"].get("first_name")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb["data"]

    if data == "plan:cancel":
        answer_callback_query(cb_id, "Закрив")
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return

    if data == "plan:nopantry":
        answer_callback_query(cb_id, "🍳 Готую…")
        _build_and_send_plan(conn, chat_id, user_id, profile, pantry="")
        return

    parts = data.split(":")
    # Both view + log forms have plan_id at index 2 and day at index 3.
    if len(parts) < 4:
        answer_callback_query(cb_id, "Невідома дія")
        return

    try:
        plan_id = int(parts[2])
        day_idx = int(parts[3])
    except ValueError:
        answer_callback_query(cb_id, "Невідома дія")
        return

    plan = get_meal_plan(conn, plan_id, user_id)
    if not plan:
        answer_callback_query(cb_id)
        send_message(chat_id, PLAN_FAILED)
        return

    if data.startswith("plan:view:"):
        answer_callback_query(cb_id)
        _send_plan_day(chat_id, plan_id, plan, day_idx)
        return

    if data.startswith("plan:log:"):
        if len(parts) < 5:
            answer_callback_query(cb_id, "Помилка")
            return
        slot_key = parts[4]
        days = plan.get("days") or []
        if day_idx < 0 or day_idx >= len(days):
            answer_callback_query(cb_id, "Поза діапазоном")
            return
        slot = (days[day_idx].get("slots") or {}).get(slot_key)
        if not slot:
            answer_callback_query(cb_id, "Прийом їжі недоступний")
            return
        answer_callback_query(cb_id, f"➕ {slot.get('name', '')[:32]}")
        analysis = mealplan_mod.slot_to_analysis(slot)
        meal_type = slot_key  # planner slots match the meal_type enum exactly
        # Route through standard preview so the user can ✏️ edit / 🔄 recalc
        # before commiting.
        _send_analysis_preview(
            conn, chat_id, user_id, meal_type, analysis,
            photo_file_id=None,
            text_description=slot.get("name", ""),
            raw="",
        )
        return

    answer_callback_query(cb_id, "Невідома дія")


# ---------- F-11: /suggest_meal extensions (fridge + variation) ----------

def _run_suggest_meal(
    conn,
    chat_id: int,
    user_id: int,
    profile: dict,
    pantry: str = "",
    extra_hint: str = "",
) -> None:
    """Shared body for /suggest_meal, the fridge handler, and "інша версія".

    Always reuses the existing ``suggest`` quota counter — fridge / variation
    are not separate buckets to keep the cost surface predictable.
    """
    if not _enforce_quota(conn, chat_id, user_id, "suggest"):
        return
    log = get_today_log(conn, user_id)
    meals = get_meals_for_day(conn, user_id, log["date"])
    send_message(chat_id, SUGGEST_THINKING)
    health_ctx = ""
    try:
        health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
    except Exception as _hx:
        error("suggest_health_addendum_failed", exc=_hx, user_id=user_id)
    try:
        recipe = suggest_meal(
            log, meals, profile,
            pantry=pantry,
            extra_hint=extra_hint,
            health_addendum=health_ctx,
        )
    except Exception as e:
        print("suggest error:", e, flush=True)
        send_message(chat_id, SUGGEST_FAILED, reply_markup=main_menu_keyboard())
        return
    send_message(chat_id, recipe, reply_markup=suggest_followup_keyboard())


def handle_fridge_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
    profile: dict,
) -> None:
    """F-11: user typed their fridge ingredients — generate a recipe with them."""
    cleaned = text.strip()
    if cleaned.lower() in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return
    if len(cleaned) > 300:
        send_message(chat_id, FRIDGE_TOO_LONG)
        return
    set_awaiting_input(conn, user_id, None)
    _run_suggest_meal(conn, chat_id, user_id, profile, pantry=cleaned, extra_hint="")


def handle_suggest_callback(conn, cb: dict, profile: dict) -> None:
    """F-11: callbacks under /suggest_meal results.

    Shapes:
        suggest:fridge    — set FSM, prompt for ingredients
        suggest:variation — re-run with a "make this different" hint
    """
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb["data"]

    if data == "suggest:fridge":
        answer_callback_query(cb_id, "🛒 Чекаю на список")
        set_awaiting_input(conn, user_id, "fridge_ingredients")
        send_message(chat_id, FRIDGE_PROMPT)
        return

    if data == "suggest:variation":
        answer_callback_query(cb_id, "🔄 Готую іншу")
        _run_suggest_meal(conn, chat_id, user_id, profile,
                          pantry="", extra_hint=SUGGEST_VARIATION_HINT)
        return

    answer_callback_query(cb_id, "Невідома дія")


def _portion_keyboard_for_product(product: dict) -> dict:
    """Inline keyboard mirroring api/barcode.py's portion picker.

    Kept here (not in lib/telegram_helpers.py) to avoid bloating that
    module — only handle_barcode_manual_input needs it.
    """
    serving = product.get("serving_size_g")
    rows = []
    if serving and 5 <= serving <= 5000:
        rows.append([{
            "text": f"📦 Порція: {int(serving)}г",
            "callback_data": f"barcode:g:{int(serving)}",
        }])
    rows.append([
        {"text": "50г",  "callback_data": "barcode:g:50"},
        {"text": "100г", "callback_data": "barcode:g:100"},
        {"text": "150г", "callback_data": "barcode:g:150"},
        {"text": "200г", "callback_data": "barcode:g:200"},
    ])
    rows.append([{"text": "✏️ Інша кількість", "callback_data": "barcode:g:custom"}])
    rows.append([{"text": "❌ Скасувати",       "callback_data": "barcode:cancel"}])
    return {"inline_keyboard": rows}


def handle_barcode_manual_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
    profile: dict,
) -> None:
    """F-8 fallback: user typed an EAN directly instead of using the camera.

    Mirrors the lookup + portion-picker flow in api/barcode.py but skips
    the Mini App and the cookie-jar quota recheck (the same daily quota
    counter under "meal_analysis" already protects this path).
    """
    cleaned = text.strip().replace(" ", "").replace("-", "")
    if cleaned.lower() in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return

    if not off_mod.looks_like_ean(cleaned):
        send_message(chat_id, BARCODE_MANUAL_INVALID)
        return

    if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
        set_awaiting_input(conn, user_id, None)
        return

    set_awaiting_input(conn, user_id, None)

    try:
        product = off_mod.lookup_product(cleaned)
    except Exception as e:
        error("off_lookup_failed", exc=e, ean=cleaned, user_id=user_id)
        send_message(chat_id, BARCODE_LOOKUP_FAILED, reply_markup=main_menu_keyboard())
        return

    if product is None:
        send_message(
            chat_id,
            BARCODE_NOT_FOUND.format(ean=cleaned),
            reply_markup=main_menu_keyboard(),
        )
        return

    # Stash a barcode-pending analysis so the existing barcode:g:* picker
    # callbacks pick it up (same shape as api/barcode.py's _save).
    meal_type = _meal_type_by_local_hour(profile)
    pseudo = {
        "_pending_kind":  "barcode",
        "ean":            product["ean"],
        "name":           product["name"],
        "brand":          product["brand"],
        "per_100g":       product["per_100g"],
        "serving_size_g": product["serving_size_g"],
    }
    try:
        save_pending_analysis(
            conn, user_id, meal_type, pseudo,
            photo_file_id=None, text_description=None,
            raw_response=json.dumps(product, ensure_ascii=False),
        )
    except Exception as e:
        error("barcode_save_pending_failed", exc=e, user_id=user_id)
        send_message(chat_id, BARCODE_LOOKUP_FAILED)
        return

    send_message(
        chat_id,
        BARCODE_FOUND_HEADER.format(
            name=product["name"],
            brand=product["brand"] or "—",
            kcal=int(round(product["per_100g"]["calories"])),
            p=int(round(product["per_100g"]["protein_g"])),
            f=int(round(product["per_100g"]["fat_g"])),
            c=int(round(product["per_100g"]["carbs_g"])),
        ),
        reply_markup=_portion_keyboard_for_product(product),
    )


def handle_barcode_grams_input(
    conn,
    chat_id: int,
    user_id: int,
    first_name: str | None,
    text: str,
    profile: dict,
) -> None:
    """F-8: custom-grams typed reply for a pending barcode lookup."""
    cleaned = text.strip().replace(",", ".").replace("г", "").replace("g", "").strip()
    if cleaned.lower() in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        pop_pending_analysis(conn, user_id)
        send_message(chat_id, MEAL_CANCELLED, reply_markup=main_menu_keyboard())
        return

    grams = _parse_float(cleaned)
    if grams is None or not (1 <= grams <= 5000):
        send_message(chat_id, BARCODE_GRAMS_INVALID)
        return

    pending = get_pending_analysis(conn, user_id)
    if not pending:
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, BARCODE_PENDING_EXPIRED, reply_markup=main_menu_keyboard())
        return

    _save_barcode_meal(conn, chat_id, user_id, first_name, profile, pending, float(grams))


# ---------- Meal management: Delete / Edit ----------

def handle_meal_manage_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    if data.startswith("meal_del:"):
        meal_id = int(data.split(":", 1)[1])
        answer_callback_query(cb_id, "🗑 Видаляю…")
        deleted = delete_meal(conn, meal_id, user_id)
        if not deleted:
            send_message(chat_id, MEAL_NOT_FOUND)
            return
        recalc_daily_log(conn, user_id, deleted["date"])
        send_message(
            chat_id,
            MEAL_DELETED.format(
                dish=_html.escape(deleted["description"][:40], quote=False),
                cal=round(deleted["calories"]),
            ),
        )

    elif data.startswith("meal_edit:"):
        meal_id = int(data.split(":", 1)[1])
        answer_callback_query(cb_id, "✏️ Готуюсь до заміни…")
        deleted = delete_meal(conn, meal_id, user_id)
        if not deleted:
            send_message(chat_id, MEAL_NOT_FOUND)
            return
        recalc_daily_log(conn, user_id, deleted["date"])
        save_pending_analysis(conn, user_id, deleted["meal_type"], {}, None, None, "")
        set_awaiting_manual(conn, user_id, meal_type=deleted["meal_type"])
        send_message(
            chat_id,
            MEAL_EDIT_PROMPT.format(
                dish=_html.escape(deleted["description"][:40], quote=False),
            ),
            reply_markup=cancel_only_keyboard(),
        )


# ---------- Commands ----------

def handle_command(conn, message: dict, text: str, first_name: str | None, profile: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    cal_target = (profile or {}).get("daily_calorie_target") or 2000

    if cmd == "/help":
        send_message(chat_id, help_message(), reply_markup=main_menu_keyboard())
        return

    if cmd == "/profile":
        send_message(chat_id, format_profile(profile), reply_markup=profile_edit_keyboard())
        return

    if cmd == "/today":
        log = get_today_log(conn, user_id)
        streak_row = None
        try:
            streak_row = get_streak(conn, user_id)
        except Exception as _streak_exc:  # don't block /today on streak issues
            error("streak_fetch_failed", exc=_streak_exc, user_id=user_id)
        send_message(
            chat_id,
            format_today_progress(log, cal_target, first_name, profile=profile, streak=streak_row),
            reply_markup=main_menu_keyboard(),
        )
        return

    if cmd == "/streak":
        try:
            streak_row = get_streak(conn, user_id)
        except Exception as _streak_exc:
            error("streak_fetch_failed", exc=_streak_exc, user_id=user_id)
            streak_row = None
        send_message(
            chat_id,
            format_streak_summary(streak_row, first_name),
            reply_markup=main_menu_keyboard(),
        )
        return

    # F-8: open the barcode scanner Mini App.
    if cmd == "/scan":
        send_message(
            chat_id,
            BARCODE_SCAN_INTRO,
            reply_markup=scanner_inline_keyboard(),
        )
        return

    # F-9: restaurant menu OCR — set state + ask for a photo.
    if cmd == "/menu":
        set_awaiting_input(conn, user_id, "menu_photo")
        send_message(chat_id, MENU_PROMPT_INTRO, reply_markup=cancel_only_keyboard())
        return

    # F-10: 3-day meal plan — ask the user for optional pantry items first.
    if cmd == "/plan":
        set_awaiting_input(conn, user_id, "plan_pantry")
        send_message(chat_id, PLAN_INTRO, reply_markup=plan_pantry_keyboard())
        return

    # F-12: shareable PNG recap card on demand.
    if cmd == "/recap":
        try:
            png, caption = build_user_recap(conn, user_id, profile, first_name)
        except Exception as e:
            error("recap_build_failed", exc=e, user_id=user_id)
            send_message(chat_id, "❌ Не зміг скласти recap. Спробуй пізніше.",
                         reply_markup=main_menu_keyboard())
            return
        resp = send_photo(chat_id, png, caption=caption)
        if not resp.get("ok"):
            error("recap_send_failed", user_id=user_id, response=resp)
            send_message(chat_id, "❌ Не зміг відправити картинку. Спробуй пізніше.")
        return

    # F-7: user's learned food aliases (read-only view; auto-built from accepted meals).
    if cmd == "/aliases":
        try:
            aliases = personalization_mod.recent_aliases(conn, user_id, limit=20)
        except Exception as _ax:
            error("aliases_fetch_failed", exc=_ax, user_id=user_id)
            aliases = []
        send_message(
            chat_id,
            format_aliases(aliases, first_name),
            reply_markup=main_menu_keyboard(),
        )
        return

    # F-5: dedicated goals view + weekly-delta editor.
    if cmd == "/goals":
        if not profile:
            send_message(chat_id, GOALS_NO_PROFILE)
            return
        projection = goals_mod.projection_for_profile(profile)
        # Compute actual weekly delta from recent weight history (best-effort).
        actual = None
        status = None
        try:
            history = get_weight_history(conn, user_id, limit=20)
            actual = goals_mod.actual_weekly_delta(history, window_weeks=4)
            if actual is not None:
                status = goals_mod.classify_actual_vs_target(
                    actual, projection.weekly_delta_kg
                )
        except Exception as _gx:  # never block /goals on history fetch
            error("goals_history_failed", exc=_gx, user_id=user_id)
        send_message(
            chat_id,
            format_goals(profile, projection, actual_weekly_delta=actual,
                         status=status, first_name=first_name),
            reply_markup=goals_edit_keyboard(
                has_target=bool(profile.get("target_weight_kg")),
                has_delta=profile.get("weekly_delta_kg") is not None,
            ),
        )
        return

    if cmd == "/yesterday":
        from datetime import timedelta
        y = (now_user(profile) - timedelta(days=1)).strftime("%Y-%m-%d")
        log = get_log_for_date(conn, user_id, y)
        meals = get_meals_for_day(conn, user_id, y)
        send_message(chat_id, format_yesterday(log, meals, cal_target, first_name, profile=profile), reply_markup=main_menu_keyboard())
        return

    if cmd == "/meals":
        log = get_today_log(conn, user_id)
        meals = get_meals_for_day(conn, user_id, log["date"])
        if not meals:
            send_message(chat_id, NO_MEALS_TO_MANAGE)
            return
        macros = macro_gram_targets_from_profile(
            (profile or {}).get("weight_kg"),
            (profile or {}).get("goal") or "maintain",
        )
        send_message(
            chat_id,
            format_meals_list(meals, log=log, daily_cal_target=cal_target, macros=macros),
            reply_markup=meals_list_keyboard(meals),
        )
        return

    if cmd == "/history":
        rows = get_history(conn, user_id, days=7)
        send_message(chat_id, format_history(rows, cal_target), reply_markup=main_menu_keyboard())
        return

    if cmd == "/history_detail":
        if not args:
            send_message(chat_id, HISTORY_USAGE)
            return
        date = args[0]
        meals = get_meals_for_day(conn, user_id, date)
        send_message(chat_id, format_day_detail(date, meals))
        return

    if cmd == "/suggest_meal":
        _run_suggest_meal(conn, chat_id, user_id, profile, pantry="", extra_hint="")
        return

    if cmd == "/ask":
        question = " ".join(args).strip()
        if question:
            handle_ask(conn, user_id, chat_id, question, profile)
        else:
            send_message(
                chat_id,
                ASK_PROMPT,
                reply_markup={"force_reply": True, "selective": True},
            )
        return

    if cmd == "/fav":
        meals = get_favorites(conn, user_id)
        if not meals:
            send_message(chat_id, FAV_EMPTY_LIST, reply_markup=main_menu_keyboard())
            return
        lines = ["⭐ <b>Улюблені страви</b>", ""] + [f"• {format_meal_list_entry(m)}" for m in meals[:20]]
        lines.append("")
        lines.append("👇 Тисни 🔁 щоб записати страву на сьогодні:")
        send_message(chat_id, "\n".join(lines), reply_markup=recent_meals_keyboard(meals, variant="fav"))
        return

    if cmd == "/recent":
        meals = get_recent_meals(conn, user_id, limit=10)
        if not meals:
            send_message(chat_id, RECENT_EMPTY_LIST, reply_markup=main_menu_keyboard())
            return
        lines = ["🕘 <b>Останні страви</b>", ""] + [f"• {format_meal_list_entry(m)}" for m in meals]
        lines.append("")
        lines.append("👇 Тисни 🔁 щоб повторити:")
        send_message(chat_id, "\n".join(lines), reply_markup=recent_meals_keyboard(meals, variant="recent"))
        return

    if cmd == "/timezone":
        if not profile_is_complete(profile):
            send_message(chat_id, TIMEZONE_NOT_ONBOARDED)
            return
        cur = (profile or {}).get("tz") or "Europe/Kyiv"
        send_message(
            chat_id,
            TIMEZONE_PROMPT.format(current=cur),
            reply_markup=tz_keyboard(prefix="tz:set"),
        )
        return

    if cmd == "/health":
        if not profile_is_complete(profile):
            send_message(chat_id, HEALTH_NOT_ONBOARDED)
            return
        _send_health_menu(conn, chat_id, user_id)
        return

    if cmd == "/language":
        lang = (profile or {}).get("lang") or "en"
        if not profile_is_complete(profile):
            send_message(chat_id, i18n_mod.t("language_not_onboarded", locale=lang))
            return
        cur_label = i18n_mod.t(f"lang_label_{lang}", locale=lang)
        send_message(
            chat_id,
            i18n_mod.t("language_prompt", locale=lang, current=cur_label),
            reply_markup=language_keyboard(),
        )
        return

    if cmd == "/water":
        total = get_water_today(conn, user_id)
        target = get_water_target(conn, user_id)
        send_message(chat_id, format_water(total, target), reply_markup=water_keyboard())
        return

    send_message(chat_id, UNKNOWN_COMMAND)


# ---------- Favorites / Recent / Undo callbacks ----------

def build_user_recap(
    conn,
    user_id: int,
    profile: dict | None,
    first_name: str | None,
) -> tuple[bytes, str]:
    """F-12: assemble the weekly recap PNG + caption for a user.

    Returns ``(png_bytes, caption)``. Caller decides whether to send via
    /recap (interactive) or the Sunday cron (push).
    """
    from datetime import date as _date, timedelta as _td

    # Use the user's local date to define the 7-day window when we have their tz.
    try:
        end_date_obj = now_user(profile).date() if profile else _date.today()
    except Exception:
        end_date_obj = _date.today()
    start_date_obj = end_date_obj - _td(days=6)

    meals_7d = get_meals_in_range(
        conn, user_id,
        start_date_obj.isoformat(),
        end_date_obj.isoformat(),
    )
    weights_recent = get_weight_history(conn, user_id, limit=20)
    try:
        streak_row = get_streak(conn, user_id)
    except Exception:
        streak_row = None

    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=meals_7d,
        weight_history_recent=weights_recent,
        streak_row=streak_row,
        end_date=end_date_obj,
    )
    png = recap_mod.render_recap_png(stats, first_name=first_name)

    # Caption ends with a sharing nudge — Telegram's "Forward / Share" button
    # does the rest of the heavy lifting (built-in virality).
    caption = (
        f"🔥 <b>Мій тиждень з KusWise</b>\n"
        f"Серія: {stats['streak']} · "
        f"Середньо: {stats['avg_kcal']} ккал/день · "
        f"Залогованих днів: {stats['days_logged']}/7\n\n"
        f"<i>Поділись цим, якщо хочеш — натисни на картинку → 📤 Поділитись.</i>"
    )
    return png, caption


def _meal_type_by_local_hour(profile: dict | None = None) -> str:
    """Pick a default meal type from the user's local clock.

    When ``profile`` carries a ``tz`` field, use it; otherwise fall back to
    Europe/Kyiv via ``now_user(None)``.
    """
    h = now_user(profile).hour
    if 6 <= h < 11:
        return "breakfast"
    if 11 <= h < 16:
        return "lunch"
    if 16 <= h < 21:
        return "dinner"
    return "snack"


_MEAL_TYPE_UA = {"breakfast": "сніданок", "lunch": "обід", "dinner": "вечерю", "snack": "перекус"}


def handle_fav_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")

    parts = data.split(":")
    if len(parts) != 3:
        answer_callback_query(cb_id, "Помилка")
        return
    try:
        meal_id = int(parts[1])
    except ValueError:
        answer_callback_query(cb_id, "Помилка")
        return
    target_state = parts[2] == "1"
    ok = set_favorite(conn, meal_id, user_id, target_state)
    if not ok:
        answer_callback_query(cb_id, "Страву не знайдено")
        return
    answer_callback_query(cb_id, FAV_ADDED if target_state else FAV_REMOVED)
    if message_id:
        try:
            edit_message_reply_markup(
                chat_id, message_id,
                meal_logged_actions_keyboard(meal_id, is_fav=target_state),
            )
        except Exception as e:
            print("edit_reply_markup error:", e, flush=True)


def handle_relog_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    try:
        meal_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, "Помилка")
        return

    src = get_meal_by_id(conn, meal_id, user_id)
    if not src:
        answer_callback_query(cb_id, "Страву не знайдено")
        return

    profile = get_profile(conn, user_id) or {}
    meal_type = _meal_type_by_local_hour(profile)
    new_id = clone_meal_for_today(conn, meal_id, user_id, meal_type)
    if not new_id:
        answer_callback_query(cb_id, "Не вдалося")
        send_message(chat_id, RELOG_FAILED)
        return
    answer_callback_query(cb_id, "✅ Записав")
    send_message(
        chat_id,
        RELOG_DONE.format(
            dish=_html.escape((src.get("description") or "страву")[:40], quote=False),
            meal_type=_MEAL_TYPE_UA.get(meal_type, meal_type),
        ),
        reply_markup=undo_relog_keyboard(new_id),
    )


def handle_undo_callback(conn, cb: dict) -> None:
    from datetime import datetime, timezone, timedelta
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")

    try:
        meal_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, "Помилка")
        return

    meal = get_meal_by_id(conn, meal_id, user_id)
    if not meal:
        answer_callback_query(cb_id, "Уже немає")
        return

    # 10-min TTL
    try:
        created = datetime.fromisoformat(meal["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=10):
            answer_callback_query(cb_id, "Пізно")
            send_message(chat_id, UNDO_EXPIRED)
            return
    except Exception:
        pass

    deleted = delete_meal(conn, meal_id, user_id)
    if not deleted:
        answer_callback_query(cb_id, "Не знайдено")
        return
    recalc_daily_log(conn, user_id, deleted["date"])
    answer_callback_query(cb_id, UNDO_DONE)
    if message_id:
        try:
            safe_desc = _html.escape(deleted["description"][:40], quote=False)
            edit_message_text(chat_id, message_id, f"↩️ Скасовано: {safe_desc}")
        except Exception:
            pass


# ---------- Water callbacks ----------

def handle_water_quickadd(conn, chat_id: int, user_id: int, amount_ml: int) -> None:
    total = add_water(conn, user_id, amount_ml)
    target = get_water_target(conn, user_id)
    send_message(chat_id, format_water(total, target), reply_markup=water_keyboard())


def handle_water_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")

    parts = data.split(":")
    # Forms: water:add:<ml>, water:undo, water:goal, water:goal:set:<ml>, water:back
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "add" and len(parts) == 3:
        try:
            ml = int(parts[2])
        except ValueError:
            answer_callback_query(cb_id, "Помилка")
            return
        if ml not in (200, 250, 300, 500, 750):
            answer_callback_query(cb_id, "Невірно")
            return
        total = add_water(conn, user_id, ml)
        target = get_water_target(conn, user_id)
        answer_callback_query(cb_id, f"+{ml} мл")
        if message_id:
            edit_message_text(chat_id, message_id, format_water(total, target), reply_markup=water_keyboard())
        else:
            send_message(chat_id, format_water(total, target), reply_markup=water_keyboard())
        return

    if sub == "undo":
        new_total = remove_last_water_today(conn, user_id)
        if new_total is None:
            answer_callback_query(cb_id, WATER_UNDO_EMPTY)
            return
        target = get_water_target(conn, user_id)
        answer_callback_query(cb_id, "Відкотив")
        if message_id:
            edit_message_text(chat_id, message_id, format_water(new_total, target), reply_markup=water_keyboard())
        return

    if sub == "goal" and len(parts) == 2:
        answer_callback_query(cb_id)
        if message_id:
            edit_message_text(chat_id, message_id, WATER_GOAL_PROMPT, reply_markup=water_goal_keyboard())
        else:
            send_message(chat_id, WATER_GOAL_PROMPT, reply_markup=water_goal_keyboard())
        return

    if sub == "goal" and len(parts) == 4 and parts[2] == "set":
        try:
            ml = int(parts[3])
        except ValueError:
            answer_callback_query(cb_id, "Помилка")
            return
        set_water_target(conn, user_id, ml, overridden=True)
        answer_callback_query(cb_id, WATER_GOAL_SAVED.format(target=ml))
        total = get_water_today(conn, user_id)
        if message_id:
            edit_message_text(chat_id, message_id, format_water(total, ml), reply_markup=water_keyboard())
        return

    if sub == "back":
        answer_callback_query(cb_id)
        total = get_water_today(conn, user_id)
        target = get_water_target(conn, user_id)
        if message_id:
            edit_message_text(chat_id, message_id, format_water(total, target), reply_markup=water_keyboard())
        return

    answer_callback_query(cb_id, "Невідома дія")


# ---------- /ask chat mode ----------

def handle_ask(conn, user_id: int, chat_id: int, question: str, profile: dict) -> None:
    if not _enforce_quota(conn, chat_id, user_id, "ask"):
        return
    send_message(chat_id, ASK_THINKING)
    try:
        today_log = get_today_log(conn, user_id)
        today_meals = get_meals_for_day(conn, user_id, today_log["date"])
        history = get_chat_history(conn, user_id, limit=10, minutes=60)
        answer = ask_chat(question, history, today_log, today_meals, profile)
    except Exception as e:
        print("ask_chat error:", traceback.format_exc(), flush=True)
        send_message(chat_id, ASK_ERROR, reply_markup=main_menu_keyboard())
        return

    append_chat_message(conn, user_id, "user", question)
    append_chat_message(conn, user_id, "assistant", answer)
    send_message(chat_id, answer, reply_markup=main_menu_keyboard())


# ---------- Weight / goal edit ----------

_GOAL_LABEL_UA = {
    "lose": "🔥 Схуднути",
    "maintain": "⚖️ Підтримувати вагу",
    "gain": "💪 Набрати м'язи",
}


def _apply_new_weight(conn, user_id: int, new_weight: float, goal: str, source: str) -> dict:
    """Persist a weight change: history + profile + calorie + water recompute."""
    insert_weight(conn, user_id, new_weight, source=source)
    new_cal = calorie_target_from_profile(new_weight, goal)
    update_profile(
        conn, user_id,
        weight_kg=float(new_weight),
        daily_calorie_target=new_cal,
    )
    # Auto-recompute water target (30 ml/kg) unless user has manually overridden.
    try:
        upsert_water_target_from_profile(conn, user_id, float(new_weight))
    except Exception as e:
        print("water recompute after weight change failed:", e, flush=True)
    return {
        "calories": new_cal,
        "macros": macro_gram_targets_from_profile(new_weight, goal),
    }


def _weight_change_reply(
    new_weight: float,
    old_weight: float | None,
    new_cal: int,
    macros: dict,
    goal: str | None = None,
    target_weight: float | None = None,
) -> str:
    lines = []
    if old_weight:
        delta_kg = float(new_weight) - float(old_weight)
        delta_g = round(delta_kg * 1000)
        if delta_g == 0:
            delta_txt = "без змін"
        else:
            delta_txt = f"{'+' if delta_g > 0 else ''}{delta_g}г за тиждень"
        lines.append(f"✅ Записав: <b>{new_weight} кг</b> ({delta_txt}).")
    else:
        lines.append(f"✅ Записав: <b>{new_weight} кг</b>.")

    if target_weight and goal in ("lose", "gain"):
        delta = float(new_weight) - float(target_weight)  # + = need to lose, − = need to gain
        if goal == "lose":
            togo = max(0.0, delta)
            reached = togo <= 0.05
            tail = "досягнуто 🎉" if reached else f"залишилось −{togo:.1f} кг"
        else:
            togo = max(0.0, -delta)
            reached = togo <= 0.05
            tail = "досягнуто 🎉" if reached else f"залишилось +{togo:.1f} кг"
        lines.append(f"🏁 Ціль <b>{target_weight} кг</b> — {tail}")

    lines.append(f"🎯 Нова норма калорій: <b>{new_cal} ккал/день</b>")
    lines.append(
        f"🥩 Білки {macros['protein']}г · 🍚 Вуглеводи {macros['carbs']}г · 🧈 Жири {macros['fat']}г"
    )
    return "\n".join(lines)


def handle_weight_input(
    conn,
    chat_id: int,
    user_id: int,
    first_name: str | None,
    text: str,
    profile: dict,
) -> None:
    """Process a weight reply from either the Monday check-in or /profile edit."""
    cleaned = text.strip()
    if cleaned.lower() in ("/skip", "skip"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, WEIGHT_CHECKIN_SKIPPED, reply_markup=main_menu_keyboard())
        return

    new_weight = _parse_float(cleaned)
    if new_weight is None:
        send_message(chat_id, WEIGHT_NOT_A_NUMBER)
        return
    if not (30 <= new_weight <= 300):
        send_message(chat_id, WEIGHT_INVALID)
        return

    old_weight = profile.get("weight_kg")
    goal = profile.get("goal") or "maintain"
    target_weight = profile.get("target_weight_kg")
    source = "checkin"  # cron + /profile edit both call us; treat as checkin by default

    result = _apply_new_weight(conn, user_id, float(new_weight), goal, source)
    set_awaiting_input(conn, user_id, None)

    body = _weight_change_reply(
        float(new_weight), old_weight,
        result["calories"], result["macros"],
        goal=goal, target_weight=target_weight,
    )

    # F-5: append a one-line projection / on-track-ness summary when meaningful.
    try:
        # `profile` is stale (still has old weight); patch the new weight in
        # so projection_for_profile uses the freshly-saved value.
        live_profile = {**profile, "weight_kg": float(new_weight)}
        projection = goals_mod.projection_for_profile(live_profile)
        actual = None
        status = None
        try:
            history = get_weight_history(conn, user_id, limit=20)
            actual = goals_mod.actual_weekly_delta(history, window_weeks=4)
            if actual is not None:
                status = goals_mod.classify_actual_vs_target(
                    actual, projection.weekly_delta_kg
                )
        except Exception as _hx:
            error("goals_history_failed", exc=_hx, user_id=user_id)
        line = format_projection_line(projection, status=status)
        if line:
            body = body + "\n" + line
    except Exception as _px:
        error("goals_projection_failed", exc=_px, user_id=user_id)

    send_message(
        chat_id,
        body,
        reply_markup=main_menu_keyboard(),
    )


def handle_water_target_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
) -> None:
    """Process a manual water-goal reply (ml) from the /profile → 💧 Ціль води flow."""
    cleaned = text.strip().lower().replace("мл", "").replace("ml", "").strip()
    if cleaned in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, "👌 Скасовано.", reply_markup=main_menu_keyboard())
        return

    ml = _parse_int(cleaned)
    if ml is None:
        send_message(chat_id, "Напиши ціле число в мл (наприклад, 2500).")
        return
    if not (1500 <= ml <= 4000):
        send_message(chat_id, "Ціль має бути від 1500 до 4000 мл. Спробуй ще раз.")
        return

    set_water_target(conn, user_id, ml, overridden=True)
    set_awaiting_input(conn, user_id, None)
    total = get_water_today(conn, user_id)
    send_message(
        chat_id,
        WATER_GOAL_SAVED.format(target=ml) + "\n\n" + format_water(total, ml),
        reply_markup=water_keyboard(),
    )


def handle_target_weight_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
    profile: dict,
) -> None:
    """Process a target-weight reply from the /profile → 🏁 Цільова вага flow."""
    cleaned = text.strip().lower().replace("кг", "").replace("kg", "").strip()
    if cleaned in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, "👌 Скасовано.", reply_markup=main_menu_keyboard())
        return

    tw = _parse_float(cleaned)
    if tw is None:
        send_message(chat_id, WEIGHT_NOT_A_NUMBER)
        return
    if not (30 <= tw <= 300):
        send_message(chat_id, TARGET_WEIGHT_INVALID)
        return
    current_w = profile.get("weight_kg")
    goal = profile.get("goal") or "maintain"
    if current_w is not None:
        if goal == "lose" and tw >= float(current_w):
            send_message(chat_id, TARGET_WEIGHT_LOSE_MISMATCH.format(current=current_w))
            return
        if goal == "gain" and tw <= float(current_w):
            send_message(chat_id, TARGET_WEIGHT_GAIN_MISMATCH.format(current=current_w))
            return

    update_profile(conn, user_id, target_weight_kg=float(tw))
    set_awaiting_input(conn, user_id, None)
    send_message(
        chat_id,
        TARGET_WEIGHT_SAVED.format(target=tw),
        reply_markup=main_menu_keyboard(),
    )


def handle_weekly_delta_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
    profile: dict,
) -> None:
    """Process a weekly_delta_kg reply from the /goals (or /profile) edit flow.

    The user types an unsigned magnitude (e.g. "0.5"); we sign it based on the
    profile's goal direction so they don't have to think about minus signs.
    """
    cleaned = (
        text.strip().lower()
            .replace("кг", "")
            .replace("kg", "")
            .replace(",", ".")
            .strip()
    )
    if cleaned in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, "👌 Скасовано.", reply_markup=main_menu_keyboard())
        return

    raw = _parse_float(cleaned)
    if raw is None:
        send_message(chat_id, WEEKLY_DELTA_INVALID)
        return

    magnitude = abs(raw)
    if not (0.1 <= magnitude <= 2.0):
        send_message(chat_id, WEEKLY_DELTA_INVALID)
        return

    goal = profile.get("goal") or "maintain"
    if goal == "maintain":
        send_message(chat_id, WEEKLY_DELTA_NOT_FOR_MAINTAIN, reply_markup=main_menu_keyboard())
        set_awaiting_input(conn, user_id, None)
        return

    # The prompt + WRONG_SIGN copy both ask the user to type a POSITIVE number;
    # we apply the sign automatically based on goal direction.
    #   - lose: + (as instructed) or − (already-signed) both accepted.
    #   - gain: − is wrong-direction (wants up, typed down) → reject.
    if raw < 0 and goal == "gain":
        send_message(chat_id, WEEKLY_DELTA_WRONG_SIGN)
        return

    signed = -magnitude if goal == "lose" else magnitude
    update_profile(conn, user_id, weekly_delta_kg=float(signed))
    set_awaiting_input(conn, user_id, None)
    send_message(
        chat_id,
        WEEKLY_DELTA_SAVED.format(delta=signed),
        reply_markup=main_menu_keyboard(),
    )


def handle_profile_edit_callback(conn, cb: dict, profile: dict) -> None:
    """Handle inline buttons from the /profile screen: weight / goal / water edit."""
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)

    # prof:weight → prompt for new weight, FSM picks it up via awaiting_input_type.
    if data == "prof:weight":
        answer_callback_query(cb_id, "⚖️ Чекаю на вагу")
        set_awaiting_input(conn, user_id, "weight")
        send_message(chat_id, WEIGHT_INPUT_PROMPT)
        return

    # prof:goal → show the goal picker.
    if data == "prof:goal":
        answer_callback_query(cb_id)
        send_message(chat_id, GOAL_UPDATE_PROMPT, reply_markup=profile_goal_keyboard())
        return

    # prof:goal:<lose|maintain|gain> → apply the goal change.
    if data.startswith("prof:goal:"):
        new_goal = data.split(":", 2)[2]
        if new_goal not in _VALID_GOALS:
            answer_callback_query(cb_id, "Невірна відповідь")
            return
        weight = profile.get("weight_kg") or 70
        new_cal = calorie_target_from_profile(float(weight), new_goal)
        # Any goal change invalidates the motivation target — clear it.
        # For lose/gain we'll prompt for a fresh one right after.
        update_profile(
            conn, user_id,
            goal=new_goal,
            daily_calorie_target=new_cal,
            recommended_calorie_target=new_cal,
            target_weight_kg=None,
        )
        answer_callback_query(cb_id, "🎯 Оновив")
        macros = macro_gram_targets_from_profile(float(weight), new_goal)
        send_message(
            chat_id,
            f"{GOAL_UPDATED.format(goal=_GOAL_LABEL_UA.get(new_goal, new_goal))}\n"
            f"🎯 Нова норма калорій: <b>{new_cal} ккал/день</b>\n"
            f"🥩 Білки {macros['protein']}г · 🍚 Вуглеводи {macros['carbs']}г · 🧈 Жири {macros['fat']}г",
            reply_markup=main_menu_keyboard(),
        )
        if new_goal in ("lose", "gain"):
            set_awaiting_input(conn, user_id, "target_weight")
            prompt = TARGET_WEIGHT_ASK_LOSE if new_goal == "lose" else TARGET_WEIGHT_ASK_GAIN
            send_message(chat_id, prompt)
        else:
            send_message(chat_id, TARGET_WEIGHT_CLEARED)
        return

    # prof:target_weight → prompt for the motivation target.
    if data == "prof:target_weight":
        goal = profile.get("goal") or "maintain"
        if goal == "maintain":
            answer_callback_query(cb_id, "Для цієї мети не потрібна")
            send_message(chat_id, TARGET_WEIGHT_CLEARED, reply_markup=main_menu_keyboard())
            return
        answer_callback_query(cb_id, "🎯 Чекаю на число")
        set_awaiting_input(conn, user_id, "target_weight")
        send_message(chat_id,
                     TARGET_WEIGHT_ASK_LOSE if goal == "lose" else TARGET_WEIGHT_ASK_GAIN)
        return

    # F-5: prof:weekly_delta → prompt for kg/week target.
    if data == "prof:weekly_delta":
        goal = profile.get("goal") or "maintain"
        if goal == "maintain":
            answer_callback_query(cb_id, "Для цієї мети не потрібна")
            send_message(chat_id, WEEKLY_DELTA_NOT_FOR_MAINTAIN, reply_markup=main_menu_keyboard())
            return
        answer_callback_query(cb_id, "📈 Чекаю на число")
        set_awaiting_input(conn, user_id, "weekly_delta")
        send_message(
            chat_id,
            WEEKLY_DELTA_ASK_LOSE if goal == "lose" else WEEKLY_DELTA_ASK_GAIN,
        )
        return

    # prof:water → show preset picker (reuses the existing water_goal_keyboard).
    if data == "prof:water":
        answer_callback_query(cb_id)
        send_message(chat_id, WATER_GOAL_PROMPT, reply_markup=water_goal_keyboard())
        return

    # prof:water:custom → prompt for manual ml entry, FSM picks it up.
    if data == "prof:water:custom":
        answer_callback_query(cb_id, "💧 Чекаю на число")
        set_awaiting_input(conn, user_id, "water_target")
        send_message(chat_id, "💧 Напиши ціль по воді в мл (1500–4000):")
        return

    answer_callback_query(cb_id, "Невідома дія")
