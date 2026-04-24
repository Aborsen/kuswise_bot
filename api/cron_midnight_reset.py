"""Vercel Cron endpoint — runs daily at 00:00 UTC for housekeeping."""
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
from lib.database import get_conn, init_db, mark_all_previous_summaries_sent


def _authorized(headers) -> bool:
    """Verify Vercel Cron bearer token. Fails closed if CRON_SECRET is not set."""
    if not CRON_SECRET:
        return False  # fail closed — refuse to serve when not configured
    auth = headers.get("Authorization", "")
    return auth == f"Bearer {CRON_SECRET}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        result = {"ok": True}
        try:
            result = run_midnight_reset()
        except Exception:
            print("cron_midnight_reset error:", traceback.format_exc(), flush=True)
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
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"ok": True, "ran_at": datetime.now(timezone.utc).isoformat()}
