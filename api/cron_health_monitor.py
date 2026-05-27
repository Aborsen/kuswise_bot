"""Vercel Cron endpoint — runs daily at 09:00 UTC (≈12:00 Kyiv).

Posts a single short health report to the KusWise Users channel
(`ADMIN_NOTIFY_CHAT_ID`). The report is the same skeleton every day so
your eye trains on the bottom line — healthy days end with
`ALL CHECKS PASSED ✓`, anomaly days end with the alert list.

Motivation: the F-17 hotfix surfaced that an entire cron can be silently
dead for 54 hours without anyone noticing. Daily cadence makes the
monitor itself observable too — if a day passes with no health report,
the monitor itself is down.

Checks (each returns `ok` / `alert` + one-line message):

  1. Cron firing health        — count `cron_runs` in last 24h per cron
  2. Cron-level errors          — any `status='error'` or non-empty `error`
  3. Per-user errors            — `result_json.errors` non-empty in any 24h run
  4. Auto-quiet sweep sanity    — `get_users_to_auto_quiet` should be empty
                                  right after the midnight sweep
  5. Activation funnel progress — anyone stuck in demo/d4/d7 beyond their stage
  6. Daily-summary cohort       — meals logged yesterday vs. `sent_summary`
  7. Block spike                — today's `blocked_at` stamps vs. 7-day avg

The 8th "check" is informational: the activity line (signups, meals, active
users, first-meal conversions) so even a clean day reports useful data.

Same auth + record_cron_run pattern as the other cron endpoints.
"""
from __future__ import annotations
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import CRON_SECRET, ADMIN_NOTIFY_CHAT_ID
from lib.database import (
    avg_daily_blocks,
    count_cron_runs_24h,
    count_cron_runs_24h_by_status,
    count_first_meal_logs_today,
    count_meals_and_active_users_24h,
    count_new_blocks,
    count_signups_24h,
    count_users_logged_yesterday_utc,
    get_conn,
    get_cron_errors_24h,
    get_users_stuck_in_activation_step,
    get_users_to_auto_quiet,
    get_user_errors_in_cron_runs_24h,
    init_db,
    record_cron_run,
    sum_counters_24h,
)
from lib.telegram_helpers import send_message
from lib.log import setup_sentry, http_handler, error

setup_sentry("cron_health_monitor")


# Tolerances. Hourly crons get 20+ as the "ok" floor to absorb Vercel
# delivery jitter (which has produced 22/24 or 23/24 fires on otherwise
# healthy days). Tune in code if false-positive rate is too high.
_HOURLY_OK_FLOOR = 20
_HOURLY_EXPECTED = 24
_BLOCK_SPIKE_MULTIPLIER = 2.0
# Funnel stuck thresholds — how long a user can sit in each step before
# we flag the morning cron as not progressing them.
_FUNNEL_STUCK_DAYS = {
    "demo":         3,  # should advance to d4_followup within ~2 days
    "d4_followup":  4,  # should advance to d7_final within ~3 days
    "d7_final":     2,  # should be auto-quieted within ~2 days
}


def _authorized(headers) -> bool:
    """Constant-time bearer-token check; fails closed when `CRON_SECRET`
    is unset (same pattern as the other cron endpoints)."""
    if not CRON_SECRET:
        return False
    auth = headers.get("Authorization", "")
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))


class handler(BaseHTTPRequestHandler):
    @http_handler("cron_health_monitor")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401)
            self.end_headers()
            return

        result: dict = {"ok": True}
        try:
            result = run_health_monitor()
        except Exception as exc:
            error("cron_health_monitor_failed", exc=exc)
            result = {"ok": False, "error": "internal"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, default=str).encode())


# ---------- Individual checks ----------
# Each check returns a dict `{ok: bool, line: str, alerts: list[str]}`.
# `line` is the always-rendered status line for the report; `alerts` is
# extra bullets that only appear in the bottom "🚨 ALERTS" section when
# `ok` is False. Kept tiny + composable so each check stays testable
# in isolation.


