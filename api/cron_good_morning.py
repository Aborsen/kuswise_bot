"""Vercel Cron endpoint — runs hourly at :30 UTC.

For each invocation, processes users whose local clock is in the 08:00
hour right now. With users spread across timezones, every UTC hour
catches its own cohort; users in `Europe/Kyiv` (the default tz) match
the 05:30 UTC fire (summer) / 06:30 UTC fire (winter), arriving at
~08:30 local.

Per cohort user:
  * onboarding_step == 'done'  → upbeat `morning.greeting_done`
  * everyone else (mid-flow)   → encouraging `morning.greeting_mid_onboarding`
                                 ("come back and finish onboarding")

Gates:
  * `nudge_optout = 0`            (user hasn't muted)
  * `blocked_at IS NULL`          (Telegram hasn't refused us)
  * per-user-local-day dedup via `last_morning_sent_at`

Same auto-block behavior as `cron_daily_summary`: 400/403 from Telegram
stamps `blocked_at` so the user vanishes from cohorts until they message
the bot again.
"""
# Defer annotations to strings at module load — bulletproof against the
# `callable | None` class of typo (lowercase builtin used as a type) that
# crashed this module on import during F-17. With deferred annotations,
# even semantically-wrong text in annotations doesn't crash the runtime;
# it'd only surface at static-analysis or `typing.get_type_hints()` time.
from __future__ import annotations
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
    get_users_due_morning_greeting,
    get_users_for_first_meal_demo,
    get_users_for_d4_followup,
    get_users_for_d7_final,
    mark_morning_sent,
    record_cron_run,
    set_activation_step,
    set_blocked,
    get_profile,
)
from lib.telegram_helpers import send_message
from lib import i18n as i18n_mod
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_good_morning")

# Telegram global rate cap is 30 msg/sec; 40ms keeps us comfortably under.
_SEND_DELAY_S = 0.04


def _authorized(headers) -> bool:
    """Verify Vercel Cron bearer token. Fails closed if CRON_SECRET unset.
    Constant-time comparison to resist timing attacks."""
    if not CRON_SECRET:
        return False
    auth = headers.get("Authorization", "")
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))


