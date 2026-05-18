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
    ALLOWED_USER_IDS,
    ADMIN_NOTIFY_CHAT_ID,
    calorie_target_from_profile,
    macro_gram_targets_from_profile,
    macro_gram_targets,
    language_for_locale,
    recalc_prompt,
)
from lib import config as config_mod
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
    set_nudge_optout,
    count_chat_messages,
    clear_chat_history,
    save_recipe,
    list_recipes,
    get_recipe,
    delete_recipe,
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
    set_my_commands,
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
    lang_confirm_keyboard,
    nudge_optout_keyboard,
    ai_menu_keyboard,
)
from lib.openai_vision import (
    analyze_photo,
    analyze_text,
    normalize_candidates,
    is_ambiguous,
    candidate_to_analysis,
    analyze_menu,
    extract_pantry_from_photo,
)
from lib.openai_voice import transcribe_voice
from lib.openai_nutrition import suggest_meal
from lib.openai_chat import ask_chat, ask_chat_with_photo
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
    format_new_user_notification,
    format_alternates_intro,
    format_aliases,
    # BARCODE_*, MENU_* migrated to lib/i18n keys (F-2b Phase 3).
    # Use _t("barcode.foo", profile) / _t("menu.foo", profile) at call sites.
    format_menu_dishes_intro,
    format_menu_dish_row,
    # PLAN_* migrated to lib/i18n keys (F-2b Phase 3 + Chunk 4 — plan.*)
    format_meal_plan_day,
    # FRIDGE_*, SUGGEST_VARIATION_HINT migrated to lib/i18n (F-2b Phase 3 — fridge.*)
    format_meals_list,
    format_profile,
    format_recommendation,
    # Bulk error/prompt/weight constants migrated to lib/i18n (F-2b Chunk 3).
    # Use _t("section.key", profile) at call sites.
    btn_label,
    menu_button_labels,
    button_text_to_command,
    format_water,
    format_meal_list_entry,
    # FAV_*, RECENT_EMPTY_LIST, RELOG_*, UNDO_*, WATER_* migrated to
    # lib/i18n keys (F-2b Chunk 4b) — favorite.* / relog.* / undo.* / water.*
    # TARGET_WEIGHT_* migrated to lib/i18n keys (F-2b Phase 3 — target_weight.*)
    # WEEKLY_DELTA_* + GOALS_* migrated to lib/i18n (F-2b Phase 3 — goals.*)
    format_goals,
    format_projection_line,
    # ONBOARDING_TZ_* + TIMEZONE_* migrated to lib/i18n (F-2b Phase 2/3 — timezone.*)
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
from lib.bot_commands import build_commands


def _t(key: str, profile=None, **kwargs):
    """Localize ``key`` against the user's profile language.

    Convenience wrapper for the very common
    ``i18n_mod.t(key, locale=i18n_mod.locale_of(profile), **kwargs)``
    pattern used at every send_message site that supports both UA + EN.
    """
    return i18n_mod.t(key, locale=i18n_mod.locale_of(profile), **kwargs)
# All HEALTH_* string constants migrated to lib/i18n keys (F-2b Phase 3 — health.*)


# auth.* keys live in lib/i18n; callers use i18n_mod.t("auth.foo", locale=...).


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