def _check_cron_firing(conn) -> dict:
    """Hourly crons should fire ≥20× / day; daily crons ≥1×.

    Uses the per-status breakdown (`count_cron_runs_24h_by_status`) so
    the report can distinguish:
      * Vercel-not-invoking (low `started` count)
      * Function-crashed-mid-flight (`started - finished_ok > 2` —
        rows that inserted but never updated)

    The two alert conditions render distinct messages so the followup
    investigation goes in the right direction.

    Pre-Phase-B legacy rows from `record_cron_run` have `finished_at`
    set + `status='ok'`, so they fall into `finished_ok` cleanly —
    crons we haven't migrated yet still report sensible numbers.

    Weekly check-in is `⏸️ not scheduled today` on non-Mondays —
    monitor doesn't alert outside its scheduled window.
    """
    today_dow = datetime.now(timezone.utc).weekday()  # Mon=0
    specs = [
        ("daily_summary",    "cron_daily_summary",         "hourly", None),
        ("good_morning",     "cron_good_morning",          "hourly", None),
        ("midnight_reset",   "cron_midnight_reset",        "daily",  None),
        ("weekly_checkin",   "cron_weekly_weight_checkin", "weekly", 0),  # Mon
    ]
    lines: list[str] = []
    alerts: list[str] = []
    overall_ok = True
    for label, cron_name, kind, scheduled_dow in specs:
        b = count_cron_runs_24h_by_status(conn, cron_name)
        started = b["started"]
        ok_count = b["finished_ok"]
        errored = b["errored"]
        unfinished = b["running_unfinished"]

        # Build the breakdown suffix shown on every line — same shape for
        # every cron so the eye trains on the position.
        if started > 0:
            suffix = f"{started} starts → {ok_count} ok"
            if errored:
                suffix += f", {errored} errored"
            if unfinished:
                suffix += f", {unfinished} lost"
        else:
            suffix = f"0 starts"

        # Two independent alert conditions:
        #   A) too few starts → Vercel didn't invoke us
        #   B) starts but didn't finish → function crashed mid-flight
        low_starts = False
        crashed = unfinished > 2  # tolerance for one or two genuinely-still-running

        if kind == "hourly":
            low_starts = started < _HOURLY_OK_FLOOR
            ok = not (low_starts or crashed)
            lines.append(f"{'✅' if ok else '🚨'} {label:<16} {suffix}")
            if low_starts:
                alerts.append(
                    f"{cron_name}: only {started}/{_HOURLY_EXPECTED} starts "
                    f"in 24h (Vercel cron not invoking — check delivery)"
                )
                overall_ok = False
            if crashed:
                alerts.append(
                    f"{cron_name}: {unfinished} fires started but never "
                    f"finished (function crashing mid-flight — check Vercel logs)"
                )
                overall_ok = False
        elif kind == "daily":
            low_starts = started < 1
            ok = not (low_starts or crashed)
            lines.append(f"{'✅' if ok else '🚨'} {label:<16} {suffix}")
            if low_starts:
                alerts.append(f"{cron_name}: 0 starts in last 24h")
                overall_ok = False
            if crashed:
                alerts.append(
                    f"{cron_name}: {unfinished} fires started but never finished"
                )
                overall_ok = False
        elif kind == "weekly":
            if today_dow == scheduled_dow:
                low_starts = started < 1
                ok = not (low_starts or crashed)
                lines.append(f"{'✅' if ok else '🚨'} {label:<16} {suffix}")
                if low_starts:
                    alerts.append(f"{cron_name}: 0 starts on Monday")
                    overall_ok = False
                if crashed:
                    alerts.append(
                        f"{cron_name}: {unfinished} fires started but never finished"
                    )
                    overall_ok = False
            else:
                lines.append(f"⏸️ {label:<16} not scheduled today")
    return {"ok": overall_ok, "lines": lines, "alerts": alerts}


def _check_cron_errors(conn) -> dict:
    """Any `cron_runs` row whose own status flipped to error in the last 24h."""
    errs = get_cron_errors_24h(conn)
    if not errs:
        return {"ok": True, "lines": [], "alerts": []}
    alerts = []
    for e in errs[:5]:  # cap to 5 lines so we don't blow up the message
        msg = (e.get("error") or "").splitlines()[0][:140]
        alerts.append(f"{e['cron_name']} error: {msg}")
    if len(errs) > 5:
        alerts.append(f"…and {len(errs) - 5} more cron errors")
    return {"ok": False, "lines": [], "alerts": alerts}


def _check_user_errors(conn) -> dict:
    """Per-user errors that crons swallowed into `result_json.errors`."""
    rows = get_user_errors_in_cron_runs_24h(conn)
    if not rows:
        return {"ok": True, "lines": [], "alerts": []}
    total = sum(r["errors_count"] for r in rows)
    alerts: list[str] = []
    for r in rows[:3]:
        sample = r["sample"]
        sample_msg = ""
        if isinstance(sample, dict):
            sample_msg = str(sample.get("error", sample))[:120]
        alerts.append(
            f"{r['cron_name']}: {r['errors_count']} per-user errors "
            f"(sample: {sample_msg})"
        )
    if len(rows) > 3:
        alerts.append(f"…and {total - sum(r['errors_count'] for r in rows[:3])} more user errors")
    return {"ok": False, "lines": [], "alerts": alerts}


