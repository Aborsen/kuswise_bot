"""Vercel Cron endpoint — runs daily at 22:00 UTC to send end-of-day summaries."""
import hmac
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import CRON_SECRET
from lib.database import (
    get_conn,
    init_db,
    get_users_needing_summary,
    get_today_log,
    get_meals_for_day,
    save_recommendation,
    mark_summary_sent,
    get_profile,
    profile_is_complete,
)
from lib.telegram_helpers import send_message
from lib.config import language_for_locale
from lib.openai_nutrition import generate_daily_summary
from lib import i18n as i18n_mod
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_daily_summary")


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

        result = {"ok": True, "sent": 0, "errors": []}
        try:
            result = run_daily_summary()
        except Exception as exc:
            error("cron_daily_summary_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def run_daily_summary() -> dict:
    conn = get_conn()
    sent = 0
    errors = []
    try:
        init_db(conn)
        targets = get_users_needing_summary(conn)
        for user_id, date in targets:
            try:
                profile = get_profile(conn, user_id)
                if not profile_is_complete(profile):
                    continue
                log = get_today_log(conn, user_id)
                meals = get_meals_for_day(conn, user_id, date)
                text = generate_daily_summary(meals, log, profile, language=language_for_locale(i18n_mod.locale_of(profile)))
                send_message(user_id, text)
                save_recommendation(conn, user_id, date, text)
                mark_summary_sent(conn, user_id, date)
                sent += 1
            except Exception as e:
                errors.append({"user_id": user_id, "error": str(e)})
                error("summary_user_failed", exc=e, user_id=user_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "ok": True,
        "sent": sent,
        "errors": errors,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
