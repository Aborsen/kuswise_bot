#!/usr/bin/env python3
"""Three-cohort rescue for users stranded by today's onboarding cleanup.

Action A (13 users at `awaiting_lang_confirm`)
    → advance to `awaiting_sex`, stamp `lang_confirmed_at`, send the
      new welcome + sex question. Same end state as if they had tapped
      one of the cached lang_confirm buttons.

Action B (12 users at `awaiting_age` / `awaiting_target_weight` /
        `awaiting_gym` / `awaiting_weight`)
    → send a "👋 one step away" kicker + re-send the question for
      their current step (with the right keyboard). No state change.
      Stamps `nudge_mid_flow_sent_at` for idempotency.

Action C (1 user at `awaiting_tz_custom`)
    → stranded mid-tz-step when we removed that step today.
      Stamp `tz='Europe/Kyiv'`, advance to `done`, water-target
      upsert, send the "🎉 All set" message, post the admin-channel
      notification (stamping `admin_notified_at` on success).

Hardcoded user_id lists per cohort. Idempotent — re-runs become
no-ops because each user's step / stamp value changes after the
first successful pass.

Usage::

    .venv/bin/python scripts/backfill_stuck_users_today.py
    .venv/bin/python scripts/backfill_stuck_users_today.py --dry-run

Reads `TELEGRAM_BOT_TOKEN`, `ADMIN_NOTIFY_CHAT_ID`, `DATABASE_URL`
from env / `.env`.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

from lib.config import ADMIN_NOTIFY_CHAT_ID
from lib import i18n as i18n_mod
from lib.database import (
    get_conn,
    get_profile,
    set_blocked,
    update_profile,
    upsert_water_target_from_profile,
)
from lib.formatters import format_new_user_notification
from lib.telegram_helpers import (
    gym_keyboard,
    main_menu_keyboard,
    send_message,
    sex_keyboard,
)


# ---------- Cohort lists (hardcoded, audited from 2026-05-27 18:00 UTC) ----------

_ACTION_A_LANG_CONFIRM = [
    1493116852,  # Zeyn      en  site_blog_best-telegram-diet-bot
    305568044,   # 0633627396 uk
    1576438678,  # Valeri    uk
    914726156,   # 𝒜𝓃𝒶𝓈𝓉𝒶𝓈𝒾𝒶 uk
    675323404,   # Богдан    uk
    935140786,   # Настя     uk
    733852966,   # Olga      uk
    548395915,   # Victoria  uk
    833555443,   # A         uk
    287867201,   # Vitalina  en
    767091800,   # Iryna     uk  site_banner_home_uk
    526179512,   # Анна      uk
    941733473,   # Elena     uk  site_banner_home_uk
]

_ACTION_B_MID_FLOW = [
    # awaiting_age (8)
    8385032134,   # Aron
    928814452,    # Denys
    410350445,    # Sergii
    340080391,    # Mykhailo
    1676186775,   # Yura
    781417190,    # Анастасия
    2036702279,   # Оля Шевченко-Зарудняя
    7648520537,   # Maryna | Head of QC
    # awaiting_target_weight (2)
    493356679,    # Julia
    5614518306,   # Margo
    # awaiting_gym (1)
    527532976,    # Діана
    # awaiting_weight (1)
    470425369,    # Mariia
]

_ACTION_C_TZ_CUSTOM = [
    622839673,    # Алена
]

# Inter-send delay. 26 messages total — well under Telegram's 30 msg/s
# global cap. Matches the morning cron's pacing.
_SEND_DELAY_S = 0.04


# ---------- Helpers ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_with_autoblock(conn, user_id: int, text: str, reply_markup=None) -> str:
    """Send + auto-stamp blocked_at on Telegram 400/403. Same semantics
    as the morning + evening crons."""
    if reply_markup is not None:
        resp = send_message(user_id, text, reply_markup=reply_markup)
    else:
        resp = send_message(user_id, text)
    if isinstance(resp, dict) and resp.get("ok") is False:
        if resp.get("error_code") in (400, 403):
            try:
                set_blocked(conn, user_id, True)
            except Exception:
                pass
            return "blocked"
        return "failed"
    return "sent"


def _step_prompt_and_keyboard(profile: dict, lang: str):
    """For Action B: return (prompt_text, reply_markup) for the user's
    current onboarding step. None for keyboard when the step expects
    a typed answer."""
    step = profile.get("onboarding_step") or ""
    if step == "awaiting_age":
        return i18n_mod.t("onboarding.ask_age", locale=lang), None
    if step == "awaiting_weight":
        return i18n_mod.t("onboarding.ask_weight", locale=lang), None
    if step == "awaiting_height":
        return i18n_mod.t("onboarding.ask_height", locale=lang), None
    if step == "awaiting_gym":
        return i18n_mod.t("onboarding.ask_gym", locale=lang), gym_keyboard()
    if step == "awaiting_target_weight":
        goal = profile.get("goal") or "maintain"
        key = ("target_weight.ask_lose" if goal == "lose"
               else "target_weight.ask_gain")
        return i18n_mod.t(key, locale=lang), None
    return None, None


# ---------- Action A: lang_confirm rescue ----------


def _process_action_a(conn, uid: int, dry_run: bool) -> str:
    profile = get_profile(conn, uid)
    if not profile:
        return f"! uid={uid:>10} A skipped (no profile)"
    step = profile.get("onboarding_step") or ""
    if step != "awaiting_lang_confirm":
        return (f"⏭ uid={uid:>10} A already advanced "
                f"(step={step!r})")
    lang = profile.get("lang") or "en"

    if dry_run:
        return f"DRY uid={uid:>10} A ({lang}) would advance → awaiting_sex"

    update_profile(
        conn, uid,
        lang=lang,
        lang_confirmed_at=_now_iso(),
        onboarding_step="awaiting_sex",
    )
    # Intro then ask_sex with the sex keyboard.
    o1 = _send_with_autoblock(
        conn, uid, i18n_mod.t("onboarding.intro", locale=lang),
    )
    if o1 == "blocked":
        return f"🚫 uid={uid:>10} A ({lang}) blocked"
    if o1 == "failed":
        return f"⚠ uid={uid:>10} A ({lang}) intro send failed"
    time.sleep(_SEND_DELAY_S)
    o2 = _send_with_autoblock(
        conn, uid, i18n_mod.t("onboarding.ask_sex", locale=lang),
        reply_markup=sex_keyboard(locale=lang),
    )
    if o2 == "blocked":
        return f"🚫 uid={uid:>10} A ({lang}) blocked on ask_sex"
    if o2 == "failed":
        return f"⚠ uid={uid:>10} A ({lang}) ask_sex send failed"
    return f"✓ uid={uid:>10} A ({lang}) advanced + welcomed"


# ---------- Action B: mid-flow kicker ----------


def _process_action_b(conn, uid: int, dry_run: bool) -> str:
    profile = get_profile(conn, uid)
    if not profile:
        return f"! uid={uid:>10} B skipped (no profile)"
    if profile.get("nudge_mid_flow_sent_at"):
        return (f"⏭ uid={uid:>10} B already nudged at "
                f"{profile['nudge_mid_flow_sent_at']}")
    if (profile.get("onboarding_step") or "") == "done":
        return f"⏭ uid={uid:>10} B already done"
    lang = profile.get("lang") or "en"
    prompt, kb = _step_prompt_and_keyboard(profile, lang)
    if prompt is None:
        return (f"! uid={uid:>10} B skipped — unrecognised step "
                f"{profile.get('onboarding_step')!r}")

    if dry_run:
        return (f"DRY uid={uid:>10} B ({lang}) would kicker+re-ask "
                f"{profile.get('onboarding_step')}")

    # Send the kicker followed by the step prompt (re-attaching its
    # keyboard where the step expects one). Two messages so the kicker
    # has its own bubble — easier to skim than a long combined text.
    o1 = _send_with_autoblock(
        conn, uid, i18n_mod.t("nudge.mid_onboarding_kicker", locale=lang),
    )
    if o1 == "blocked":
        return f"🚫 uid={uid:>10} B ({lang}) blocked"
    if o1 == "failed":
        return f"⚠ uid={uid:>10} B ({lang}) kicker send failed"
    time.sleep(_SEND_DELAY_S)
    o2 = _send_with_autoblock(conn, uid, prompt, reply_markup=kb)
    if o2 == "blocked":
        return f"🚫 uid={uid:>10} B ({lang}) blocked on prompt"
    if o2 == "failed":
        return f"⚠ uid={uid:>10} B ({lang}) prompt send failed"

    update_profile(conn, uid, nudge_mid_flow_sent_at=_now_iso())
    return (f"✓ uid={uid:>10} B ({lang}) nudged at "
            f"{profile.get('onboarding_step')}")


# ---------- Action C: finalize awaiting_tz_custom ----------


def _process_action_c(conn, uid: int, dry_run: bool) -> str:
    profile = get_profile(conn, uid)
    if not profile:
        return f"! uid={uid:>10} C skipped (no profile)"
    if (profile.get("onboarding_step") or "") == "done":
        return f"⏭ uid={uid:>10} C already done"
    lang = profile.get("lang") or "en"
    first_name_row = ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(first_name, ''), COALESCE(username, '') "
                "FROM users WHERE user_id = %s", (uid,))
            row = cur.fetchone()
        first_name_row = (row[0] if row else "") or ""
        username = (row[1] if row else "") or ""
    except Exception:
        username = ""
    cal = int(profile.get("daily_calorie_target") or
              profile.get("recommended_calorie_target") or 2000)
    weight = profile.get("weight_kg")

    if dry_run:
        return (f"DRY uid={uid:>10} C ({lang}) would finalize "
                f"@ {cal} kcal, tz=Europe/Kyiv, name={first_name_row!r}")

    # Step 1: stamp the finalised state.
    update_profile(
        conn, uid,
        tz="Europe/Kyiv",
        onboarding_step="done",
    )
    # Step 2: water target from weight (best-effort).
    water = None
    try:
        if weight is not None:
            water = upsert_water_target_from_profile(conn, uid, float(weight))
    except Exception:
        pass

    # Step 3: welcome message.
    done_text = i18n_mod.t(
        "onboarding.done", locale=lang,
        name=first_name_row or i18n_mod.t("onboarding.default_name", locale=lang),
        cal=cal, water=water or 2000,
    )
    o1 = _send_with_autoblock(
        conn, uid, done_text,
        reply_markup=main_menu_keyboard(locale=lang),
    )
    if o1 == "blocked":
        return f"🚫 uid={uid:>10} C ({lang}) blocked on done"
    if o1 == "failed":
        return f"⚠ uid={uid:>10} C ({lang}) done send failed"

    # Step 4: admin-channel post (best-effort, gated on
    # ADMIN_NOTIFY_CHAT_ID being set + the send landing).
    admin_sent = False
    if ADMIN_NOTIFY_CHAT_ID:
        try:
            admin_chat = int(ADMIN_NOTIFY_CHAT_ID)
            admin_text = format_new_user_notification(
                {**profile, "tz": "Europe/Kyiv",
                 "daily_calorie_target": cal,
                 "onboarding_step": "done"},
                username, first_name_row,
            )
            resp = send_message(admin_chat, admin_text)
            if isinstance(resp, dict) and resp.get("ok"):
                admin_sent = True
                update_profile(conn, uid, admin_notified_at=_now_iso())
        except Exception:
            pass

    suffix = " + admin-posted" if admin_sent else " (no admin post)"
    return f"✓ uid={uid:>10} C ({lang}) finalised @ {cal} kcal{suffix}"


# ---------- Orchestrator ----------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen for each user without writing "
             "to the DB or calling Telegram.",
    )
    args = parser.parse_args()

    conn = get_conn()
    a_done = b_done = c_done = 0
    try:
        print("=== Action A: lang_confirm rescue ===")
        for uid in _ACTION_A_LANG_CONFIRM:
            outcome = _process_action_a(conn, uid, dry_run=args.dry_run)
            print(outcome, flush=True)
            if outcome.startswith("✓"):
                a_done += 1
            time.sleep(_SEND_DELAY_S)

        print("\n=== Action B: mid-flow kicker ===")
        for uid in _ACTION_B_MID_FLOW:
            outcome = _process_action_b(conn, uid, dry_run=args.dry_run)
            print(outcome, flush=True)
            if outcome.startswith("✓"):
                b_done += 1
            time.sleep(_SEND_DELAY_S)

        print("\n=== Action C: finalize awaiting_tz_custom ===")
        for uid in _ACTION_C_TZ_CUSTOM:
            outcome = _process_action_c(conn, uid, dry_run=args.dry_run)
            print(outcome, flush=True)
            if outcome.startswith("✓"):
                c_done += 1
            time.sleep(_SEND_DELAY_S)

        print(f"\nDone. A={a_done}/13, B={b_done}/12, C={c_done}/1.")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
