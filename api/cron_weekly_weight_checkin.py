"""Vercel Cron — every Monday 09:00 Kyiv (06:00 UTC) ask each user for weight.

Logic:
  * For every onboarded user whose last weekly check-in is ≥ 6 days ago,
    flag `awaiting_input_type = 'weight'`, send a prompt, and stamp
    `weekly_checkin_sent_at = now()`.
  * The user's text reply is handled by `handle_weight_input` in webhook.py:
    that function writes `weight_history`, updates `user_profiles.weight_kg`,
    recomputes `daily_calorie_target` (new weight × goal formula) and the
    water target.
"""
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
    get_users_due_weekly_checkin,
    set_awaiting_input,
    mark_weekly_checkin_sent,
)
from lib.formatters import WEIGHT_CHECKIN_PROMPT
from lib.telegram_helpers import send_message
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_weekly_weight_checkin")


def _authorized(headers) -> bool:
    """Constant-time comparison; fails closed when CRON_SECRET is unset."""
    if not CRON_SECRET:
        return False
    auth = headers.get("Authorization", "")
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))


class handler(BaseHTTPRequestHandler):
    @http_handler("cron_weekly_weight_checkin")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        try:
            result = run_weekly_checkin()
        except Exception as exc:
            error("cron_weekly_weight_checkin_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def run_weekly_checkin() -> dict:
    conn = get_conn()
    sent = 0
    errors = []
    try:
        init_db(conn)
        user_ids = get_users_due_weekly_checkin(conn, min_days_since_last=6)
        for user_id in user_ids:
            try:
                set_awaiting_input(conn, user_id, "weight")
                resp = send_message(user_id, WEIGHT_CHECKIN_PROMPT)
                if resp.get("ok"):
                    mark_weekly_checkin_sent(conn, user_id)
                    sent += 1
                else:
                    # Telegram rejected (blocked bot, etc.) — roll back the flag
                    # so we don't block real interactions. Do mark as sent so
                    # we don't retry every hour.
                    set_awaiting_input(conn, user_id, None)
                    mark_weekly_checkin_sent(conn, user_id)
                    errors.append({"user_id": user_id, "error": resp})
            except Exception as e:
                errors.append({"user_id": user_id, "error": str(e)})
                error("weekly_checkin_user_failed", exc=e, user_id=user_id)
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
