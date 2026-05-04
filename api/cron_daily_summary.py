"""Vercel Cron endpoint — runs daily at 20:00 UTC.

Per opt-in, onboarded user:
  (1) meals today          → GPT-4o end-of-day summary (`summary_sent=1` flag)
  (2) zero today, recent   → static `nudge.zero_today` (active in last 7 days)
  (2) zero today, stale    → static `nudge.come_back`  (no meals 7+ days OR
                             never logged), gated by 3-day `last_nudge_sent_at`
                             cooldown to avoid spam

Carries over the deleted `cron_inactivity_nudge`'s safety behavior: when
Telegram returns 400/403 we auto-flip `nudge_optout=1` so we don't keep
blasting blocked or vanished chats.
"""
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import CRON_SECRET, language_for_locale
from lib.database import (
    get_conn,
    init_db,
    get_users_needing_summary,
    get_users_to_nudge,
    mark_nudge_sent,
    get_today_log,
    get_meals_for_day,
    save_recommendation,
    mark_summary_sent,
    set_nudge_optout,
    get_profile,
    profile_is_complete,
)
from lib.telegram_helpers import send_message, nudge_optout_keyboard
from lib.openai_nutrition import generate_daily_summary
from lib import i18n as i18n_mod
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_daily_summary")

# Telegram global rate cap is 30 msg/sec; 40ms keeps us comfortably under.
_SEND_DELAY_S = 0.04


def _authorized(headers) -> bool:
    """Verify Vercel Cron bearer token. Fails closed if CRON_SECRET is not set.
    Uses constant-time comparison to resist timing attacks."""
    if not CRON_SECRET:
        return False  # fail closed — refuse to serve when not configured
    auth = headers.get("Authorization", "")
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))


class handler(BaseHTTPRequestHandler):
    @http_handler("cron_daily_summary")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        result = {"ok": True, "sent_summary": 0, "sent_recent": 0, "sent_stale": 0, "errors": []}
        try:
            result = run_daily_summary()
        except Exception as exc:
            error("cron_daily_summary_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def _send_with_autoptout(conn, user_id: int, text: str, reply_markup=None) -> str:
    """Send a Telegram message and auto-opt-out on 400/403.

    Returns "sent", "blocked" (auto-opted-out), or "failed".
    """
    resp = send_message(user_id, text, reply_markup=reply_markup) if reply_markup is not None \
        else send_message(user_id, text)
    if isinstance(resp, dict) and resp.get("ok") is False:
        if resp.get("error_code") in (400, 403):
            set_nudge_optout(conn, user_id, True)
            return "blocked"
        return "failed"
    return "sent"


def run_daily_summary() -> dict:
    conn = get_conn()
    sent_summary = 0
    sent_recent = 0
    sent_stale = 0
    skipped_blocked = 0
    errors: list[dict] = []
    try:
        init_db(conn)

        # Branch (1): users with meals today — full AI summary path.
        for user_id, date in get_users_needing_summary(conn):
            try:
                profile = get_profile(conn, user_id)
                if not profile_is_complete(profile):
                    continue
                log = get_today_log(conn, user_id)
                meals = get_meals_for_day(conn, user_id, date)
                text = generate_daily_summary(
                    meals, log, profile,
                    language=language_for_locale(i18n_mod.locale_of(profile)),
                )
                outcome = _send_with_autoptout(conn, user_id, text)
                if outcome == "blocked":
                    skipped_blocked += 1
                    continue
                save_recommendation(conn, user_id, date, text)
                mark_summary_sent(conn, user_id, date)
                sent_summary += 1
                time.sleep(_SEND_DELAY_S)
            except Exception as e:
                errors.append({"user_id": user_id, "branch": "summary", "error": str(e)})
                error("summary_user_failed", exc=e, user_id=user_id)

        # Branch (2): zero today — tier-aware nudge (recent daily, stale every 3d).
        for u in get_users_to_nudge(conn):
            uid = u["user_id"]
            tier = u["tier"]
            try:
                profile = get_profile(conn, uid)
                if not profile:
                    continue
                lang = i18n_mod.locale_of(profile)
                key = "nudge.zero_today" if tier == "recent" else "nudge.come_back"
                text = i18n_mod.t(key, locale=lang)
                outcome = _send_with_autoptout(
                    conn, uid, text, reply_markup=nudge_optout_keyboard(locale=lang),
                )
                if outcome == "blocked":
                    skipped_blocked += 1
                    continue
                if outcome == "sent":
                    if tier == "stale":
                        mark_nudge_sent(conn, uid)
                        sent_stale += 1
                    else:
                        sent_recent += 1
                time.sleep(_SEND_DELAY_S)
            except Exception as e:
                errors.append({"user_id": uid, "branch": f"nudge_{tier}", "error": str(e)})
                error("nudge_user_failed", exc=e, user_id=uid)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "ok": True,
        "sent_summary": sent_summary,
        "sent_recent": sent_recent,
        "sent_stale": sent_stale,
        "skipped_blocked": skipped_blocked,
        "errors": errors,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