def _enforce_quota(conn, chat_id: int, user_id: int, action: str) -> bool:
    """Atomically increment the quota counter; if over limit, send a friendly
    locale-aware reject message and return False. Returns True when allowed.

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
        # Locale resolved per-call from profile so cron contexts work too.
        profile = get_profile(conn, user_id) or {}
        locale = i18n_mod.locale_of(profile)
        label = i18n_mod.t(f"quota.kind_{action}", locale=locale) or action
        send_message(chat_id, i18n_mod.t("quota.daily_limit_message", locale=locale, action=label, limit=limit))
        return False
    return True


MAX_WEBHOOK_BYTES = 512 * 1024


# AI menu smart intent: question prefixes the classifier recognizes.
# Heuristic only — no model call. Falls through to "suggest" on ambiguity.
_INTENT_QUESTION_PREFIXES: tuple[str, ...] = (
    "how ", "why ", "what ", "when ", "where ", "can ", "is ",
    "should ", "could ", "do ", "does ", "are ", "will ", "would ",
    "як ",  "чому ", "що ", "коли ", "де ", "чи ", "скільки ",  # noqa: i18n
    "хочу знати", "розкажи",  # noqa: i18n
)


def _is_reply_to_ask_prompt(message: dict) -> bool:
    """True if the user is replying to one of our /ask prompts (UA or EN).
    Used by the photo and voice routers to dispatch to the Q&A handlers
    instead of the default meal-logging path."""
    reply_to = message.get("reply_to_message") or {}
    if not reply_to.get("from", {}).get("is_bot"):
        return False
    text = reply_to.get("text") or ""
    prompts = (
        i18n_mod.t("ask.prompt", "uk"),
        i18n_mod.t("ask.prompt", "en"),
    )
    return text in prompts


def _classify_ai_intent(text: str) -> str:
    """Return one of: 'ask' / 'fridge' / 'suggest'.

    Used by the AI-menu chooser when the user types or speaks instead of
    tapping a button. Falls through to 'suggest' on ambiguity — the
    safest default since /suggest_meal accepts free-form `extra_hint`.
    """
    s = (text or "").strip().lower()
    if not s:
        return "suggest"
    if "?" in s or any(s.startswith(p) for p in _INTENT_QUESTION_PREFIXES):
        return "ask"
    if s.count(",") >= 2:
        return "fridge"
    return "suggest"


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

# Awaiting-input states whose user-side expectation is *text*. When a photo
# arrives in any of these states, we treat it as an implicit abandonment of
# the prompt and clear the state (otherwise the next text reply gets trapped
# by the input handler — same Sergey-family trap as commit 0e46a66). Keep
# this in sync with the dispatcher's `awaiting_input_type` branches at the
# top of `do_POST`. Photo-expecting states (menu_photo / fridge_ingredients
# / plan_pantry) intentionally NOT in this set — they're handled by their
# own specific branches earlier in the dispatch order.
_TEXT_INPUT_STATES = frozenset({
    "weight", "water_target", "target_weight", "weekly_delta",
    "barcode_grams", "barcode_manual", "timezone",
    "health_allergens", "health_conditions",
})


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
        # Persist the two Telegram fields as-is. `username` is the public
        # @handle (often empty); `first_name` is the display name. Do NOT
        # collapse them — the admin panel renders `@username` vs plain
        # display-name differently and depends on the distinction.
        username = user.get("username") or ""
        first_name = user.get("first_name") or ""

        # F-15 attribution: parse the deep-link payload from `/start <token>`
        # so we can attribute the signup to the surface that sent them
        # (site banner, IG bio, partner, QR, etc.). Sanitise to Telegram's
        # allowed `[A-Za-z0-9_-]{1,64}` set + truncate. Empty / typed
        # `/start` falls through to source="" (organic). First-write-wins
        # is enforced inside upsert_user so repeat-tappers stay attributed
        # to their first arrival surface.
        _msg_text = (message.get("text") or "").strip()
        source = ""
        if _msg_text.startswith("/start") and len(_msg_text) > 6:
            _raw = _msg_text[6:].strip()
            source = "".join(c for c in _raw if c.isalnum() or c in "_-")[:64]

        if not _is_allowed(user_id):
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                # Profile may not exist yet — locale falls back to EN. The
                # rejection path only fires under a non-empty ALLOWED_USER_IDS
                # (dev/staging), so polishing per-user locale isn't worth it.
                send_message(chat_id, i18n_mod.t("auth.not_authorized", locale="en"))
            return

        if user_id:
            upsert_user(conn, user_id, username, first_name, source)

        chat_id = message["chat"]["id"]

        # Onboarding takes precedence over everything except explicit /start reset.
        profile = get_profile(conn, user_id) if user_id else None
        # F-16: auto-clear blocked_at on inbound message. If we'd previously
        # marked them blocked (TG 400/403 on a send) but they're messaging
        # us now, they've unblocked the bot — flip the flag so they re-enter
        # notification cohorts on the next cron fire.
        if user_id and profile and profile.get("blocked_at"):
            from lib.database import set_blocked
            set_blocked(conn, user_id, False)
            profile["blocked_at"] = None
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
                send_message(chat_id, help_message(i18n_mod.locale_of(profile)))
                return
            if message.get("photo"):
                send_message(chat_id, _t("onboarding.required", profile))
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

        # F-11 / F-10 extension: a photo of the fridge / pantry while the
        # user is in pantry-input mode → OCR the visible food and feed the
        # extracted list straight into the existing pantry handler.
        if message.get("photo") and profile and profile.get("awaiting_input_type") == "fridge_ingredients":
            handle_fridge_photo(conn, message, profile)
            return
        if message.get("photo") and profile and profile.get("awaiting_input_type") == "plan_pantry":
            handle_plan_pantry_photo(conn, message, profile)
            return

        # Photo replied to /ask prompt: vision-aware Q&A instead of meal log.
        # Caption (if any) is the question; missing caption uses default.
        if message.get("photo") and _is_reply_to_ask_prompt(message):
            handle_ask_photo(conn, message, profile)
            return

        # Photo arriving while the user is in a text-input state is an
        # implicit "abandon the prompt" signal. Clear `awaiting_input_type`
        # so the user's next plain-text message isn't trapped by the input
        # handler — same Sergey-family bug we fixed in commit 0e46a66 but
        # for the photo path. Photo-expecting states (`menu_photo`,
        # `fridge_ingredients`, `plan_pantry`) are handled by the more
        # specific branches above and never reach this point.
        if (
            message.get("photo")
            and user_id
            and profile
            and profile.get("awaiting_input_type") in _TEXT_INPUT_STATES
        ):
            set_awaiting_input(conn, user_id, None)
            # fall through to handle_photo below — the photo is a legit meal log

        if message.get("photo"):
            handle_photo(conn, message)
            return

        if not text:
            return

        # Bilingual reply-keyboard dispatch: accepts UA + EN labels for all
        # 11 buttons, plus legacy pre-F-2b UA labels. See lib.formatters.
        _menu_labels = menu_button_labels()
        _dashboard_labels = {btn_label("dashboard", locale=l) for l in ("uk", "en")}
        _water_labels     = {btn_label("water",     locale=l) for l in ("uk", "en")}
        if text in _dashboard_labels:
            send_message(
                chat_id,
                _t("dashboard.open_prompt", profile),
                reply_markup=dashboard_inline_keyboard(locale=i18n_mod.locale_of(profile)),
            )
            return
        if text in _water_labels:
            # Quick-add 250 ml and reply with updated bar + keyboard.
            handle_water_quickadd(conn, chat_id, user_id, amount_ml=250)
            return
        if text in _menu_labels:
            mapped = button_text_to_command(text)
            if mapped:
                handle_command(conn, message, mapped, first_name, profile)
                return

        # F4: meal-edit / manual-correction takes priority over every
        # awaiting_input_type branch below, mirroring the order
        # `handle_voice` has used since day one. Without this, a stuck
        # 'weight' state from the weekly cron silently steers meal-edit
        # text into the weight handler ("not a number" error).
        if user_id:
            _pending_for_text = get_pending_analysis(conn, user_id)
            if _pending_for_text and _pending_for_text["awaiting_manual"]:
                handle_manual_text_input(conn, message, text, _pending_for_text, profile)
                return

        # Weight check-in / manual weight edit takes priority over everything
        # except the /cancel escape hatch (handled further down).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "weight"
            and not text.startswith("/")
        ):
            handle_weight_input(conn, chat_id, user_id, first_name, text, profile)
            return

        # Manual water-target entry from /profile → Water goal → Custom value.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "water_target"
            and not text.startswith("/")
        ):
            handle_water_target_input(conn, chat_id, user_id, text)
            return

        # Motivation target-weight entry from /profile → Target weight.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "target_weight"
            and not text.startswith("/")
        ):
            handle_target_weight_input(conn, chat_id, user_id, text, profile)
            return

        # F-5: weekly delta input (kg/week target).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "weekly_delta"
            and not text.startswith("/")
        ):
            handle_weekly_delta_input(conn, chat_id, user_id, text, profile)
            return

        # F-8: custom barcode portion grams input.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "barcode_grams"
            and not text.startswith("/")
        ):
            handle_barcode_grams_input(conn, chat_id, user_id, first_name, text, profile)
            return

        # F-8: manual EAN entry (fallback when camera path fails).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "barcode_manual"
            and not text.startswith("/")
        ):
            handle_barcode_manual_input(conn, chat_id, user_id, text, profile)
            return

        # F-10: pantry items capture for /plan.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "plan_pantry"
            and not text.startswith("/")
        ):
            handle_plan_pantry_input(conn, chat_id, user_id, text, profile)
            return

        # F-11: fridge ingredients capture for /suggest_meal variation.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "fridge_ingredients"
            and not text.startswith("/")
        ):
            handle_fridge_input(conn, chat_id, user_id, text, profile)
            return

        # Active /ask thread: any plain text continues the conversation.
        # Slash commands escape (handled by the command dispatcher below).
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "ask_thread"
            and text
            and not text.startswith("/")
        ):
            handle_ask(conn, user_id, chat_id, text, profile)
            return

        # AI menu smart intent: user tapped the merged AI button (or ran /ai),
        # then typed a message instead of tapping a chooser button. Classify
        # the text and route to the right handler. Slash commands fall
        # through to the command dispatcher below.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "ai_menu"
            and text
            and not text.startswith("/")
        ):
            set_awaiting_input(conn, user_id, None)
            intent = _classify_ai_intent(text)
            if intent == "ask":
                handle_ask(conn, user_id, chat_id, text, profile)
            elif intent == "fridge":
                _run_suggest_meal(conn, chat_id, user_id, profile,
                                  pantry=text, extra_hint="")
            else:  # 'suggest'
                _run_suggest_meal(conn, chat_id, user_id, profile,
                                  pantry="", extra_hint=text)
            return

        # Free-text IANA timezone from /timezone → Other zone.
        # Slash commands escape to the command dispatcher below.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") == "timezone"
            and not text.startswith("/")
        ):
            handle_timezone_input(conn, chat_id, user_id, text)
            return

        # Free-text health profile input (allergens / conditions) from /health.
        # Slash commands escape to the command dispatcher below.
        if (
            user_id
            and profile
            and profile.get("awaiting_input_type") in ("health_allergens", "health_conditions")
            and not text.startswith("/")
        ):
            handle_health_input(conn, chat_id, user_id, text, profile["awaiting_input_type"])
            return

        if text.startswith("/"):
            if user_id and text.lower().strip() == "/cancel":
                pending = get_pending_analysis(conn, user_id)
                if pending and pending["awaiting_manual"]:
                    pop_pending_analysis(conn, user_id)
                    pop_pending_entry(conn, user_id)
                    # F2: /cancel is the universal escape hatch — also clear
                    # any lingering awaiting_input_type so the next typed
                    # message routes to the default meal-logging path.
                    set_awaiting_input(conn, user_id, None)
                    send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
                    return
            handle_command(conn, message, text, first_name, profile)
            return

        reply_to = message.get("reply_to_message") or {}
        # F-2b: ASK_PROMPT now has UA + EN versions; compare against both so a
        # reply matches regardless of which locale the user was on when sent.
        _ask_prompts = (
            i18n_mod.t("ask.prompt", "uk"),
            i18n_mod.t("ask.prompt", "en"),
        )
        replying_to_bot = bool(reply_to.get("from", {}).get("is_bot"))
        replying_to_ask_prompt = replying_to_bot and reply_to.get("text") in _ask_prompts
        # Continue an in-flight /ask thread: a reply to ANY bot message when
        # the user has chat_session rows in the rolling 60-min window means
        # they're still chatting with us, not logging a meal.
        in_active_thread = (
            replying_to_bot
            and user_id
            and count_chat_messages(conn, user_id, minutes=60) > 0
        )
        if user_id and (replying_to_ask_prompt or in_active_thread):
            handle_ask(conn, user_id, chat_id, text, profile)
            return

        # F4: the awaiting_manual route is now handled above (top of the
        # text dispatcher), mirroring `handle_voice`'s precedence. If we
        # reach here, no FSM state matched — fall through to default
        # meal-logging text entry.
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
        send_message(chat_id, i18n_mod.t("auth.not_authorized", locale="en"))
    answer_callback_query(cb["id"], i18n_mod.t("auth.unavailable_short", locale="en"))


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
        set_chat_menu_button(chat_id=chat_id, locale=i18n_mod.locale_of(profile))
    except Exception as e:
        print("set_chat_menu_button error:", e, flush=True)

    if profile_is_complete(profile):
        send_message(chat_id, welcome_message(first_name, locale=i18n_mod.locale_of(profile)), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    # Fresh user or unfinished profile: kick off onboarding.
    profile = ensure_profile_row(conn, user_id)
    reset_onboarding(conn, user_id)

    # F-2b onboarding step zero: confirm the auto-detected language.
    # We seed `lang` from Telegram's language_code so the entire onboarding
    # message stack runs in the right locale, then ask the user to confirm
    # or override before the existing 6-question flow.
    detected = i18n_mod.normalize_lang(language_code) if language_code else "en"
    update_profile(conn, user_id, lang=detected, onboarding_step="awaiting_lang_confirm")
    send_message(
        chat_id,
        i18n_mod.t("lang_confirm_prompt", locale=detected),
        reply_markup=lang_confirm_keyboard(detected),
    )


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


def _meal_to_analysis(meal: dict) -> dict:
    """Synthesize an ``analysis`` dict the AI can patch from a saved meal row.

    Used by the ``/meals`` → ✏️ Edit path so the modification prompt has full
    context of the existing meal when the user types a delta. Prefer the
    original raw AI JSON (richer schema) and fall back to a column-derived
    shape for migrated rows where ``ai_raw_response`` is absent or has a
    different schema (Iryna's Food-imported meals fall through to the
    fallback because Food's prompt produced a slimmer JSON).
    """
    raw = meal.get("ai_raw_response") or ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("ingredients"):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "dish_name":   meal.get("description") or "",
        "description": meal.get("description") or "",
        "ingredients": meal.get("ingredients") or [],
        "nutrition": {
            "calories":  meal.get("calories")  or 0,
            "protein_g": meal.get("protein_g") or 0,
            "carbs_g":   meal.get("carbs_g")   or 0,
            "fat_g":     meal.get("fat_g")     or 0,
            "fiber_g":   meal.get("fiber_g")   or 0,
            "sugar_g":   meal.get("sugar_g")   or 0,
        },
        "allergen_flags": meal.get("allergen_warnings") or [],
        "crohn_flags":    meal.get("crohn_warnings")    or [],
    }


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
    # Variant A copy: cal + water targets are inlined into `onboarding.done`
    # itself so the activation CTA stays at the bottom of the message instead
    # of being buried by appended target lines. Fall back to a reasonable
    # water default in the rare case the upsert failed.
    done_text = _t(
        "onboarding.done",
        profile,
        name=first_name or _t("onboarding.default_name", profile),
        cal=cal,
        water=water or 2000,
    )
    send_message(chat_id, done_text, reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))

    # Best-effort admin-channel notification. Wrapped to never affect the user.
    try:
        _notify_admin_new_user(conn, user_id, first_name)
    except Exception:
        # Deliberately no detail in the log — the chat id and any underlying
        # send error must not leak into Vercel logs.
        print("admin notify dispatch failed", flush=True)


def _notify_admin_new_user(conn, user_id: int, first_name: str | None) -> None:
    """Post a freshly-onboarded user's profile to the configured admin channel.

    No-op when ``ADMIN_NOTIFY_CHAT_ID`` is unset. Never logs the chat id or
    Telegram error bodies — keeps the destination secret even on failures.
    """
    if not ADMIN_NOTIFY_CHAT_ID:
        return
    try:
        chat_id = int(ADMIN_NOTIFY_CHAT_ID)
    except (TypeError, ValueError):
        print("admin notify: invalid chat id format", flush=True)
        return
    try:
        profile = get_profile(conn, user_id) or {}
        username = ""
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM users WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
            username = (row[0] if row else "") or ""
        except Exception:
            username = ""
        text = format_new_user_notification(profile, username, first_name)
        resp = send_message(chat_id, text)
        if not (isinstance(resp, dict) and resp.get("ok")):
            # Generic log only — no chat id, no response body.
            print("admin notify: telegram returned non-ok", flush=True)
    except Exception:
        print("admin notify: send failed", flush=True)


def handle_onboarding_text(conn, chat_id: int, user_id: int, first_name: str | None,
                           text: str, profile: dict | None) -> None:
    if profile is None:
        profile = ensure_profile_row(conn, user_id)
    step = profile.get("onboarding_step") or "awaiting_age"

    if step == "awaiting_age":
        age = _parse_int(text)
        if age is None:
            send_message(chat_id, _t("onboarding.invalid_number", profile))
            return
        if not (10 <= age <= 100):
            send_message(chat_id, _t("onboarding.age_range", profile))
            return
        update_profile(conn, user_id, age=age, onboarding_step="awaiting_sex")
        send_message(chat_id, _t("onboarding.ask_sex", profile), reply_markup=sex_keyboard(locale=i18n_mod.locale_of(profile)))

    elif step == "awaiting_sex":
        send_message(chat_id, _t("onboarding.need_button", profile), reply_markup=sex_keyboard(locale=i18n_mod.locale_of(profile)))

    elif step == "awaiting_weight":
        w = _parse_float(text)
        if w is None:
            send_message(chat_id, _t("onboarding.invalid_number", profile))
            return
        if not (30 <= w <= 300):
            send_message(chat_id, _t("onboarding.weight_range", profile))
            return
        update_profile(conn, user_id, weight_kg=w, onboarding_step="awaiting_height")
        send_message(chat_id, _t("onboarding.ask_height", profile))

    elif step == "awaiting_height":
        h = _parse_int(text)
        if h is None:
            send_message(chat_id, _t("onboarding.invalid_number", profile))
            return
        if not (100 <= h <= 250):
            send_message(chat_id, _t("onboarding.height_range", profile))
            return
        update_profile(conn, user_id, height_cm=h, onboarding_step="awaiting_gym")
        send_message(chat_id, _t("onboarding.ask_gym", profile), reply_markup=gym_keyboard())

    elif step == "awaiting_gym":
        send_message(chat_id, _t("onboarding.need_button", profile), reply_markup=gym_keyboard())

    elif step == "awaiting_goal":
        send_message(chat_id, _t("onboarding.need_button", profile), reply_markup=goal_keyboard(locale=i18n_mod.locale_of(profile)))

    elif step == "awaiting_target_weight":
        tw = _parse_float(text)
        if tw is None:
            send_message(chat_id, _t("weight.not_a_number", profile))
            return
        if not (30 <= tw <= 300):
            send_message(chat_id, _t("target_weight.invalid", profile))
            return
        current_w = profile.get("weight_kg")
        goal = profile.get("goal") or "maintain"
        if current_w is not None:
            if goal == "lose" and tw >= float(current_w):
                send_message(chat_id, _t("target_weight.lose_mismatch", profile, current=current_w))
                return
            if goal == "gain" and tw <= float(current_w):
                send_message(chat_id, _t("target_weight.gain_mismatch", profile, current=current_w))
                return
        rec = calorie_target_from_profile(float(current_w or 70), goal)
        update_profile(
            conn, user_id,
            target_weight_kg=float(tw),
            recommended_calorie_target=rec,
            onboarding_step="awaiting_confirm",
        )
        profile_after = get_profile(conn, user_id) or {}
        send_message(chat_id, _t("target_weight.saved", profile, target=tw))
        send_message(
            chat_id,
            format_recommendation(profile_after, rec, locale=i18n_mod.locale_of(profile_after)),
            reply_markup=confirm_calories_keyboard(locale=i18n_mod.locale_of(profile)),
        )

    elif step == "awaiting_confirm":
        send_message(chat_id, _t("onboarding.need_button", profile), reply_markup=confirm_calories_keyboard(locale=i18n_mod.locale_of(profile)))

    elif step == "awaiting_custom_cal":
        cal = _parse_int(text)
        if cal is None:
            send_message(chat_id, _t("onboarding.invalid_number", profile))
            return
        if not (1000 <= cal <= 6000):
            send_message(chat_id, _t("onboarding.custom_cal_range", profile))
            return
        update_profile(
            conn, user_id,
            daily_calorie_target=cal,
            onboarding_step="awaiting_tz",
        )
        send_message(chat_id, _t("onboarding.ask_tz", profile), reply_markup=tz_keyboard(prefix="onb:tz", locale=i18n_mod.locale_of(profile)))

    elif step == "awaiting_tz":
        # User typed instead of tapping a preset — reshow the keyboard.
        send_message(chat_id, _t("onboarding.need_button", profile), reply_markup=tz_keyboard(prefix="onb:tz", locale=i18n_mod.locale_of(profile)))

    elif step == "awaiting_tz_custom":
        tz_input = text.strip()
        if tz_input.lower() in ("/cancel", "cancel"):
            update_profile(conn, user_id, onboarding_step="awaiting_tz")
            send_message(chat_id, _t("onboarding.ask_tz", profile), reply_markup=tz_keyboard(prefix="onb:tz", locale=i18n_mod.locale_of(profile)))
            return
        if not is_valid_tz(tz_input):
            send_message(chat_id, _t("onboarding.tz_invalid", profile))
            return
        update_profile(conn, user_id, tz=tz_input, onboarding_step="done")
        send_message(chat_id, _t("onboarding.tz_saved", profile, tz=tz_input))
        _finalize_onboarding(conn, chat_id, user_id, first_name)

    else:
        # Unexpected state — restart
        reset_onboarding(conn, user_id)
        send_message(chat_id, _t("onboarding.ask_age", profile))


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
        answer_callback_query(cb_id, _t("toast.restart", profile))
        reset_onboarding(conn, user_id)
        send_message(chat_id, _t("onboarding.intro", profile))
        send_message(chat_id, _t("onboarding.ask_age", profile))
        return

    # F-2b: onboarding step zero — language confirmation
    if data.startswith("onb:lang:"):
        chosen = data.split(":", 2)[2]
        if chosen not in i18n_mod.supported_langs():
            answer_callback_query(cb_id, "?")
            return
        # Persist choice + mark confirmed + advance to the existing first question.
        from datetime import datetime as _dt, timezone as _tz
        update_profile(
            conn, user_id,
            lang=chosen,
            lang_confirmed_at=_dt.now(_tz.utc).isoformat(),
            onboarding_step="awaiting_age",
        )
        # Brief inline ack matching the chosen language.
        ack_key = "lang_confirm_saved_" + chosen
        answer_callback_query(cb_id, i18n_mod.t(ack_key, locale=chosen))
        # Pin a chat-scoped `/` command menu in the chosen language so
        # the autocomplete flips immediately. Without this, a UA user
        # whose Telegram UI is in English would keep seeing the EN menu.
        try:
            set_my_commands(
                commands=build_commands(locale=chosen),
                scope={"type": "chat", "chat_id": chat_id},
            )
        except Exception as e:
            print("set_my_commands (lang_confirm) error:", e, flush=True)
        # Re-register the chat menu button URL with the chosen locale —
        # /start fired before profile.lang was set, so the menu button
        # currently points at a stale ?lang=en URL even for UA users.
        try:
            set_chat_menu_button(chat_id=chat_id, locale=chosen)
        except Exception as e:
            print("set_chat_menu_button (lang_confirm) error:", e, flush=True)
        # Now run the standard onboarding intro + first question. These
        # strings are still hardcoded UA in Phase 1; Phase 2 migrates them.
        send_message(chat_id, _t("onboarding.intro", profile))
        send_message(chat_id, _t("onboarding.ask_age", profile))
        return

    if data.startswith("onb:sex:"):
        if step != "awaiting_sex":
            answer_callback_query(cb_id, _t("toast.already_answered", profile))
            return
        sex = data.split(":", 2)[2]
        if sex not in ("male", "female"):
            answer_callback_query(cb_id, _t("toast.invalid", profile))
            return
        update_profile(conn, user_id, sex=sex, onboarding_step="awaiting_weight")
        answer_callback_query(cb_id, _t("toast.saved", profile))
        send_message(chat_id, _t("onboarding.ask_weight", profile))
        return

    if data.startswith("onb:gym:"):
        if step != "awaiting_gym":
            answer_callback_query(cb_id, _t("toast.already_answered", profile))
            return
        freq = data.split(":", 2)[2]
        if freq not in _VALID_GYM_FREQ:
            answer_callback_query(cb_id, _t("toast.invalid", profile))
            return
        update_profile(conn, user_id, gym_per_week=freq, onboarding_step="awaiting_goal")
        answer_callback_query(cb_id, _t("toast.saved", profile))
        send_message(chat_id, _t("onboarding.ask_goal", profile), reply_markup=goal_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if data.startswith("onb:goal:"):
        if step != "awaiting_goal":
            answer_callback_query(cb_id, _t("toast.already_answered", profile))
            return
        goal = data.split(":", 2)[2]
        if goal not in _VALID_GOALS:
            answer_callback_query(cb_id, _t("toast.invalid", profile))
            return
        updated = get_profile(conn, user_id) or {}
        if not updated.get("weight_kg"):
            reset_onboarding(conn, user_id)
            answer_callback_query(cb_id, _t("toast.something_wrong", profile))
            send_message(chat_id, _t("onboarding.ask_age", profile))
            return

        # lose / gain → ask the motivation target weight first, then recommendation.
        if goal in ("lose", "gain"):
            update_profile(
                conn, user_id,
                goal=goal,
                target_weight_kg=None,
                onboarding_step="awaiting_target_weight",
            )
            answer_callback_query(cb_id, _t("toast.saved", profile))
            prompt = _t("target_weight.ask_lose" if goal == "lose" else "target_weight.ask_gain", profile)
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
        answer_callback_query(cb_id, _t("toast.calculating", profile))
        profile_after = get_profile(conn, user_id) or {}
        send_message(
            chat_id,
            format_recommendation(profile_after, rec, locale=i18n_mod.locale_of(profile_after)),
            reply_markup=confirm_calories_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    if data == "onb:cal:accept":
        if step != "awaiting_confirm":
            answer_callback_query(cb_id, _t("toast.already_accepted", profile))
            return
        profile_after = get_profile(conn, user_id) or {}
        rec = profile_after.get("recommended_calorie_target") or 2000
        update_profile(
            conn, user_id,
            daily_calorie_target=rec,
            onboarding_step="awaiting_tz",
        )
        answer_callback_query(cb_id, _t("toast.accepted", profile))
        send_message(chat_id, _t("onboarding.ask_tz", profile), reply_markup=tz_keyboard(prefix="onb:tz", locale=i18n_mod.locale_of(profile)))
        return

    if data.startswith("onb:tz:"):
        if step != "awaiting_tz":
            answer_callback_query(cb_id, _t("toast.already_answered", profile))
            return
        tz_value = data.split(":", 2)[2]
        if tz_value == "custom":
            update_profile(conn, user_id, onboarding_step="awaiting_tz_custom")
            answer_callback_query(cb_id, _t("toast.waiting_zone", profile))
            send_message(chat_id, _t("onboarding.tz_custom_prompt", profile))
            return
        if not is_valid_tz(tz_value):
            answer_callback_query(cb_id, _t("toast.unknown_zone", profile))
            return
        update_profile(conn, user_id, tz=tz_value, onboarding_step="done")
        answer_callback_query(cb_id, _t("toast.saved_check", profile))
        send_message(chat_id, _t("onboarding.tz_saved", profile, tz=tz_value))
        _finalize_onboarding(conn, chat_id, user_id, first_name)
        return

    if data == "onb:cal:custom":
        if step != "awaiting_confirm":
            answer_callback_query(cb_id, _t("toast.already_accepted", profile))
            return
        update_profile(conn, user_id, onboarding_step="awaiting_custom_cal")
        answer_callback_query(cb_id, _t("toast.waiting_number", profile))
        send_message(chat_id, _t("onboarding.custom_cal_prompt", profile))
        return

    answer_callback_query(cb_id, _t("toast.unknown_action", profile))


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
        answer_callback_query(cb_id, _t("toast.unknown_action", profile))
        return
    tz_value = data.split(":", 2)[2]
    if tz_value == "custom":
        set_awaiting_input(conn, user_id, "timezone")
        answer_callback_query(cb_id, _t("toast.waiting_zone", profile))
        send_message(chat_id, _t("timezone.custom_prompt", profile))
        return
    if not is_valid_tz(tz_value):
        answer_callback_query(cb_id, _t("toast.unknown_zone", profile))
        return
    update_profile(conn, user_id, tz=tz_value)
    set_awaiting_input(conn, user_id, None)
    answer_callback_query(cb_id, _t("toast.saved_check", profile))
    send_message(
        chat_id,
        _t("timezone.saved", profile, tz=tz_value),
        reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
    )


def handle_timezone_input(conn, chat_id: int, user_id: int, text: str) -> None:
    """Free-text IANA timezone input from /timezone → Other zone."""
    profile = get_profile(conn, user_id)
    cleaned = text.strip()
    if cleaned.lower() in ("/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(
            chat_id,
            _t("timezone.cancelled", profile),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return
    if not is_valid_tz(cleaned):
        send_message(chat_id, _t("onboarding.tz_invalid", profile))
        return
    update_profile(conn, user_id, tz=cleaned)
    set_awaiting_input(conn, user_id, None)
    send_message(
        chat_id,
        _t("timezone.saved", profile, tz=cleaned),
        reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
    )


# ---------- Health profile (F-1) ----------

def _send_health_menu(conn, chat_id: int, user_id: int) -> None:
    profile = get_profile(conn, user_id)
    h = get_health_profile(conn, user_id) or {"allergens": [], "conditions": []}
    locale_for_health = i18n_mod.locale_of(profile)
    send_message(
        chat_id,
        _t(
            "health.header", profile,
            allergens=render_health_labels(h["allergens"], "allergens", locale=locale_for_health),
            conditions=render_health_labels(h["conditions"], "conditions", locale=locale_for_health),
        ),
        reply_markup=health_menu_keyboard(locale=locale_for_health),
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
        answer_callback_query(cb_id, _t("toast.waiting_list", profile))
        send_message(chat_id, _t("health.allergens_prompt", profile))
        return
    if data == "h:set:conditions":
        set_awaiting_input(conn, user_id, "health_conditions")
        answer_callback_query(cb_id, _t("toast.waiting_list", profile))
        send_message(chat_id, _t("health.conditions_prompt", profile))
        return
    if data == "h:clear":
        clear_health_profile(conn, user_id)
        set_awaiting_input(conn, user_id, None)
        answer_callback_query(cb_id, _t("toast.cleared", profile))
        send_message(chat_id, _t("health.cleared", profile))
        _send_health_menu(conn, chat_id, user_id)
        return
    answer_callback_query(cb_id, _t("toast.unknown_action", profile))


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
    # Attach the new locale's reply keyboard to the confirmation message so
    # the bottom-bar labels (Ask AI / Favorites / +250ml / etc.) flip
    # immediately instead of waiting for the user's next interaction.
    send_message(
        chat_id,
        i18n_mod.t("language_saved", locale=lang, lang=label),
        reply_markup=main_menu_keyboard(locale=lang),
    )
    # Pin a chat-scoped `/` command menu in the user's chosen language.
    # Telegram's setMyCommands lookup picks the chat-scope registration
    # over any language_code match, so this overrides whatever the
    # client's UI language would otherwise serve.
    try:
        set_my_commands(
            commands=build_commands(locale=lang),
            scope={"type": "chat", "chat_id": chat_id},
        )
    except Exception as e:
        print("set_my_commands (lang switch) error:", e, flush=True)
    # Re-register the persistent chat menu button so its Mini App URL
    # carries the new ?lang= query param. Telegram caches the registered
    # URL until we call setChatMenuButton again, so a stale UA URL would
    # otherwise keep opening the dashboard in the old locale.
    try:
        set_chat_menu_button(chat_id=chat_id, locale=lang)
    except Exception as e:
        print("set_chat_menu_button (lang switch) error:", e, flush=True)


def handle_nudge_callback(conn, cb: dict, profile: dict) -> None:
    """One-tap opt-out from the inactivity-nudge inline keyboard."""
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb.get("data", "")

    if data == "nudge:off":
        set_nudge_optout(conn, user_id, True)
        answer_callback_query(cb_id, _t("toast.nudge_off", profile))
        send_message(chat_id, _t("nudge.opted_out", profile))
        return

    answer_callback_query(cb_id)


def handle_ai_menu_callback(conn, cb: dict, profile: dict) -> None:
    """Combined-AI-helper chooser. Routes to existing /ask, /suggest_meal,
    or fridge-mode handlers — no new flows underneath."""
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb.get("data", "")

    # Always clear the transient ai_menu FSM state so the smart-intent
    # text/voice classifier doesn't fire on the user's next message
    # after they've explicitly picked a branch.
    if (profile or {}).get("awaiting_input_type") == "ai_menu":
        set_awaiting_input(conn, user_id, None)

    if data == "ai:cancel":
        answer_callback_query(cb_id, _t("toast.cancelled", profile))
        return

    if data == "ai:ask":
        answer_callback_query(cb_id)
        send_message(
            chat_id,
            _t("ask.prompt", profile),
            reply_markup={"force_reply": True, "selective": True},
        )
        return

    if data == "ai:suggest":
        answer_callback_query(cb_id)
        _run_suggest_meal(conn, chat_id, user_id, profile, pantry="", extra_hint="")
        return

    if data == "ai:fridge":
        answer_callback_query(cb_id, _t("toast.waiting_pantry", profile))
        set_awaiting_input(conn, user_id, "fridge_ingredients")
        send_message(chat_id, _t("fridge.prompt", profile))
        return

    answer_callback_query(cb_id)


def handle_recipes_callback(conn, cb: dict, profile: dict) -> None:
    """Saved-recipes browser callbacks: rec:show:<id> and rec:del:<id>."""
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    data = cb.get("data", "")

    if data.startswith("rec:show:"):
        try:
            rec_id = int(data.split(":", 2)[2])
        except (ValueError, IndexError):
            answer_callback_query(cb_id, _t("toast.unknown_action", profile))
            return
        rec = get_recipe(conn, user_id, rec_id)
        if not rec:
            answer_callback_query(cb_id, _t("toast.unknown_action", profile))
            return
        answer_callback_query(cb_id)
        send_message(chat_id, rec["body"])
        return

    if data.startswith("rec:del:"):
        try:
            rec_id = int(data.split(":", 2)[2])
        except (ValueError, IndexError):
            answer_callback_query(cb_id, _t("toast.unknown_action", profile))
            return
        ok = delete_recipe(conn, user_id, rec_id)
        if ok:
            answer_callback_query(cb_id, _t("toast.cleared", profile))
        else:
            answer_callback_query(cb_id, _t("toast.unknown_action", profile))
        return

    answer_callback_query(cb_id)


def handle_health_input(conn, chat_id: int, user_id: int, text: str, kind: str) -> None:
    """Free-text input for /health → set allergens / conditions.

    ``kind`` is "health_allergens" or "health_conditions" — picked up from
    the user's ``awaiting_input_type``.
    """
    profile = get_profile(conn, user_id)
    cleaned = text.strip()
    if cleaned.lower() in ("/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(
            chat_id,
            _t("health.cancelled", profile),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    is_allergens = kind == "health_allergens"
    registry = HEALTH_ALLERGENS if is_allergens else HEALTH_CONDITIONS

    if is_clear_keyword(cleaned):
        if is_allergens:
            set_health_allergens(conn, user_id, [])
        else:
            set_health_conditions(conn, user_id, [])
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("health.cleared", profile))
        _send_health_menu(conn, chat_id, user_id)
        return

    canon, unknown = parse_health_csv(cleaned, registry)
    if not canon:
        send_message(chat_id, _t("health.invalid_all", profile))
        return

    if is_allergens:
        set_health_allergens(conn, user_id, canon)
    else:
        set_health_conditions(conn, user_id, canon)
    set_awaiting_input(conn, user_id, None)

    saved = render_health_labels(canon, "allergens" if is_allergens else "conditions", locale=i18n_mod.locale_of(profile))
    if unknown:
        send_message(
            chat_id,
            _t("health.saved_with_hints", profile, saved=saved, unknown=", ".join(unknown)),
        )
    else:
        send_message(chat_id, _t("health.saved", profile, saved=saved))
    _send_health_menu(conn, chat_id, user_id)


# ---------- Photo / text entry ----------

# Hard cap on photo size before we pay to download the file from Telegram and
# ship it to GPT-4o vision. The largest entry in `photo[]` is the highest-
# resolution version Telegram is willing to surface.
MAX_PHOTO_BYTES = 5 * 1024 * 1024


def handle_photo(conn, message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    profile = get_profile(conn, user_id)
    photos = message["photo"]
    largest = photos[-1] if photos else {}
    file_id = largest.get("file_id")
    if not file_id:
        return
    file_size = int(largest.get("file_size") or 0)
    if file_size > MAX_PHOTO_BYTES:
        send_message(chat_id, _t("errors.photo_too_large", profile))
        return
    save_pending_photo(conn, user_id, file_id)
    send_message(chat_id, _t("prompts.photo_meal_type", profile), reply_markup=meal_type_keyboard(locale=i18n_mod.locale_of(profile)))


def handle_text_entry(conn, message: dict, text: str) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    profile = get_profile(conn, user_id)
    save_pending_text(conn, user_id, text)
    send_message(chat_id, _t("prompts.text_meal_type", profile), reply_markup=meal_type_keyboard(locale=i18n_mod.locale_of(profile)))


# ---------- Voice entry (Whisper) ----------

def handle_voice(conn, message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    profile = get_profile(conn, user_id) or {}
    voice = message.get("voice") or message.get("audio") or {}
    file_id = voice.get("file_id")
    file_size = voice.get("file_size") or 0
    if not file_id:
        return

    # Hard cap ~2 MB (≈60–90 s OGG/Opus) to keep Whisper + analysis under Vercel timeout.
    if file_size > 2 * 1024 * 1024:
        send_message(chat_id, _t("voice.too_long", profile))
        return

    if not _enforce_quota(conn, chat_id, user_id, "voice_transcribe"):
        return

    send_chat_action(chat_id, "typing")

    try:
        audio_bytes = get_file_bytes(file_id)
    except Exception as e:
        print("voice getFile error:", e, flush=True)
        send_message(chat_id, _t("voice.error", profile))
        return

    try:
        transcript = transcribe_voice(audio_bytes)
    except Exception as e:
        print("whisper error:", e, flush=True)
        send_message(chat_id, _t("voice.error", profile))
        return

    if not transcript or len(transcript) < 3:
        send_message(chat_id, _t("voice.empty", profile))
        return

    safe = _html.escape(transcript, quote=False)
    send_message(chat_id, _t("voice.transcript", profile, text=safe))

    # Re-fetch profile in case state changed during the (potentially
    # seconds-long) Whisper call.
    fresh_profile = get_profile(conn, user_id) or profile

    # Meal-edit correction by voice: if the user tapped ✏️ Modify (after
    # a fresh analysis) or ✏️ Edit on /meals, ``pending_analyses`` carries
    # ``awaiting_manual=1`` plus the ``previous_analysis`` and (for the
    # /meals path) ``replaces_meal_id``. The text dispatcher checks this
    # flag and routes to ``handle_manual_text_input``; the voice handler
    # used to fall through to the fresh-meal path instead, so voice edits
    # silently dropped the replacement intent and the AI patch context —
    # AI saw only the transcript ("eggs 150g") and produced a single-
    # ingredient meal, then ``mod:accept`` saw no ``replaces_meal_id`` and
    # left the original in place → duplicate. Mirror the text dispatcher.
    pending = get_pending_analysis(conn, user_id)
    if pending and pending.get("awaiting_manual"):
        handle_manual_text_input(conn, message, transcript, pending, fresh_profile)
        return

    # F-11 / F-10 extension: a voice message during fridge / plan pantry
    # input is treated as the typed pantry list — same length cap, same
    # downstream handler.
    pantry_state = (fresh_profile or {}).get("awaiting_input_type")
    if pantry_state == "fridge_ingredients":
        handle_fridge_input(conn, chat_id, user_id, transcript, fresh_profile)
        return
    if pantry_state == "plan_pantry":
        handle_plan_pantry_input(conn, chat_id, user_id, transcript, fresh_profile)
        return

    # Active /ask thread: a voice message continues the conversation.
    if pantry_state == "ask_thread" and transcript:
        handle_ask(conn, user_id, chat_id, transcript, fresh_profile)
        return

    # AI menu smart intent: user tapped the merged AI button (or /ai), then
    # sent voice instead of tapping a chooser button. Classify the
    # transcript and route to the right handler.
    if pantry_state == "ai_menu" and transcript:
        set_awaiting_input(conn, user_id, None)
        intent = _classify_ai_intent(transcript)
        if intent == "ask":
            handle_ask(conn, user_id, chat_id, transcript, fresh_profile)
        elif intent == "fridge":
            _run_suggest_meal(conn, chat_id, user_id, fresh_profile,
                              pantry=transcript, extra_hint="")
        else:  # 'suggest'
            _run_suggest_meal(conn, chat_id, user_id, fresh_profile,
                              pantry="", extra_hint=transcript)
        return

    # If this voice message is a reply to the /ask prompt OR continues an
    # in-flight /ask thread (reply to any bot message + recent chat_sessions),
    # treat transcript as a chat question instead of a meal log.
    reply_to = message.get("reply_to_message") or {}
    _ask_prompts = (
        i18n_mod.t("ask.prompt", "uk"),
        i18n_mod.t("ask.prompt", "en"),
    )
    replying_to_bot = bool(reply_to.get("from", {}).get("is_bot"))
    replying_to_ask_prompt = replying_to_bot and reply_to.get("text") in _ask_prompts
    in_active_thread = (
        replying_to_bot
        and count_chat_messages(conn, user_id, minutes=60) > 0
    )
    if replying_to_ask_prompt or in_active_thread:
        profile = get_profile(conn, user_id)
        handle_ask(conn, user_id, chat_id, transcript, profile)
        return

    # Otherwise reuse the existing text-entry flow: saves as pending and asks meal type.
    save_pending_text(conn, user_id, transcript)
    send_message(chat_id, _t("prompts.text_meal_type", profile), reply_markup=meal_type_keyboard(locale=i18n_mod.locale_of(profile)))


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
        answer_callback_query(cb["id"], _t("toast.first_start", profile))
        message = cb.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if chat_id:
            send_message(chat_id, _t("onboarding.required", profile))
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
    elif data.startswith("nudge:"):
        handle_nudge_callback(conn, cb, profile)
    elif data.startswith("ai:"):
        handle_ai_menu_callback(conn, cb, profile)
    elif data.startswith("rec:"):
        handle_recipes_callback(conn, cb, profile)
    elif data == "noop":
        answer_callback_query(cb["id"])
    else:
        answer_callback_query(cb["id"], i18n_mod.t("toast.unknown_action", locale=i18n_mod.locale_of(profile) if profile else "en"))


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
        answer_callback_query(cb_id, _t("toast.cancelled", profile))
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    locale_for_toast = i18n_mod.locale_of(profile)
    meal_label = i18n_mod.t(f"meal_type.{meal_type}", locale=locale_for_toast).lower() if meal_type else meal_type
    answer_callback_query(cb_id, _t("toast.analyzing_meal", profile, meal_type=meal_label))

    entry = pop_pending_entry(conn, user_id)
    if entry is None:
        send_message(chat_id, _t("errors.pending_expired", profile))
        return
    file_id, text_description = entry

    if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
        return

    send_message(chat_id, _t("prompts.analyzing", profile))

    health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
    personal_ctx = ""
    try:
        personal_ctx = personalization_mod.aliases_prompt_block(conn, user_id)
    except Exception as _px:
        error("personalization_prompt_failed", exc=_px, user_id=user_id)
    language = config_mod.language_for_locale(i18n_mod.locale_of(profile))
    analysis, raw = None, ""
    try:
        if file_id:
            try:
                image_bytes = get_file_bytes(file_id)
            except Exception as e:
                print("getFile error:", e, flush=True)
                send_message(chat_id, _t("errors.photo_download_failed", profile))
                return
            analysis, raw = analyze_photo(
                image_bytes,
                health_addendum=health_ctx,
                personalization_addendum=personal_ctx,
                language=language,
            )
        elif text_description:
            analysis, raw = analyze_text(
                text_description,
                health_addendum=health_ctx,
                personalization_addendum=personal_ctx,
                language=language,
            )
        else:
            send_message(chat_id, _t("errors.pending_expired", profile))
            return
    except Exception as e:
        print("analysis error:", e, flush=True)
        send_message(chat_id, _t("errors.text_analysis_failed", profile) if text_description else _t("errors.photo_analysis_failed", profile))
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
    replaces_meal_id: int | None = None,
) -> None:
    """F-6: dispatch to alternates picker (when ambiguous) or normal preview.

    Single source of truth for "show the user what we got and ask them to
    confirm". Persists pending state in either branch so the moderation /
    pick callbacks can find it.

    ``replaces_meal_id`` carries the /meals → ✏️ Edit replacement target
    through the AI patch / recalc round-trip. Default None keeps every
    fresh-meal caller (photo upload, menu OCR pick, alternates pick)
    behaving as before. Callers that already have a `pending` in scope
    (handle_manual_text_input, the recalc handler) MUST forward
    `pending.get("replaces_meal_id")` so the field survives until the
    user taps ✅ — otherwise the replace-on-confirm logic in mod:accept
    sees None and the new meal lands as a duplicate next to the old one.
    """
    profile = get_profile(conn, user_id)
    candidates = normalize_candidates(analysis)
    if is_ambiguous(candidates):
        save_pending_analysis(
            conn, user_id, meal_type, analysis,
            photo_file_id, text_description, raw,
            candidates=candidates,
            replaces_meal_id=replaces_meal_id,
        )
        send_message(
            chat_id,
            format_alternates_intro(meal_type, candidates, locale=i18n_mod.locale_of(profile)),
            reply_markup=alternates_keyboard(candidates, locale=i18n_mod.locale_of(profile)),
        )
        return
    save_pending_analysis(
        conn, user_id, meal_type, analysis,
        photo_file_id, text_description, raw,
        replaces_meal_id=replaces_meal_id,
    )
    send_message(
        chat_id,
        format_meal_preview(meal_type, analysis, locale=i18n_mod.locale_of(profile)),
        reply_markup=moderation_keyboard(locale=i18n_mod.locale_of(profile)),
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
        answer_callback_query(cb_id, _t("toast.saved_kg", profile))
        pending = pop_pending_analysis(conn, user_id)
        if not pending:
            send_message(chat_id, _t("errors.pending_expired", profile))
            return
        analysis = pending["analysis"]
        meal_id = save_meal(conn, user_id, pending["meal_type"], analysis, pending["photo_file_id"] or "", pending["raw_response"])
        upsert_daily_log_from_meal(conn, user_id, analysis)
        # /meals → ✏️ Edit replacement: now that the new meal is safely
        # saved, drop the row we're replacing and recompute that date's
        # totals. Single recalc on the old date covers both cases —
        # if old date == today, the recalc subtracts the now-deleted
        # old meal from today's totals (the upsert above already added
        # the new one); if old date != today, today's totals are already
        # correct and only the old date needs subtraction.
        old_meal_id = pending.get("replaces_meal_id")
        if old_meal_id:
            deleted_old = delete_meal(conn, old_meal_id, user_id)
            if deleted_old:
                recalc_daily_log(conn, user_id, deleted_old["date"])
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
        # Pass health_profile so the minimal log message renders allergen /
        # Crohn warnings only for users who actually have those configured.
        health_profile = get_health_profile(conn, user_id)
        send_message(
            chat_id,
            format_meal_logged(pending["meal_type"], analysis, today_log, cal_target, first_name, locale=i18n_mod.locale_of(profile), health_profile=health_profile),
            reply_markup=meal_logged_actions_keyboard(meal_id, is_fav=False, locale=i18n_mod.locale_of(profile)),
        )

    elif action == "recalc":
        answer_callback_query(cb_id, _t("toast.recalculating", profile))
        pending = get_pending_analysis(conn, user_id)
        if not pending:
            send_message(chat_id, _t("errors.pending_expired", profile))
            return
        if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
            return
        send_message(chat_id, _t("prompts.recalc", profile))

        health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
        personal_ctx = ""
        try:
            personal_ctx = personalization_mod.aliases_prompt_block(conn, user_id)
        except Exception as _px:
            error("personalization_prompt_failed", exc=_px, user_id=user_id)
        language = language_for_locale(i18n_mod.locale_of(profile))
        recalc_text = recalc_prompt(language=language)
        try:
            if pending["photo_file_id"]:
                image_bytes = get_file_bytes(pending["photo_file_id"])
                analysis, raw = analyze_photo(
                    image_bytes,
                    retry_prompt=recalc_text,
                    health_addendum=health_ctx,
                    personalization_addendum=personal_ctx,
                    language=language,
                )
            elif pending["text_description"]:
                analysis, raw = analyze_text(
                    pending["text_description"],
                    retry_prompt=recalc_text,
                    health_addendum=health_ctx,
                    personalization_addendum=personal_ctx,
                    language=language,
                )
            else:
                send_message(chat_id, _t("errors.pending_expired", profile))
                return
        except Exception as e:
            print("recalc error:", e, flush=True)
            send_message(chat_id, _t("errors.photo_analysis_failed", profile))
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
            replaces_meal_id=pending.get("replaces_meal_id"),
        )

    elif action == "manual":
        answer_callback_query(cb_id, _t("toast.waiting_text", profile))
        pending = get_pending_analysis(conn, user_id)
        if not pending:
            send_message(chat_id, _t("errors.pending_expired", profile))
            return
        set_awaiting_manual(conn, user_id)
        send_message(chat_id, _t("prompts.manual_input", profile), reply_markup=cancel_only_keyboard(locale=i18n_mod.locale_of(profile)))

    elif action == "cancel":
        answer_callback_query(cb_id, _t("toast.cancelled", profile))
        pop_pending_analysis(conn, user_id)
        pop_pending_entry(conn, user_id)
        # F3: Cancel is the universal escape — also clear any lingering
        # awaiting_input_type (weight, fridge_ingredients, ai_menu, etc.)
        # so the next typed message lands on the default meal-logging path.
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))


def handle_manual_text_input(conn, message: dict, text: str, pending: dict, profile: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
        return

    send_message(chat_id, _t("prompts.analyzing", profile))

    health_ctx = addendum_for_profile(get_health_profile(conn, user_id))
    personal_ctx = ""
    try:
        personal_ctx = personalization_mod.aliases_prompt_block(conn, user_id)
    except Exception as _px:
        error("personalization_prompt_failed", exc=_px, user_id=user_id)
    # Pass the prior analysis so the AI can PATCH it instead of producing a
    # fresh single-ingredient meal from a delta like "eggs 150g". The
    # `mod:manual` callback is the only path that reaches this handler with
    # a populated `pending["analysis"]`, so non-modify flows are unaffected.
    try:
        analysis, raw = analyze_text(
            text,
            previous_analysis=pending.get("analysis"),
            health_addendum=health_ctx,
            personalization_addendum=personal_ctx,
            language=language_for_locale(i18n_mod.locale_of(profile)),
        )
    except Exception as e:
        print("manual text analysis error:", e, flush=True)
        send_message(chat_id, _t("errors.text_analysis_failed", profile))
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
        replaces_meal_id=pending.get("replaces_meal_id"),
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
        answer_callback_query(cb_id, _t("toast.unknown_option", profile))
        return

    pending = get_pending_analysis(conn, user_id)
    if not pending:
        answer_callback_query(cb_id)
        send_message(chat_id, _t("errors.pending_expired", profile))
        return

    candidates = pending.get("candidates") or []
    if idx < 0 or idx >= len(candidates):
        answer_callback_query(cb_id, _t("toast.option_unavailable", profile))
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
        # Drop candidates so the next "Accept" goes through the normal path.
        candidates=None,
        # Forward the /meals → ✏️ Edit replacement target. Without this the
        # F-6 picker resets the field to NULL and `mod:accept` later skips
        # deleting the old meal — landing the new one as a duplicate next
        # to the original. (Same shape as the leak fixed in 6c5e4cf for
        # _send_analysis_preview.)
        replaces_meal_id=pending.get("replaces_meal_id"),
    )
    send_message(
        chat_id,
        format_meal_preview(pending["meal_type"], new_analysis, locale=i18n_mod.locale_of(profile)),
        reply_markup=moderation_keyboard(locale=i18n_mod.locale_of(profile)),
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
        send_message(chat_id, _t("barcode.pending_expired", profile))
        return

    product = {
        "ean":            pseudo.get("ean", ""),
        "name":           pseudo.get("name", ""),
        "brand":          pseudo.get("brand", ""),
        "per_100g":       pseudo.get("per_100g", {}) or {},
        "serving_size_g": pseudo.get("serving_size_g"),
    }
    analysis = off_mod.product_to_analysis(product, grams, locale=i18n_mod.locale_of(profile))

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
    # Pass health_profile so the minimal log message renders allergen /
    # Crohn warnings only for users who actually have those configured.
    health_profile = get_health_profile(conn, user_id)
    send_message(
        chat_id,
        format_meal_logged(pending["meal_type"], analysis, today_log, cal_target, first_name, locale=i18n_mod.locale_of(profile), health_profile=health_profile),
        reply_markup=meal_logged_actions_keyboard(meal_id, is_fav=False, locale=i18n_mod.locale_of(profile)),
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
        answer_callback_query(cb_id, _t("toast.cancelled", profile))
        pop_pending_analysis(conn, user_id)
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    # Manual EAN entry — fallback for devices where the Mini App camera
    # doesn't work (older iOS, denied camera permission, etc.).
    if data == "barcode:manual":
        answer_callback_query(cb_id, _t("toast.waiting_digits", profile))
        set_awaiting_input(conn, user_id, "barcode_manual")
        send_message(chat_id, _t("barcode.manual_prompt", profile))
        return

    # Scanner-menu merge: route the "📋 Scan restaurant menu" chooser entry
    # to the existing /menu flow (set state + ask for a photo). Identical
    # to the body of `/menu` in handle_command.
    if data == "barcode:menu_ocr":
        answer_callback_query(cb_id)
        set_awaiting_input(conn, user_id, "menu_photo")
        send_message(
            chat_id,
            _t("menu.prompt_intro", profile),
            reply_markup=cancel_only_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    if data == "barcode:g:custom":
        answer_callback_query(cb_id, _t("toast.waiting_grams", profile))
        set_awaiting_input(conn, user_id, "barcode_grams")
        send_message(chat_id, _t("barcode.grams_prompt", profile))
        return

    if data.startswith("barcode:g:"):
        try:
            grams = int(data.split(":", 2)[2])
        except (ValueError, IndexError):
            answer_callback_query(cb_id, _t("toast.invalid_amount", profile))
            return
        if not (1 <= grams <= 5000):
            answer_callback_query(cb_id, _t("toast.invalid_amount", profile))
            return
        pending = get_pending_analysis(conn, user_id)
        if not pending:
            answer_callback_query(cb_id)
            send_message(chat_id, _t("barcode.pending_expired", profile))
            return
        answer_callback_query(cb_id, _t("toast.grams_logging", profile, grams=grams))
        _save_barcode_meal(conn, chat_id, user_id, first_name, profile, pending, float(grams))
        return

    answer_callback_query(cb_id, _t("toast.unknown_action", profile))


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
        send_message(chat_id, _t("menu.ocr_failed", profile))
        return

    # Telegram sends multiple resolutions — pick the largest (last entry).
    largest = photos[-1]
    file_id = largest.get("file_id")
    if not file_id:
        send_message(chat_id, _t("menu.ocr_failed", profile))
        return

    send_message(chat_id, _t("menu.reading", profile))

    try:
        image_bytes = get_file_bytes(file_id)
    except Exception as e:
        error("menu_photo_download_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("menu.ocr_failed", profile))
        set_awaiting_input(conn, user_id, None)
        return

    try:
        dishes, _raw = analyze_menu([image_bytes], language=language_for_locale(i18n_mod.locale_of(profile)))
    except Exception as e:
        error("menu_analyze_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("menu.ocr_failed", profile))
        set_awaiting_input(conn, user_id, None)
        return

    set_awaiting_input(conn, user_id, None)

    if not dishes:
        send_message(chat_id, _t("menu.no_dishes", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    try:
        save_menu_ocr_result(conn, user_id, dishes)
    except Exception as e:
        error("menu_save_result_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("menu.ocr_failed", profile))
        return

    # Build the results message: header + one line per dish.
    locale = i18n_mod.locale_of(profile)
    lines = [format_menu_dishes_intro(len(dishes), locale)]
    for d in dishes[:15]:  # cap message length; keyboard buttons go up to 25
        lines.append(format_menu_dish_row(d, locale))
    send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=menu_log_keyboard(dishes, locale=i18n_mod.locale_of(profile)),
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
        answer_callback_query(cb_id, _t("toast.closed", profile))
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if not data.startswith("menu:log:"):
        answer_callback_query(cb_id, _t("toast.unknown_action", profile))
        return

    try:
        idx = int(data.split(":", 2)[2])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, _t("toast.out_of_range", profile))
        return

    dishes = get_menu_ocr_result(conn, user_id) or []
    if not dishes or idx < 0 or idx >= len(dishes):
        answer_callback_query(cb_id)
        send_message(chat_id, MENU__t("errors.pending_expired", profile))
        return

    chosen = dishes[idx]
    answer_callback_query(cb_id, f"➕ {chosen.get('name', '')[:40]}")

    # Promote the menu dish into a standard analysis dict and route through
    # _send_analysis_preview so the user gets the normal accept/recalc
    # moderation card. We pass NO photo_file_id and a synthetic
    # text_description (so /recalc has something sensible to retry against).
    portion_note = chosen.get("portion_note", "")
    locale_for_menu = i18n_mod.locale_of(profile)
    analysis = {
        "dish_name":         chosen["name"],
        "description":       chosen["name"],
        "estimated_portion": portion_note or i18n_mod.t("menu.restaurant_portion", locale=locale_for_menu),
        "portion_reasoning": i18n_mod.t("menu.ocr_source", locale=locale_for_menu, pct=int(round(chosen.get('confidence', 0) * 100))),
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
    "Skip list" callback can share the heavy lifting.
    """
    if not _enforce_quota(conn, chat_id, user_id, "plan_generate"):
        set_awaiting_input(conn, user_id, None)
        return

    set_awaiting_input(conn, user_id, None)
    send_message(chat_id, _t("plan.generating", profile))

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
            language=language_for_locale(i18n_mod.locale_of(profile)),
        )
    except Exception as e:
        error("plan_generate_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("plan.failed", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    try:
        plan_id = save_meal_plan(conn, user_id, plan)
    except Exception as e:
        error("plan_save_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("plan.failed", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    _send_plan_day(chat_id, plan_id, plan, day_idx=0, locale=i18n_mod.locale_of(profile))


def _send_plan_day(chat_id: int, plan_id: int, plan: dict, day_idx: int, locale: str = "en") -> None:
    """Render one day of a saved plan + the per-day inline keyboard."""
    days = plan.get("days") or []
    if day_idx < 0 or day_idx >= len(days):
        return
    day = days[day_idx]
    body = format_meal_plan_day(day, day_idx, locale)
    if day_idx == 0 and plan.get("notes"):
        body = i18n_mod.t("plan.header_notes", locale, notes=plan["notes"]) + body
    # The `locale` parameter is already in scope; the previous code referenced
    # an undefined `profile` here which crashed every /plan day render with
    # NameError. Use the parameter directly.
    send_message(chat_id, body, reply_markup=plan_day_keyboard(plan_id, day_idx, day, locale=locale))


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
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return
    if len(cleaned) > 200:
        send_message(chat_id, _t("plan.pantry_too_long", profile))
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
        answer_callback_query(cb_id, _t("toast.closed", profile))
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if data == "plan:nopantry":
        answer_callback_query(cb_id, _t("toast.preparing", profile))
        _build_and_send_plan(conn, chat_id, user_id, profile, pantry="")
        return

    parts = data.split(":")
    # Both view + log forms have plan_id at index 2 and day at index 3.
    if len(parts) < 4:
        answer_callback_query(cb_id, _t("toast.unknown_action", profile))
        return

    try:
        plan_id = int(parts[2])
        day_idx = int(parts[3])
    except ValueError:
        answer_callback_query(cb_id, _t("toast.unknown_action", profile))
        return

    plan = get_meal_plan(conn, plan_id, user_id)
    if not plan:
        answer_callback_query(cb_id)
        send_message(chat_id, _t("plan.failed", profile))
        return

    if data.startswith("plan:view:"):
        answer_callback_query(cb_id)
        _send_plan_day(chat_id, plan_id, plan, day_idx, locale=i18n_mod.locale_of(profile))
        return

    if data.startswith("plan:log:"):
        if len(parts) < 5:
            answer_callback_query(cb_id, _t("toast.error", profile))
            return
        slot_key = parts[4]
        days = plan.get("days") or []
        if day_idx < 0 or day_idx >= len(days):
            answer_callback_query(cb_id, _t("toast.out_of_range", profile))
            return
        slot = (days[day_idx].get("slots") or {}).get(slot_key)
        if not slot:
            answer_callback_query(cb_id, _t("toast.meal_type_unavailable", profile))
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

    answer_callback_query(cb_id, _t("toast.unknown_action", profile))


# ---------- F-11: /suggest_meal extensions (fridge + variation) ----------

def _run_suggest_meal(
    conn,
    chat_id: int,
    user_id: int,
    profile: dict,
    pantry: str = "",
    extra_hint: str = "",
) -> None:
    """Shared body for /suggest_meal, the fridge handler, and "different version".

    Always reuses the existing ``suggest`` quota counter — fridge / variation
    are not separate buckets to keep the cost surface predictable.
    """
    if not _enforce_quota(conn, chat_id, user_id, "suggest"):
        return
    log = get_today_log(conn, user_id)
    meals = get_meals_for_day(conn, user_id, log["date"])
    send_message(chat_id, _t("suggest.thinking", profile))
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
            language=language_for_locale(i18n_mod.locale_of(profile)),
        )
    except Exception as e:
        print("suggest error:", e, flush=True)
        send_message(chat_id, _t("suggest.failed", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return
    send_message(chat_id, recipe, reply_markup=suggest_followup_keyboard(locale=i18n_mod.locale_of(profile)))


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
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return
    if len(cleaned) > 300:
        send_message(chat_id, _t("fridge.too_long", profile))
        return
    set_awaiting_input(conn, user_id, None)
    _run_suggest_meal(conn, chat_id, user_id, profile, pantry=cleaned, extra_hint="")


def _handle_pantry_photo(
    conn,
    message: dict,
    profile: dict,
    delegate,
) -> None:
    """Shared body for fridge / plan pantry photo OCR.

    Quota note: we do NOT call ``_enforce_quota`` here. The downstream
    delegate (``handle_fridge_input`` → ``_run_suggest_meal``, or
    ``handle_plan_pantry_input`` → ``_build_and_send_plan``) already runs
    the appropriate counter. Calling it here would double-tick the daily
    limit. Cost of an OCR call when the user is over quota is ~$0.01,
    bounded — they can't actually use the result because the recipe /
    plan request gets rejected.
    """
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    photos = message.get("photo") or []
    largest = photos[-1] if photos else {}
    file_id = largest.get("file_id")
    if not file_id:
        return
    file_size = int(largest.get("file_size") or 0)
    if file_size > MAX_PHOTO_BYTES:
        send_message(chat_id, _t("errors.photo_too_large", profile))
        return
    send_chat_action(chat_id, "typing")
    try:
        image_bytes = get_file_bytes(file_id)
    except Exception as e:
        print("pantry photo getFile error:", e, flush=True)
        send_message(chat_id, _t("errors.photo_analysis_failed", profile))
        return
    try:
        pantry = extract_pantry_from_photo(
            image_bytes,
            language=language_for_locale(i18n_mod.locale_of(profile)),
        )
    except Exception as e:
        print("pantry photo OCR error:", e, flush=True)
        send_message(chat_id, _t("errors.photo_analysis_failed", profile))
        return
    if not pantry or len(pantry.strip()) < 2:
        # Clear FSM so the user isn't stuck in pantry-input mode after a
        # bad photo; main_menu_keyboard surfaces the standard buttons again.
        set_awaiting_input(conn, user_id, None)
        send_message(
            chat_id,
            _t("pantry.photo_no_food", profile),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return
    # Echo the extracted list so the user has feedback before the recipe /
    # plan response lands. If they spot an error, /cancel mid-flow has no
    # effect (delegate already cleared FSM); they re-trigger from menu.
    send_message(
        chat_id,
        _t("pantry.photo_extracted", profile, items=_html.escape(pantry, quote=False)),
    )
    delegate(conn, chat_id, user_id, pantry, profile)


def handle_fridge_photo(conn, message: dict, profile: dict) -> None:
    """F-11 extension: photo of the fridge → OCR → /suggest_meal recipe."""
    _handle_pantry_photo(conn, message, profile, handle_fridge_input)


def handle_plan_pantry_photo(conn, message: dict, profile: dict) -> None:
    """F-10 extension: photo of the pantry → OCR → /plan with that list."""
    _handle_pantry_photo(conn, message, profile, handle_plan_pantry_input)


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
        answer_callback_query(cb_id, _t("toast.waiting_pantry", profile))
        set_awaiting_input(conn, user_id, "fridge_ingredients")
        send_message(chat_id, _t("fridge.prompt", profile))
        return

    if data == "suggest:variation":
        answer_callback_query(cb_id, _t("toast.preparing_other", profile))
        _run_suggest_meal(conn, chat_id, user_id, profile,
                          pantry="", extra_hint=_t("fridge.variation_hint", profile))
        return

    if data == "suggest:save":
        body = (message or {}).get("text") or ""
        if not body.strip():
            answer_callback_query(cb_id, _t("toast.something_wrong", profile))
            return
        try:
            save_recipe(conn, user_id, body, pantry="")
            answer_callback_query(cb_id, _t("toast.recipe_saved", profile))
        except Exception as e:
            error("save_recipe_failed", exc=e, user_id=user_id)
            answer_callback_query(cb_id, _t("toast.something_wrong", profile))
        return

    answer_callback_query(cb_id, _t("toast.unknown_action", profile))


def _portion_keyboard_for_product(product: dict, locale: str = "en") -> dict:
    """Inline keyboard mirroring api/barcode.py's portion picker.

    Kept here (not in lib/telegram_helpers.py) to avoid bloating that
    module — only handle_barcode_manual_input needs it.
    """
    serving = product.get("serving_size_g")
    rows = []
    if serving and 5 <= serving <= 5000:
        rows.append([{
            "text": i18n_mod.t("barcode.portion_label", locale=locale, grams=int(serving)),
            "callback_data": f"barcode:g:{int(serving)}",
        }])
    rows.append([
        {"text": i18n_mod.t("barcode.portion_50g",  locale=locale), "callback_data": "barcode:g:50"},
        {"text": i18n_mod.t("barcode.portion_100g", locale=locale), "callback_data": "barcode:g:100"},
        {"text": i18n_mod.t("barcode.portion_150g", locale=locale), "callback_data": "barcode:g:150"},
        {"text": i18n_mod.t("barcode.portion_200g", locale=locale), "callback_data": "barcode:g:200"},
    ])
    rows.append([{"text": i18n_mod.t("barcode.portion_custom", locale=locale), "callback_data": "barcode:g:custom"}])
    rows.append([{"text": i18n_mod.t("inline_button.cancel",   locale=locale), "callback_data": "barcode:cancel"}])
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
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if not off_mod.looks_like_ean(cleaned):
        send_message(chat_id, _t("barcode.manual_invalid", profile))
        return

    if not _enforce_quota(conn, chat_id, user_id, "meal_analysis"):
        set_awaiting_input(conn, user_id, None)
        return

    set_awaiting_input(conn, user_id, None)

    try:
        product = off_mod.lookup_product(cleaned)
    except Exception as e:
        error("off_lookup_failed", exc=e, ean=cleaned, user_id=user_id)
        send_message(chat_id, _t("barcode.lookup_failed", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if product is None:
        send_message(
            chat_id,
            _t("barcode.not_found", profile, ean=cleaned),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
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
        send_message(chat_id, _t("barcode.lookup_failed", profile))
        return

    send_message(
        chat_id,
        _t(
            "barcode.found_header", profile,
            name=product["name"],
            brand=product["brand"] or "—",
            kcal=int(round(product["per_100g"]["calories"])),
            p=int(round(product["per_100g"]["protein_g"])),
            f=int(round(product["per_100g"]["fat_g"])),
            c=int(round(product["per_100g"]["carbs_g"])),
        ),
        reply_markup=_portion_keyboard_for_product(product, locale=i18n_mod.locale_of(profile)),
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
    cleaned = text.strip().replace(",", ".").replace("г", "").replace("g", "").strip()  # noqa: i18n
    if cleaned.lower() in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        pop_pending_analysis(conn, user_id)
        send_message(chat_id, _t("meals_mgmt.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    grams = _parse_float(cleaned)
    if grams is None or not (1 <= grams <= 5000):
        send_message(chat_id, _t("barcode.grams_invalid", profile))
        return

    pending = get_pending_analysis(conn, user_id)
    if not pending:
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("barcode.pending_expired", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    _save_barcode_meal(conn, chat_id, user_id, first_name, profile, pending, float(grams))


# ---------- Meal management: Delete / Edit ----------

def handle_meal_manage_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    profile = get_profile(conn, user_id)

    if data.startswith("meal_del:"):
        meal_id = int(data.split(":", 1)[1])
        answer_callback_query(cb_id, _t("toast.deleting", profile))
        deleted = delete_meal(conn, meal_id, user_id)
        if not deleted:
            send_message(chat_id, _t("errors.meal_not_found", profile))
            return
        recalc_daily_log(conn, user_id, deleted["date"])
        send_message(
            chat_id,
            _t(
                "meals_mgmt.deleted", profile,
                dish=_html.escape(deleted["description"][:40], quote=False),
                cal=round(deleted["calories"]),
                kcal_unit=i18n_mod.t("macro.calories_short", locale=i18n_mod.locale_of(profile)),
            ),
        )

    elif data.startswith("meal_edit:"):
        meal_id = int(data.split(":", 1)[1])
        answer_callback_query(cb_id, _t("toast.getting_ready_swap", profile))
        meal = get_meal_by_id(conn, meal_id, user_id)
        if not meal:
            send_message(chat_id, _t("errors.meal_not_found", profile))
            return
        # Stage the existing meal as a pending analysis WITHOUT deleting it.
        # The user types a delta ("eggs 150g"); handle_manual_text_input
        # passes this analysis as `previous_analysis` so the AI patches it
        # instead of producing a fresh single-ingredient meal. The original
        # row only gets removed once the user confirms the new analysis —
        # cancellation mid-edit leaves the meal intact.
        save_pending_analysis(
            conn,
            user_id,
            meal_type=meal["meal_type"],
            analysis=_meal_to_analysis(meal),
            photo_file_id=meal.get("photo_file_id"),
            text_description=None,
            raw_response=meal.get("ai_raw_response") or "",
            replaces_meal_id=meal_id,
        )
        set_awaiting_manual(conn, user_id, meal_type=meal["meal_type"])
        # F1: explicit "new flow" — discard any lingering awaiting_input_type
        # (e.g. 'weight' set by the weekly cron and never cleared) so the
        # user's edit text doesn't get intercepted by the weight handler.
        set_awaiting_input(conn, user_id, None)
        send_message(
            chat_id,
            _t(
                "meals_mgmt.edit_prompt", profile,
                dish=_html.escape((meal.get("description") or "")[:40], quote=False),
            ),
            reply_markup=cancel_only_keyboard(locale=i18n_mod.locale_of(profile)),
        )


# ---------- Commands ----------

def handle_command(conn, message: dict, text: str, first_name: str | None, profile: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    cal_target = (profile or {}).get("daily_calorie_target") or 2000

    # Any slash command exits the active /ask thread (handle_ask itself
    # re-sets the state if the user reopens the thread). Without this,
    # /today / /water / /profile mid-thread would silently leave the
    # FSM in 'ask_thread' and the next plain text would re-enter chat.
    if cmd != "/ask" and (profile or {}).get("awaiting_input_type") == "ask_thread":
        set_awaiting_input(conn, user_id, None)

    if cmd == "/help":
        send_message(chat_id, help_message(i18n_mod.locale_of(profile)), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/profile":
        send_message(chat_id, format_profile(profile, locale=i18n_mod.locale_of(profile)), reply_markup=profile_edit_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/today" or cmd == "/meals":
        # Combined view (formerly split between /meals and /today): the
        # user gets today's meal list (with per-meal edit/delete inline
        # buttons) followed by the rich progress card (calorie bar,
        # macros, streak, quip). `/meals` stays as a typed alias for
        # backwards compat and slash-menu autocomplete.
        log = get_today_log(conn, user_id)
        meals = get_meals_for_day(conn, user_id, log["date"])
        streak_row = None
        try:
            streak_row = get_streak(conn, user_id)
        except Exception as _streak_exc:  # don't block the view on streak issues
            error("streak_fetch_failed", exc=_streak_exc, user_id=user_id)

        progress_card = format_today_progress(
            log, cal_target, first_name, profile=profile, streak=streak_row,
        )
        locale = i18n_mod.locale_of(profile)
        if meals:
            # Build meal list WITHOUT the compact daily-totals header —
            # the progress card below already covers that ground in
            # richer detail, so omitting the args avoids a duplicate.
            meal_list = format_meals_list(meals, locale=locale)
            macros = macro_gram_targets_from_profile(
                (profile or {}).get("weight_kg"),
                (profile or {}).get("goal") or "maintain",
            )
            # The meals list keyboard powers per-meal edit/delete from
            # inside this combined message, matching the old /meals UX.
            send_message(
                chat_id,
                meal_list + "\n\n" + progress_card,
                reply_markup=meals_list_keyboard(meals, locale=locale),
            )
        else:
            # No meals yet today — show just the progress card with the
            # persistent reply keyboard for quick navigation elsewhere.
            send_message(
                chat_id,
                progress_card,
                reply_markup=main_menu_keyboard(locale=locale),
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
            format_streak_summary(streak_row, first_name, locale=i18n_mod.locale_of(profile)),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    # F-8: open the barcode scanner Mini App.
    if cmd == "/scan":
        send_message(
            chat_id,
            _t("barcode.scan_intro", profile),
            reply_markup=scanner_inline_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    # F-9: restaurant menu OCR — set state + ask for a photo.
    if cmd == "/menu":
        set_awaiting_input(conn, user_id, "menu_photo")
        send_message(chat_id, _t("menu.prompt_intro", profile), reply_markup=cancel_only_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    # F-10: 3-day meal plan — ask the user for optional pantry items first.
    if cmd == "/plan":
        set_awaiting_input(conn, user_id, "plan_pantry")
        send_message(chat_id, _t("plan.intro", profile), reply_markup=plan_pantry_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    # F-12: shareable PNG recap card on demand.
    if cmd == "/recap":
        try:
            png, caption = recap_mod.build_user_recap(conn, user_id, profile, first_name)
        except Exception as e:
            error("recap_build_failed", exc=e, user_id=user_id)
            send_message(chat_id, _t("errors.recap_render_failed", profile),
                         reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
            return
        resp = send_photo(chat_id, png, caption=caption)
        if not resp.get("ok"):
            error("recap_send_failed", user_id=user_id, response=resp)
            send_message(chat_id, _t("errors.recap_send_failed", profile))
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
            format_aliases(aliases, first_name, locale=i18n_mod.locale_of(profile)),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    # F-5: dedicated goals view + weekly-delta editor.
    if cmd == "/goals":
        if not profile:
            send_message(chat_id, _t("goals.no_profile", profile))
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
                         status=status, first_name=first_name,
                         locale=i18n_mod.locale_of(profile)),
            reply_markup=goals_edit_keyboard(
                has_target=bool(profile.get("target_weight_kg")),
                has_delta=profile.get("weekly_delta_kg") is not None,
                locale=i18n_mod.locale_of(profile),
            ),
        )
        return

    if cmd == "/yesterday":
        from datetime import timedelta
        y = (now_user(profile) - timedelta(days=1)).strftime("%Y-%m-%d")
        log = get_log_for_date(conn, user_id, y)
        meals = get_meals_for_day(conn, user_id, y)
        send_message(chat_id, format_yesterday(log, meals, cal_target, first_name, profile=profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/history":
        rows = get_history(conn, user_id, days=7)
        send_message(chat_id, format_history(rows, cal_target, locale=i18n_mod.locale_of(profile)), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/history_detail":
        if not args:
            send_message(chat_id, _t("prompts.history_usage", profile))
            return
        date = args[0]
        meals = get_meals_for_day(conn, user_id, date)
        send_message(chat_id, format_day_detail(date, meals, locale=i18n_mod.locale_of(profile)))
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
                _t("ask.prompt", profile),
                reply_markup={"force_reply": True, "selective": True},
            )
        return

    if cmd == "/fav":
        meals = get_favorites(conn, user_id)
        locale = i18n_mod.locale_of(profile)
        if not meals:
            send_message(chat_id, _t("favorite.empty_list", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
            return
        lines = [_t("favorite.title", profile), ""] + [f"• {format_meal_list_entry(m, locale=locale)}" for m in meals[:20]]
        lines.append("")
        lines.append(_t("favorite.relog_hint", profile))
        send_message(chat_id, "\n".join(lines), reply_markup=recent_meals_keyboard(meals, variant="fav", locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/recent":
        meals = get_recent_meals(conn, user_id, limit=10)
        locale = i18n_mod.locale_of(profile)
        if not meals:
            send_message(chat_id, _t("favorite.recent_empty_list", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
            return
        lines = [_t("favorite.recent_title", profile), ""] + [f"• {format_meal_list_entry(m, locale=locale)}" for m in meals]
        lines.append("")
        lines.append(_t("favorite.recent_relog_hint", profile))
        send_message(chat_id, "\n".join(lines), reply_markup=recent_meals_keyboard(meals, variant="recent", locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/timezone":
        if not profile_is_complete(profile):
            send_message(chat_id, _t("timezone.not_onboarded", profile))
            return
        cur = (profile or {}).get("tz") or "Europe/Kyiv"
        send_message(
            chat_id,
            _t("timezone.prompt", profile, current=cur),
            reply_markup=tz_keyboard(prefix="tz:set", locale=i18n_mod.locale_of(profile)),
        )
        return

    if cmd == "/health":
        if not profile_is_complete(profile):
            send_message(chat_id, _t("health.not_onboarded", profile))
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
        send_message(chat_id, format_water(total, target, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if cmd == "/quiet":
        # Clear any in-flight free-text input so /quiet can't strand the user.
        if (profile or {}).get("awaiting_input_type"):
            set_awaiting_input(conn, user_id, None)
        currently_off = bool((profile or {}).get("nudge_optout"))
        set_nudge_optout(conn, user_id, not currently_off)
        key = "nudge.opted_in" if currently_off else "nudge.opted_out"
        send_message(
            chat_id,
            _t(key, profile),
            reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    if cmd == "/ai":
        # Combined AI helper. Sets transient FSM state so the smart-intent
        # text/voice classifier can fire if the user types instead of tapping.
        set_awaiting_input(conn, user_id, "ai_menu")
        send_message(
            chat_id,
            _t("ai_menu.title", profile),
            reply_markup=ai_menu_keyboard(locale=i18n_mod.locale_of(profile)),
        )
        return

    if cmd == "/ask_new":
        n = clear_chat_history(conn, user_id)
        if (profile or {}).get("awaiting_input_type") == "ask_thread":
            set_awaiting_input(conn, user_id, None)
        key = "ask.thread_cleared" if n > 0 else "ask.thread_already_empty"
        send_message(chat_id, _t(key, profile))
        return

    if cmd == "/recipes":
        rows = list_recipes(conn, user_id, limit=20)
        locale = i18n_mod.locale_of(profile)
        if not rows:
            send_message(chat_id, _t("recipes.empty", profile))
            return
        header = _t("recipes.header", profile, n=len(rows))
        lines = [header]
        kb_rows: list[list[dict]] = []
        for i, r in enumerate(rows, 1):
            when = (r["created_at"] or "")[:10]  # YYYY-MM-DD
            first_line = (r["body"] or "").strip().splitlines()[0] if r["body"] else ""
            preview = first_line[:80]
            lines.append(f"{i}. <i>{_html.escape(when, quote=False)}</i> — {_html.escape(preview, quote=False)}")
            kb_rows.append([
                {"text": _t("recipes.show_full_n", profile, n=i),
                 "callback_data": f"rec:show:{r['id']}"},
                {"text": i18n_mod.t("inline_button.delete_n", locale=locale, n=i),
                 "callback_data": f"rec:del:{r['id']}"},
            ])
        send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": kb_rows})
        return

    send_message(chat_id, _t("errors.unknown_command", profile))


# ---------- Favorites / Recent / Undo callbacks ----------

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


# _MEAL_TYPE_UA dropped — meal-type label lookups now use lib/i18n meal_type.* keys.


def handle_fav_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")
    profile = get_profile(conn, user_id) or {}

    parts = data.split(":")
    if len(parts) != 3:
        answer_callback_query(cb_id, _t("toast.error", profile))
        return
    try:
        meal_id = int(parts[1])
    except ValueError:
        answer_callback_query(cb_id, _t("toast.error", profile))
        return
    target_state = parts[2] == "1"
    ok = set_favorite(conn, meal_id, user_id, target_state)
    if not ok:
        answer_callback_query(cb_id, _t("toast.meal_not_found", profile))
        return
    answer_callback_query(cb_id, _t("favorite.added" if target_state else "favorite.removed", profile))
    if message_id:
        try:
            edit_message_reply_markup(
                chat_id, message_id,
                meal_logged_actions_keyboard(meal_id, is_fav=target_state, locale=i18n_mod.locale_of(profile)),
            )
        except Exception as e:
            print("edit_reply_markup error:", e, flush=True)


def handle_relog_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    profile = get_profile(conn, user_id) or {}

    try:
        meal_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, _t("toast.error", profile))
        return

    src = get_meal_by_id(conn, meal_id, user_id)
    if not src:
        answer_callback_query(cb_id, _t("toast.meal_not_found", profile))
        return

    meal_type = _meal_type_by_local_hour(profile)
    new_id = clone_meal_for_today(conn, meal_id, user_id, meal_type)
    if not new_id:
        answer_callback_query(cb_id, _t("toast.failed", profile))
        send_message(chat_id, _t("relog.failed", profile))
        return
    answer_callback_query(cb_id, _t("toast.saved_check", profile))
    locale = i18n_mod.locale_of(profile)
    meal_type_label = i18n_mod.t(f"meal_type.{meal_type}", locale=locale) if meal_type else meal_type
    send_message(
        chat_id,
        _t(
            "relog.done", profile,
            dish=_html.escape((src.get("description") or "—")[:40], quote=False),
            meal_type=meal_type_label,
        ),
        reply_markup=undo_relog_keyboard(new_id, locale=i18n_mod.locale_of(profile)),
    )


def handle_undo_callback(conn, cb: dict) -> None:
    from datetime import datetime, timezone, timedelta
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")
    profile = get_profile(conn, user_id) or {}

    try:
        meal_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        answer_callback_query(cb_id, _t("toast.error", profile))
        return

    meal = get_meal_by_id(conn, meal_id, user_id)
    if not meal:
        answer_callback_query(cb_id, _t("toast.already_gone", profile))
        return

    # 10-min TTL
    try:
        created = datetime.fromisoformat(meal["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=10):
            answer_callback_query(cb_id, _t("toast.too_late", profile))
            send_message(chat_id, _t("undo.expired", profile))
            return
    except Exception:
        pass

    deleted = delete_meal(conn, meal_id, user_id)
    if not deleted:
        answer_callback_query(cb_id, _t("toast.not_found", profile))
        return
    recalc_daily_log(conn, user_id, deleted["date"])
    answer_callback_query(cb_id, _t("undo.done", profile))
    if message_id:
        try:
            safe_desc = _html.escape(deleted["description"][:40], quote=False)
            edit_message_text(chat_id, message_id, _t("undo.message_text", profile, desc=safe_desc))
        except Exception:
            pass


# ---------- Water callbacks ----------

def handle_water_quickadd(conn, chat_id: int, user_id: int, amount_ml: int) -> None:
    profile = get_profile(conn, user_id) or {}
    total = add_water(conn, user_id, amount_ml)
    target = get_water_target(conn, user_id)
    send_message(chat_id, format_water(total, target, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))


def handle_water_callback(conn, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb["data"]
    user_id = cb["from"]["id"]
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id", user_id)
    message_id = message.get("message_id")
    profile = get_profile(conn, user_id) or {}

    parts = data.split(":")
    # Forms: water:add:<ml>, water:undo, water:goal, water:goal:set:<ml>, water:back
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "add" and len(parts) == 3:
        try:
            ml = int(parts[2])
        except ValueError:
            answer_callback_query(cb_id, _t("toast.error", profile))
            return
        if ml not in (200, 250, 300, 500, 750):
            answer_callback_query(cb_id, _t("toast.invalid_short", profile))
            return
        total = add_water(conn, user_id, ml)
        target = get_water_target(conn, user_id)
        answer_callback_query(cb_id, _t("toast.water_added_ml", profile, ml=ml))
        if message_id:
            edit_message_text(chat_id, message_id, format_water(total, target, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))
        else:
            send_message(chat_id, format_water(total, target, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if sub == "undo":
        new_total = remove_last_water_today(conn, user_id)
        if new_total is None:
            answer_callback_query(cb_id, _t("water.undo_empty", profile))
            return
        target = get_water_target(conn, user_id)
        answer_callback_query(cb_id, _t("toast.reverted", profile))
        if message_id:
            edit_message_text(chat_id, message_id, format_water(new_total, target, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if sub == "goal" and len(parts) == 2:
        answer_callback_query(cb_id)
        if message_id:
            edit_message_text(chat_id, message_id, _t("water.goal_prompt", profile), reply_markup=water_goal_keyboard(locale=i18n_mod.locale_of(profile)))
        else:
            send_message(chat_id, _t("water.goal_prompt", profile), reply_markup=water_goal_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if sub == "goal" and len(parts) == 4 and parts[2] == "set":
        try:
            ml = int(parts[3])
        except ValueError:
            answer_callback_query(cb_id, _t("toast.error", profile))
            return
        set_water_target(conn, user_id, ml, overridden=True)
        answer_callback_query(cb_id, _t("water.goal_saved", profile, target=ml))
        total = get_water_today(conn, user_id)
        if message_id:
            edit_message_text(chat_id, message_id, format_water(total, ml, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    if sub == "back":
        answer_callback_query(cb_id)
        total = get_water_today(conn, user_id)
        target = get_water_target(conn, user_id)
        if message_id:
            edit_message_text(chat_id, message_id, format_water(total, target, locale=i18n_mod.locale_of(profile)), reply_markup=water_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    answer_callback_query(cb_id, _t("toast.unknown_action", profile))


# ---------- /ask chat mode ----------

def handle_ask(conn, user_id: int, chat_id: int, question: str, profile: dict) -> None:
    if not _enforce_quota(conn, chat_id, user_id, "ask"):
        return
    send_message(chat_id, _t("ask.thinking", profile))
    try:
        today_log = get_today_log(conn, user_id)
        today_meals = get_meals_for_day(conn, user_id, today_log["date"])
        history = get_chat_history(conn, user_id, limit=10, minutes=60)
        answer = ask_chat(question, history, today_log, today_meals, profile, language=language_for_locale(i18n_mod.locale_of(profile)))
    except Exception as e:
        print("ask_chat error:", traceback.format_exc(), flush=True)
        send_message(chat_id, _t("ask.error", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    append_chat_message(conn, user_id, "user", question)
    append_chat_message(conn, user_id, "assistant", answer)
    # Mark the thread active so the next plain-text / voice message continues
    # the conversation instead of falling through to meal logging. Cleared by
    # /cancel, /ask_new, or any slash command other than /ask.
    set_awaiting_input(conn, user_id, "ask_thread")
    # Thread indicator: gate at n>=4 (= 2 user-turns + 2 assistant-turns) so the
    # very first reply doesn't get cluttered.
    n = count_chat_messages(conn, user_id, minutes=60)
    footer = ("\n\n" + _t("ask.thread_footer", profile, n=n)) if n >= 4 else ""
    send_message(chat_id, answer + footer,
                 reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))


def handle_ask_photo(conn, message: dict, profile: dict) -> None:
    """Vision-aware /ask: photo replied to ask.prompt → vision Q&A.
    Caption (if any) is the question; missing caption uses the default
    `ask.photo_default_question` i18n key."""
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    if not _enforce_quota(conn, chat_id, user_id, "ask"):
        return
    photos = message.get("photo") or []
    largest = photos[-1] if photos else {}
    file_id = largest.get("file_id")
    if not file_id:
        return
    file_size = int(largest.get("file_size") or 0)
    if file_size > MAX_PHOTO_BYTES:
        send_message(chat_id, _t("errors.photo_too_large", profile))
        return
    caption = (message.get("caption") or "").strip() \
        or _t("ask.photo_default_question", profile)
    send_message(chat_id, _t("ask.thinking", profile))
    try:
        image_bytes = get_file_bytes(file_id)
    except Exception as e:
        error("ask_photo_getfile_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("ask.error", profile),
                     reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return
    try:
        today_log = get_today_log(conn, user_id)
        today_meals = get_meals_for_day(conn, user_id, today_log["date"])
        history = get_chat_history(conn, user_id, limit=10, minutes=60)
        answer = ask_chat_with_photo(
            image_bytes, caption, history, today_log, today_meals, profile,
            language=language_for_locale(i18n_mod.locale_of(profile)),
        )
    except Exception as e:
        error("ask_photo_failed", exc=e, user_id=user_id)
        send_message(chat_id, _t("ask.error", profile),
                     reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return
    append_chat_message(conn, user_id, "user", f"[photo] {caption}")
    append_chat_message(conn, user_id, "assistant", answer)
    set_awaiting_input(conn, user_id, "ask_thread")
    n = count_chat_messages(conn, user_id, minutes=60)
    footer = ("\n\n" + _t("ask.thread_footer", profile, n=n)) if n >= 4 else ""
    send_message(chat_id, answer + footer,
                 reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))


# ---------- Weight / goal edit ----------

def _goal_label(goal: str, locale: str = "en") -> str:
    """Locale-aware goal label, reused from goal_keyboard.* dict keys."""
    return i18n_mod.t(f"goal_keyboard.{goal}", locale=locale)


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
    locale: str = "en",
) -> str:
    lines = []
    if old_weight:
        delta_kg = float(new_weight) - float(old_weight)
        delta_g = round(delta_kg * 1000)
        if delta_g == 0:
            delta_txt = i18n_mod.t("weight.recap_no_change", locale=locale)
        else:
            delta_txt = i18n_mod.t(
                "weight.recap_delta_g", locale=locale,
                sign="+" if delta_g > 0 else "",
                delta_g=delta_g,
            )
        lines.append(i18n_mod.t("weight.recap_logged", locale=locale, weight=new_weight, delta_txt=delta_txt))
    else:
        lines.append(i18n_mod.t("weight.recap_logged_simple", locale=locale, weight=new_weight))

    if target_weight and goal in ("lose", "gain"):
        delta = float(new_weight) - float(target_weight)  # + = need to lose, − = need to gain
        if goal == "lose":
            togo = max(0.0, delta)
            reached = togo <= 0.05
            tail = i18n_mod.t("weight.recap_target_done", locale=locale) if reached \
                else i18n_mod.t("weight.recap_target_lose", locale=locale, togo=f"{togo:.1f}")
        else:
            togo = max(0.0, -delta)
            reached = togo <= 0.05
            tail = i18n_mod.t("weight.recap_target_done", locale=locale) if reached \
                else i18n_mod.t("weight.recap_target_gain", locale=locale, togo=f"{togo:.1f}")
        lines.append(i18n_mod.t("weight.recap_target_line", locale=locale, target=target_weight, tail=tail))

    lines.append(i18n_mod.t("weight.recap_new_target", locale=locale, cal=new_cal))
    lines.append(i18n_mod.t("weight.recap_macros", locale=locale, p=macros["protein"], c=macros["carbs"], f=macros["fat"]))
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
        send_message(chat_id, _t("weight.checkin_skipped", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    new_weight = _parse_float(cleaned)
    if new_weight is None:
        send_message(chat_id, _t("weight.not_a_number", profile))
        return
    if not (30 <= new_weight <= 300):
        send_message(chat_id, _t("weight.invalid", profile))
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
        locale=i18n_mod.locale_of(profile),
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
        line = format_projection_line(projection, status=status, locale=i18n_mod.locale_of(profile))
        if line:
            body = body + "\n" + line
    except Exception as _px:
        error("goals_projection_failed", exc=_px, user_id=user_id)

    send_message(
        chat_id,
        body,
        reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
    )


def handle_water_target_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
) -> None:
    """Process a manual water-goal reply (ml) from the /profile → Water goal flow."""
    profile = get_profile(conn, user_id) or {}
    cleaned = text.strip().lower().replace("мл", "").replace("ml", "").strip()  # noqa: i18n
    if cleaned in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("common.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    ml = _parse_int(cleaned)
    if ml is None:
        send_message(chat_id, _t("profile.water_invalid_int", profile))
        return
    if not (1500 <= ml <= 4000):
        send_message(chat_id, _t("profile.water_range", profile))
        return

    set_water_target(conn, user_id, ml, overridden=True)
    set_awaiting_input(conn, user_id, None)
    locale = i18n_mod.locale_of(profile)
    total = get_water_today(conn, user_id)
    send_message(
        chat_id,
        _t("water.goal_saved", profile, target=ml) + "\n\n" + format_water(total, ml, locale=locale),
        reply_markup=water_keyboard(locale=locale),
    )


def handle_target_weight_input(
    conn,
    chat_id: int,
    user_id: int,
    text: str,
    profile: dict,
) -> None:
    """Process a target-weight reply from the /profile → Target weight flow."""
    cleaned = text.strip().lower().replace("кг", "").replace("kg", "").strip()  # noqa: i18n
    if cleaned in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("common.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    tw = _parse_float(cleaned)
    if tw is None:
        send_message(chat_id, _t("weight.not_a_number", profile))
        return
    if not (30 <= tw <= 300):
        send_message(chat_id, _t("target_weight.invalid", profile))
        return
    current_w = profile.get("weight_kg")
    goal = profile.get("goal") or "maintain"
    if current_w is not None:
        if goal == "lose" and tw >= float(current_w):
            send_message(chat_id, _t("target_weight.lose_mismatch", profile, current=current_w))
            return
        if goal == "gain" and tw <= float(current_w):
            send_message(chat_id, _t("target_weight.gain_mismatch", profile, current=current_w))
            return

    update_profile(conn, user_id, target_weight_kg=float(tw))
    set_awaiting_input(conn, user_id, None)
    send_message(
        chat_id,
        _t("target_weight.saved", profile, target=tw),
        reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
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
            .replace("кг", "")  # noqa: i18n
            .replace("kg", "")
            .replace(",", ".")
            .strip()
    )
    if cleaned in ("/skip", "skip", "/cancel", "cancel"):
        set_awaiting_input(conn, user_id, None)
        send_message(chat_id, _t("common.cancelled", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    raw = _parse_float(cleaned)
    if raw is None:
        send_message(chat_id, _t("goals.weekly_invalid", profile))
        return

    magnitude = abs(raw)
    if not (0.1 <= magnitude <= 2.0):
        send_message(chat_id, _t("goals.weekly_invalid", profile))
        return

    goal = profile.get("goal") or "maintain"
    if goal == "maintain":
        send_message(chat_id, _t("goals.weekly_not_for_maintain", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
        set_awaiting_input(conn, user_id, None)
        return

    # The prompt + WRONG_SIGN copy both ask the user to type a POSITIVE number;
    # we apply the sign automatically based on goal direction.
    #   - lose: + (as instructed) or − (already-signed) both accepted.
    #   - gain: − is wrong-direction (wants up, typed down) → reject.
    if raw < 0 and goal == "gain":
        send_message(chat_id, _t("goals.weekly_wrong_sign", profile))
        return

    signed = -magnitude if goal == "lose" else magnitude
    update_profile(conn, user_id, weekly_delta_kg=float(signed))
    set_awaiting_input(conn, user_id, None)
    send_message(
        chat_id,
        _t("goals.weekly_saved", profile, delta=signed),
        reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)),
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
        answer_callback_query(cb_id, _t("toast.waiting_weight", profile))
        set_awaiting_input(conn, user_id, "weight")
        send_message(chat_id, _t("weight.input_prompt", profile))
        return

    # prof:goal → show the goal picker.
    if data == "prof:goal":
        answer_callback_query(cb_id)
        send_message(chat_id, _t("goal.update_prompt", profile), reply_markup=profile_goal_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    # prof:goal:<lose|maintain|gain> → apply the goal change.
    if data.startswith("prof:goal:"):
        new_goal = data.split(":", 2)[2]
        if new_goal not in _VALID_GOALS:
            answer_callback_query(cb_id, _t("toast.invalid", profile))
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
        answer_callback_query(cb_id, _t("toast.goal_updated", profile))
        macros = macro_gram_targets_from_profile(float(weight), new_goal)
        locale = i18n_mod.locale_of(profile)
        goal_label = _goal_label(new_goal, locale=locale)
        send_message(
            chat_id,
            f"{_t('goal.updated', profile, goal=goal_label)}\n"
            + _t("profile.recompute_msg", profile, cal=new_cal, p=macros["protein"], c=macros["carbs"], f=macros["fat"]),
            reply_markup=main_menu_keyboard(locale=locale),
        )
        if new_goal in ("lose", "gain"):
            set_awaiting_input(conn, user_id, "target_weight")
            prompt = _t("target_weight.ask_lose" if new_goal == "lose" else "target_weight.ask_gain", profile)
            send_message(chat_id, prompt)
        else:
            send_message(chat_id, _t("target_weight.cleared", profile))
        return

    # prof:target_weight → prompt for the motivation target.
    if data == "prof:target_weight":
        goal = profile.get("goal") or "maintain"
        if goal == "maintain":
            answer_callback_query(cb_id, _t("toast.not_needed_for_goal", profile))
            send_message(chat_id, _t("target_weight.cleared", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
            return
        answer_callback_query(cb_id, _t("toast.waiting_target_number", profile))
        set_awaiting_input(conn, user_id, "target_weight")
        send_message(chat_id,
                     _t("target_weight.ask_lose" if goal == "lose" else "target_weight.ask_gain", profile))
        return

    # F-5: prof:weekly_delta → prompt for kg/week target.
    if data == "prof:weekly_delta":
        goal = profile.get("goal") or "maintain"
        if goal == "maintain":
            answer_callback_query(cb_id, _t("toast.not_needed_for_goal", profile))
            send_message(chat_id, _t("goals.weekly_not_for_maintain", profile), reply_markup=main_menu_keyboard(locale=i18n_mod.locale_of(profile)))
            return
        answer_callback_query(cb_id, _t("toast.waiting_pace_number", profile))
        set_awaiting_input(conn, user_id, "weekly_delta")
        send_message(
            chat_id,
            _t("goals.weekly_ask_lose" if goal == "lose" else "goals.weekly_ask_gain", profile),
        )
        return

    # prof:water → show preset picker (reuses the existing water_goal_keyboard).
    if data == "prof:water":
        answer_callback_query(cb_id)
        send_message(chat_id, _t("water.goal_prompt", profile), reply_markup=water_goal_keyboard(locale=i18n_mod.locale_of(profile)))
        return

    # prof:water:custom → prompt for manual ml entry, FSM picks it up.
    if data == "prof:water:custom":
        answer_callback_query(cb_id, _t("toast.waiting_water_number", profile))
        set_awaiting_input(conn, user_id, "water_target")
        send_message(chat_id, _t("profile.water_prompt", profile))
        return

    answer_callback_query(cb_id, _t("toast.unknown_action", profile))