def _check_auto_quiet_sanity(conn) -> dict:
    """If `get_users_to_auto_quiet` has a backlog right now, the
    midnight sweep didn't fire correctly.

    Wording: the midnight cron runs at 00:00 UTC and the monitor at 09:00
    UTC, so by the time we look there should be 0 users matching the
    auto-quiet cohort. Anything > 0 → alert.
    """
    backlog = get_users_to_auto_quiet(conn, days=9)
    if not backlog:
        return {"ok": True, "lines": [], "alerts": []}
    return {
        "ok": False,
        "lines": [],
        "alerts": [
            f"auto-quiet sweep didn't fire: {len(backlog)} users "
            f"still match cohort (should be 0 post-midnight)"
        ],
    }


def _check_activation_funnel(conn) -> dict:
    """Anyone stuck in a funnel step longer than its expected duration."""
    stuck_total = 0
    alerts: list[str] = []
    for step, min_days in _FUNNEL_STUCK_DAYS.items():
        stuck = get_users_stuck_in_activation_step(conn, step, min_days)
        if stuck:
            stuck_total += len(stuck)
            alerts.append(
                f"{len(stuck)} users stuck in '{step}' >{min_days} days"
            )
    if not stuck_total:
        return {
            "ok": True,
            "lines": ["✅ 0 users stuck mid-funnel beyond their stage"],
            "alerts": [],
        }
    return {
        "ok": False,
        "lines": [f"🚨 {stuck_total} users stuck mid-funnel"],
        "alerts": alerts,
    }


def _check_summary_cohort(conn) -> dict:
    """Did everyone who logged yesterday get their AI summary?"""
    logged_y = count_users_logged_yesterday_utc(conn)
    sent = sum_counters_24h(conn, "cron_daily_summary", ["sent_summary"])
    sent_summary = sent["sent_summary"]
    # Tolerance: AI summary can fail for users on quiet hours or whose
    # profile isn't complete. Alert only on >40% gap to avoid noise.
    if logged_y == 0:
        return {"ok": True, "lines": [], "alerts": []}
    gap = max(0, logged_y - sent_summary)
    gap_pct = gap / logged_y if logged_y else 0
    if gap_pct <= 0.4:
        return {"ok": True, "lines": [], "alerts": []}
    return {
        "ok": False,
        "lines": [],
        "alerts": [
            f"daily summary gap: {logged_y} users logged yesterday "
            f"but only {sent_summary} got the AI summary"
        ],
    }


def _check_block_spike(conn) -> dict:
    """Today's new `blocked_at` count vs. 7-day average."""
    today = count_new_blocks(conn, hours=24)
    baseline = avg_daily_blocks(conn, days=7)
    # Require both a meaningful absolute count AND a multiplier breach so
    # we don't shout about 1-vs-0.
    if today >= 3 and (baseline == 0 or today > baseline * _BLOCK_SPIKE_MULTIPLIER):
        return {
            "ok": False,
            "lines": [],
            "alerts": [
                f"block spike: {today} new blocked_at vs 7-day avg "
                f"{baseline:.1f}"
            ],
        }
    return {"ok": True, "lines": [], "alerts": []}


# ---------- Report builder ----------


