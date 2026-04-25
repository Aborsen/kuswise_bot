"""Vercel Cron endpoint — runs daily at 00:00 UTC for housekeeping."""
import hmac
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import CRON_SECRET
from lib.database import (
    cleanup_old_quotas,
    cleanup_old_menu_ocr_results,
    cleanup_old_meal_plans,
    get_conn,
    init_db,
    mark_all_previous_summaries_sent,
    reset_monthly_freezes,
)
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_midnight_reset")


def _authorized(headers) -> bool:
    """Verify Vercel Cron bearer token. Fails closed if CRON_SECRET is not set.
    Uses constant-time comparison to resist timing attacks."""
    if not CRON_SECRET:
        return False  # fail closed — refuse to serve when not configured
    auth = headers.get("Authorization", "")
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))


class handler(BaseHTTPRequestHandler):
    @http_handler("cron_midnight_reset")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        result = {"ok": True}
        try:
            result = run_midnight_reset()
        except Exception as exc:
            error("cron_midnight_reset_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def run_midnight_reset() -> dict:
    conn = get_conn()
    try:
        init_db(conn)
        # Failsafe: mark any unsent prior-day summaries so they don't queue up
        mark_all_previous_summaries_sent(conn)
        # Clear any stale pending photos (>1 hour)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_photos WHERE created_at < %s", (cutoff,))
        conn.commit()
        # Prune usage_quota rows older than 7 days so the table stays small.
        cleanup_old_quotas(conn, keep_days=7)
        # F-9: drop menu OCR results > 1h old so abandoned menus don't pile up.
        cleanup_old_menu_ocr_results(conn, max_age_hours=1)
        # F-10: prune meal plans older than 90d.
        cleanup_old_meal_plans(conn, max_age_days=90)
        # F-4: refill streak freezes on the 1st of each UTC month.
        # Trade-off: not per-user-tz (same as the existing cron design); a
        # Kyiv user at 02:00 local on Feb 1 gets the refill ~24h later.
        if datetime.now(timezone.utc).day == 1:
            reset_monthly_freezes(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"ok": True, "ran_at": datetime.now(timezone.utc).isoformat()}
