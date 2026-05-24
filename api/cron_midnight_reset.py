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
    finalize_stuck_tz_users,
    get_conn,
    get_users_to_auto_quiet,
    init_db,
    mark_all_previous_summaries_sent,
    record_cron_run,
    reset_monthly_freezes,
    set_activation_step,
    set_nudge_optout,
)
from lib.telegram_helpers import send_message
from lib import i18n as i18n_mod
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
    status = "ok"
    err: str | None = None
    result: dict = {"ok": True}
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
        # F-16: safety net for users stranded mid-tz-step. Anyone stuck on
        # `awaiting_tz` or `awaiting_tz_custom` for >12h gets force-finalized
        # with the schema default (`Europe/Kyiv`). They can change via
        # `/timezone` later. Each freed user is notified.
        freed = finalize_stuck_tz_users(conn, max_age_hours=12)
        tz_unstuck_notified = 0
        for u in freed:
            try:
                send_message(
                    u["user_id"],
                    i18n_mod.t("onboarding.tz_default_applied", locale=u["lang"]),
                )
                tz_unstuck_notified += 1
            except Exception as exc:
                error("tz_unstuck_notify_failed", exc=exc, user_id=u["user_id"])
        # F-17 activation funnel safety net: any user who completed
        # onboarding ≥9 days ago and never logged a meal gets silenced
        # to avoid the Lyubov-style "10 daily nudges → mute" pattern.
        # Cohort SQL strictly gates `NOT EXISTS (... FROM meals)` so an
        # active logger is never touched.
        to_quiet = get_users_to_auto_quiet(conn, days=9)
        auto_quieted_notified = 0
        for u in to_quiet:
            try:
                set_nudge_optout(conn, u["user_id"], True)
                set_activation_step(conn, u["user_id"], "auto_quieted")
                send_message(
                    u["user_id"],
                    i18n_mod.t("morning.auto_quieted_notice", locale=u["lang"]),
                )
                auto_quieted_notified += 1
            except Exception as exc:
                error("auto_quiet_notify_failed", exc=exc, user_id=u["user_id"])
        result = {
            "ok": True,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "tz_unstuck": len(freed),
            "tz_unstuck_notified": tz_unstuck_notified,
            "auto_quieted": len(to_quiet),
            "auto_quieted_notified": auto_quieted_notified,
        }
    except Exception as exc:
        status = "error"
        err = repr(exc)
        raise
    finally:
        try:
            record_cron_run(conn, "cron_midnight_reset", status,
                            result if status == "ok" else None, err)
        except Exception:
            pass  # never let cron-status logging mask the real run outcome
        try:
            conn.close()
        except Exception:
            pass
    return result