class handler(BaseHTTPRequestHandler):
    @http_handler("cron_good_morning")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        result = {"ok": True, "sent": 0, "skipped_blocked": 0, "errors": []}
        try:
            result = run_good_morning()
        except Exception as exc:
            error("cron_good_morning_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def _send_with_autoblock(conn, user_id: int, text: str) -> str:
    """Send + auto-stamp blocked_at on Telegram 400/403. Same semantics
    as `_send_with_autoptout` in cron_daily_summary; kept local to this
    file so the helper stays close to its only call site here."""
    resp = send_message(user_id, text)
    if isinstance(resp, dict) and resp.get("ok") is False:
        if resp.get("error_code") in (400, 403):
            set_blocked(conn, user_id, True)
            return "blocked"
        return "failed"
    return "sent"


def _process_activation_cohort(
    conn,
    cohort: list[dict],
    i18n_key: str,
    new_step: str,
    counter_key: str,
    counters: dict,
    extra_format_kwargs=None,
) -> None:
    """Send the activation-funnel message to one cohort, stamp the new
    `activation_step` + `last_morning_sent_at` on each successful send.

    `cohort`: result of one of `get_users_for_first_meal_demo` /
              `get_users_for_d4_followup` / `get_users_for_d7_final`.
    `i18n_key`: the `morning.first_meal_*` key to render per user.
    `new_step`: state to transition the user into on success.
    `counter_key`: which counter in `counters` to bump on success — one of
              `activation_sent_demo` / `activation_sent_d4` /
              `activation_sent_d7`. Per-variant counters keep each stage
              individually visible in `cron_runs.result_json` so the
              health monitor can confirm every funnel rung is firing.
    `extra_format_kwargs`: callable taking a cohort dict, returning the
              extra kwargs for `i18n_mod.t()` (used by the demo card to
              inject `name` and `cal`). None → no extra kwargs.
    `counters`: mutable dict; bumps `<counter_key>` / `skipped_blocked` /
              `errors` in place so the surrounding `run_good_morning`
              report aggregates correctly.
    """
    for u in cohort:
        uid = u["user_id"]
        try:
            kwargs = extra_format_kwargs(u) if extra_format_kwargs else {}
            text = i18n_mod.t(i18n_key, locale=u["lang"], **kwargs)
            outcome = _send_with_autoblock(conn, uid, text)
            if outcome == "blocked":
                counters["skipped_blocked"] += 1
                continue
            if outcome == "sent":
                set_activation_step(conn, uid, new_step)
                mark_morning_sent(conn, uid)
                counters[counter_key] += 1
            time.sleep(_SEND_DELAY_S)
        except Exception as e:
            counters["errors"].append({"user_id": uid, "stage": new_step, "error": str(e)})
            error("morning_activation_failed", exc=e, user_id=uid, stage=new_step)


def run_good_morning() -> dict:
    conn = get_conn()
    sent = 0
    skipped_blocked = 0
    errors: list[dict] = []
    status = "ok"
    err_repr: str | None = None
    result: dict = {"ok": True}
    try:
        init_db(conn)

        # F-17 activation funnel — handle never-loggers BEFORE the
        # standard greeting iteration. Each successful send stamps
        # `last_morning_sent_at` which excludes the user from the
        # standard `get_users_due_morning_greeting` query below.
        # The cohort SQL filters on `NOT EXISTS (... FROM meals)` so
        # any user with ≥1 lifetime meal is never touched here.
        #
        # Per-variant counters (demo / d4 / d7) live in this dict so each
        # funnel rung is individually visible in `cron_runs.result_json`
        # — the health monitor cross-references each rung against the
        # corresponding cohort gate to confirm the funnel is processing.
        counters = {
            "activation_sent_demo": 0,
            "activation_sent_d4":   0,
            "activation_sent_d7":   0,
            "skipped_blocked":      skipped_blocked,
            "errors":               errors,
        }

        # Day 2+: first-meal demo card with personalised closing line.
        _process_activation_cohort(
            conn, get_users_for_first_meal_demo(conn),
            "morning.first_meal_demo_full", "demo",
            "activation_sent_demo", counters,
            extra_format_kwargs=lambda u: {
                "name": u["first_name"] or "—",
                "cal":  u["cal"],
            },
        )
        # Day 4+: lighter follow-up.
        _process_activation_cohort(
            conn, get_users_for_d4_followup(conn),
            "morning.first_meal_followup_d4", "d4_followup",
            "activation_sent_d4", counters,
        )
        # Day 7+: final softer message.
        _process_activation_cohort(
            conn, get_users_for_d7_final(conn),
            "morning.first_meal_followup_d7", "d7_final",
            "activation_sent_d7", counters,
        )

        # Re-snap counters back to local scope for the result dict.
        skipped_blocked = counters["skipped_blocked"]

        # Standard morning greeting — engaged users + mid-onboarding +
        # never-loggers in their first 24 hours (the activation cohorts
        # all gate on ≥24h since signup). Users who got an activation
        # message above are excluded by `last_morning_sent_at` dedup.
        for u in get_users_due_morning_greeting(conn):
            uid = u["user_id"]
            try:
                lang = u["lang"]
                step = u["onboarding_step"]
                key = ("morning.greeting_done" if step == "done"
                       else "morning.greeting_mid_onboarding")
                text = i18n_mod.t(key, locale=lang)
                outcome = _send_with_autoblock(conn, uid, text)
                if outcome == "blocked":
                    skipped_blocked += 1
                    continue
                if outcome == "sent":
                    mark_morning_sent(conn, uid)
                    sent += 1
                time.sleep(_SEND_DELAY_S)
            except Exception as e:
                errors.append({"user_id": uid, "error": str(e)})
                error("morning_user_failed", exc=e, user_id=uid)

        result = {
            "ok": True,
            "sent": sent,
            "activation_sent_demo": counters["activation_sent_demo"],
            "activation_sent_d4":   counters["activation_sent_d4"],
            "activation_sent_d7":   counters["activation_sent_d7"],
            "skipped_blocked": skipped_blocked,
            "errors": errors,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        status = "error"
        err_repr = repr(exc)
        raise
    finally:
        try:
            record_cron_run(conn, "cron_good_morning", status,
                            result if status == "ok" else None, err_repr)
        except Exception:
            pass  # never let cron-status logging mask the real run outcome
        try:
            conn.close()
        except Exception:
            pass

    return result
