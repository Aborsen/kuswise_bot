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
    get_profile,
    get_streak,
    get_weight_history,
    get_meals_in_range,
)
from lib.telegram_helpers import send_message, send_photo
from lib.log import setup_sentry, http_handler, error
from lib import recap as recap_mod
from lib.i18n import t as _i18n_t, locale_of as _i18n_locale_of

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
    recaps_sent = 0
    errors = []
    try:
        init_db(conn)
        user_ids = get_users_due_weekly_checkin(conn, min_days_since_last=6)
        for user_id in user_ids:
            try:
                set_awaiting_input(conn, user_id, "weight")
                # Per-user locale — fetch profile to pick the right language.
                _profile_for_lang = get_profile(conn, user_id) or {}
                resp = send_message(
                    user_id,
                    _i18n_t("weight.checkin_prompt", locale=_i18n_locale_of(_profile_for_lang)),
                )
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

                # F-12: piggyback the weekly recap PNG. Best-effort —
                # silently skip on failure (the weight prompt already went out).
                try:
                    if _send_weekly_recap_for(conn, user_id):
                        recaps_sent += 1
                except Exception as recap_exc:
                    error("weekly_recap_failed", exc=recap_exc, user_id=user_id)
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
        "recaps_sent": recaps_sent,
        "errors": errors,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def _send_weekly_recap_for(conn, user_id: int) -> bool:
    """Build + send a recap PNG to ``user_id``. Returns True on success.

    Skips users with no logged meals in the past 7 days (no point sending
    an empty card). Re-uses the same helpers as the on-demand /recap path
    so the rendering stays identical.
    """
    from datetime import date as _date, timedelta as _td

    profile = get_profile(conn, user_id) or {}
    end_date_obj = _date.today()
    start_date_obj = end_date_obj - _td(days=6)

    meals_7d = get_meals_in_range(
        conn, user_id,
        start_date_obj.isoformat(),
        end_date_obj.isoformat(),
    )
    # Skip users with zero engagement this week — no card.
    if not meals_7d:
        return False

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
    locale = _i18n_locale_of(profile)
    png = recap_mod.render_recap_png(stats, first_name=None, locale=locale)
    caption = _i18n_t(
        "recap.weekly_caption",
        locale=locale,
        streak=stats["streak"],
        avg=stats["avg_kcal"],
        days=stats["days_logged"],
    )
    resp = send_photo(user_id, png, caption=caption)
    return bool(resp.get("ok"))
