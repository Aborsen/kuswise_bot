"""Vercel Cron endpoint — runs daily at 17:00 UTC to nudge users inactive 24+ h."""
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

from lib.config import CRON_SECRET
from lib.database import (
    get_conn,
    init_db,
    get_inactive_users,
    mark_nudge_sent,
    set_nudge_optout,
    get_profile,
)
from lib.telegram_helpers import send_message, nudge_optout_keyboard
from lib import i18n as i18n_mod
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_inactivity_nudge")


def _authorized(headers) -> bool:
    if not CRON_SECRET:
        return False
    auth = headers.get("Authorization", "")
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))


class handler(BaseHTTPRequestHandler):
    @http_handler("cron_inactivity_nudge")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        result = {"ok": True, "sent": 0, "skipped": 0, "errors": []}
        try:
            result = run_inactivity_nudge()
        except Exception as exc:
            error("cron_inactivity_nudge_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def run_inactivity_nudge() -> dict:
    conn = get_conn()
    sent = 0
    skipped = 0
    errors: list[dict] = []
    try:
        init_db(conn)
        targets = get_inactive_users(conn, hours=24, cooldown_days=7)
        for u in targets:
            try:
                profile = get_profile(conn, u["user_id"])
                if not profile:
                    skipped += 1
                    continue
                lang = i18n_mod.locale_of(profile)
                text = i18n_mod.t("nudge.inactive_24h", locale=lang)
                resp = send_message(
                    u["user_id"],
                    text,
                    reply_markup=nudge_optout_keyboard(locale=lang),
                )
                # Telegram returns ok:false with error_code 403 when the user
                # blocked the bot, or 400 "chat not found" if the chat is gone.
                # Auto-opt-out so we never retry these users.
                if isinstance(resp, dict) and resp.get("ok") is False:
                    err_code = resp.get("error_code")
                    if err_code in (400, 403):
                        set_nudge_optout(conn, u["user_id"], True)
                        skipped += 1
                        continue
                mark_nudge_sent(conn, u["user_id"])
                sent += 1
                # Not load-bearing at current scale; revisit if user count > ~5000.
                time.sleep(0.04)
            except Exception as e:
                errors.append({"user_id": u["user_id"], "error": str(e)})
                error("nudge_user_failed", exc=e, user_id=u["user_id"])
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "ok": True,
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