def _build_report(conn) -> tuple[str, dict]:
    """Run all checks, render the final message + a JSON summary for
    `cron_runs.result_json`.

    Returns `(message_text, json_summary)`.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # --- run every check ---
    cron_firing = _check_cron_firing(conn)
    cron_errors = _check_cron_errors(conn)
    user_errors = _check_user_errors(conn)
    auto_quiet  = _check_auto_quiet_sanity(conn)
    funnel      = _check_activation_funnel(conn)
    summary     = _check_summary_cohort(conn)
    block       = _check_block_spike(conn)

    # Counters from the morning + midnight crons — rolled across 24h.
    morning_counters = sum_counters_24h(
        conn, "cron_good_morning",
        ["sent", "activation_sent_demo", "activation_sent_d4",
         "activation_sent_d7", "skipped_blocked"],
    )
    midnight_counters = sum_counters_24h(
        conn, "cron_midnight_reset",
        ["auto_quieted_notified", "tz_unstuck_notified"],
    )
    summary_counters = sum_counters_24h(
        conn, "cron_daily_summary",
        ["sent_summary", "sent_nudge", "skipped_blocked"],
    )
    weekly_counters = sum_counters_24h(
        conn, "cron_weekly_weight_checkin",
        ["sent", "recaps_sent"],
    )

    signups = count_signups_24h(conn)
    activity = count_meals_and_active_users_24h(conn)
    first_meals = count_first_meal_logs_today(conn)

    # --- render ---
    parts: list[str] = []
    parts.append(f"🩺 Health report — {date_str} ({now.strftime('%H:%M')} UTC)")
    parts.append("")
    parts.append("CRONS (last 24h):")
    parts.extend(cron_firing["lines"])
    parts.append("")
    parts.append("NOTIFICATIONS SENT:")
    parts.append(
        f"☀️ Morning greetings: {morning_counters['sent']} "
        f"(demo {morning_counters['activation_sent_demo']}, "
        f"d4 {morning_counters['activation_sent_d4']}, "
        f"d7 {morning_counters['activation_sent_d7']})"
    )
    parts.append(
        f"🌙 Evening: {summary_counters['sent_summary']} summaries, "
        f"{summary_counters['sent_nudge']} zero-log nudges"
    )
    parts.append(
        f"🤫 Midnight: {midnight_counters['auto_quieted_notified']} auto-quieted, "
        f"{midnight_counters['tz_unstuck_notified']} tz-unstuck"
    )
    if weekly_counters["sent"] or weekly_counters["recaps_sent"]:
        parts.append(
            f"⚖️ Weekly: {weekly_counters['sent']} weight prompts, "
            f"{weekly_counters['recaps_sent']} recap PNGs"
        )
    parts.append("")
    parts.append("ACTIVATION:")
    parts.append(
        f"✅ Demo card sent to {morning_counters['activation_sent_demo']} users, "
        f"d4 follow-up to {morning_counters['activation_sent_d4']}, "
        f"d7 final to {morning_counters['activation_sent_d7']}"
    )
    if midnight_counters['auto_quieted_notified']:
        parts.append(
            f"✅ Auto-quiet sweep silenced "
            f"{midnight_counters['auto_quieted_notified']} users last night"
        )
    parts.extend(funnel["lines"])
    parts.append("")
    parts.append("ACTIVITY:")
    parts.append(
        f"👋 {signups['done'] + signups['mid']} new signups "
        f"({signups['done']} reached done, {signups['mid']} mid-onboarding)"
    )
    parts.append(
        f"🍽️ {activity['meals']} meals logged by {activity['active_users']} active users"
    )
    if first_meals:
        parts.append(f"⭐ {first_meals} users logged their FIRST EVER meal in last 24h")

    # --- footer: pass or alert list ---
    all_alerts: list[str] = []
    for c in (cron_firing, cron_errors, user_errors, auto_quiet,
              funnel, summary, block):
        all_alerts.extend(c["alerts"])

    parts.append("")
    if not all_alerts:
        parts.append("ALL CHECKS PASSED ✓")
    else:
        parts.append("🚨 ALERTS:")
        for a in all_alerts:
            parts.append(f"• {a}")

    text = "\n".join(parts)
    json_summary = {
        "ok": not all_alerts,
        "ran_at": now.isoformat(),
        "alerts_count": len(all_alerts),
        "alerts": all_alerts,
        "morning_counters": morning_counters,
        "midnight_counters": midnight_counters,
        "summary_counters": summary_counters,
        "weekly_counters": weekly_counters,
        "signups": signups,
        "activity": activity,
        "first_meals": first_meals,
    }
    return text, json_summary


def run_health_monitor() -> dict:
    """Build the daily health report, post it to the admin channel,
    return a JSON summary for ``cron_runs.result_json``.

    Always posts — healthy days included — so the daily fire itself is
    your liveness signal. If a day passes with no message in the channel,
    the monitor itself is down.
    """
    conn = get_conn()
    status = "ok"
    err_repr: str | None = None
    result: dict = {"ok": True}
    try:
        init_db(conn)
        text, json_summary = _build_report(conn)

        # Best-effort post — the report contents go into result_json
        # regardless, so the admin panel can still surface them even if
        # Telegram is down.
        posted = False
        send_error: str | None = None
        if ADMIN_NOTIFY_CHAT_ID:
            try:
                resp = send_message(int(ADMIN_NOTIFY_CHAT_ID), text)
                posted = bool(resp.get("ok"))
                if not posted:
                    send_error = str(resp)[:200]
            except Exception as exc:
                send_error = repr(exc)[:200]
                error("health_monitor_post_failed", exc=exc)
        else:
            send_error = "ADMIN_NOTIFY_CHAT_ID unset"

        result = {
            **json_summary,
            "posted": posted,
            "send_error": send_error,
        }
    except Exception as exc:
        status = "error"
        err_repr = repr(exc)
        raise
    finally:
        try:
            record_cron_run(conn, "cron_health_monitor", status,
                            result if status == "ok" else None, err_repr)
        except Exception:
            pass  # never let cron-status logging mask the real run outcome
        try:
            conn.close()
        except Exception:
            pass

    return result
