#!/usr/bin/env python3
"""One-shot: finalise users stuck at the finish-line steps that the
2026-05 onboarding simplification removed.

Why this exists
---------------
Today's onboarding cleanup dropped the calorie-review screen
(`awaiting_confirm`) and its custom-calorie follow-up
(`awaiting_custom_cal`). Users who were sitting on those steps when the
new code shipped are now in a dead state — fresh `/start`s skip the
removed steps, but cached keyboards from these users go nowhere (well,
they DO go somewhere — the legacy handlers still advance them to
`awaiting_tz`, which we ALSO removed, but they won't tap their cached
keyboard unprompted).

For both cohorts, `recommended_calorie_target` is already populated in
`user_profiles` (set when they reached the recommendation card). They
just need:

  * `daily_calorie_target` set to the recommended target
  * `tz` defaulted to `Europe/Kyiv` (the post-2026-05 default)
  * `onboarding_step` advanced to `done`
  * Water target computed from `weight_kg`
  * The standard "🎉 All set!" welcome message sent

Idempotent — the SELECT excludes `done`, so re-running finds 0 rows.

Usage::

    .venv/bin/python scripts/backfill_finish_onboarding.py
    .venv/bin/python scripts/backfill_finish_onboarding.py --dry-run

Reads ``TELEGRAM_BOT_TOKEN`` and ``DATABASE_URL`` from env / ``.env``.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Iterable

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Same .env loading pattern as the other scripts.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

from lib.config import calorie_target_from_profile
from lib.database import (
    get_conn,
    set_blocked,
    update_profile,
    upsert_water_target_from_profile,
)
from lib import i18n as i18n_mod
from lib.telegram_helpers import main_menu_keyboard, send_message


# Targets the two finish-line cohorts removed in the 2026-05 cleanup.
_STUCK_STEPS = ("awaiting_confirm", "awaiting_custom_cal")

# Inter-send delay — Telegram global cap is 30 msg/s; we're sending ~7
# messages so this is comfortable. Matches the morning cron's pacing.
_SEND_DELAY_S = 0.04


def _select_stuck_users(conn) -> list[dict]:
    """Pull every user at one of the stuck finish-line steps along with
    the fields needed to finalise them: lang, weight, goal,
    recommended_calorie_target, first_name (from `users` for the
    welcome-message salutation)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              up.user_id,
              COALESCE(up.lang, 'en')              AS lang,
              up.onboarding_step                    AS step,
              up.weight_kg,
              up.goal,
              up.recommended_calorie_target,
              COALESCE(u.first_name, '')            AS first_name
            FROM user_profiles up
            LEFT JOIN users u ON u.user_id = up.user_id
            WHERE up.onboarding_step = ANY(%s)
            ORDER BY up.user_id
            """,
            (list(_STUCK_STEPS),),
        )
        rows = cur.fetchall()
    return [
        {
            "user_id":    r[0],
            "lang":       r[1],
            "step":       r[2],
            "weight_kg":  r[3],
            "goal":       r[4],
            "rec_cal":    r[5],
            "first_name": r[6],
        }
        for r in rows
    ]


def _resolve_target_cal(row: dict) -> int | None:
    """Prefer the already-computed `recommended_calorie_target`; fall
    back to recomputing from weight+goal. Returns None if neither path
    yields a value (the user is then skipped with a warning)."""
    rec = row.get("rec_cal")
    if rec:
        return int(rec)
    w = row.get("weight_kg")
    g = row.get("goal")
    if w is None or not g:
        return None
    try:
        return int(calorie_target_from_profile(float(w), g))
    except Exception:
        return None


def _finalise_one(conn, row: dict, dry_run: bool) -> str:
    """Finalise one stuck user. Returns a one-line outcome string for
    the operator log."""
    uid       = row["user_id"]
    lang      = row["lang"]
    step      = row["step"]
    first_name = row["first_name"] or i18n_mod.t("onboarding.default_name",
                                                 locale=lang)

    target_cal = _resolve_target_cal(row)
    if target_cal is None:
        return (f"! uid={uid:>10} ({lang}) skipped (step={step}, "
                f"no rec_cal and can't recompute — missing weight/goal)")

    if dry_run:
        return (f"DRY uid={uid:>10} ({lang}) would set {target_cal} kcal, "
                f"tz=Europe/Kyiv, step=done")

    # Stage 1: flip the DB into the finalised state.
    update_profile(
        conn, uid,
        daily_calorie_target=target_cal,
        tz="Europe/Kyiv",
        onboarding_step="done",
    )

    # Stage 2: compute water target from weight (same path
    # _finalize_onboarding uses). Safe to skip on failure — the welcome
    # message has a 2000 ml fallback.
    water = None
    try:
        w = row.get("weight_kg")
        if w is not None:
            water = upsert_water_target_from_profile(conn, uid, float(w))
    except Exception as exc:
        print(f"  ⚠ water target upsert failed for uid={uid}: {exc!r}",
              flush=True)

    # Stage 3: send the welcome message with the main-menu keyboard.
    done_text = i18n_mod.t(
        "onboarding.done", locale=lang,
        name=first_name, cal=target_cal, water=water or 2000,
    )
    resp = send_message(uid, done_text,
                        reply_markup=main_menu_keyboard(locale=lang))
    if isinstance(resp, dict) and resp.get("ok") is False:
        if resp.get("error_code") in (400, 403):
            # Same auto-block semantics as the morning + evening crons.
            try:
                set_blocked(conn, uid, True)
            except Exception as exc:
                print(f"  ⚠ set_blocked failed for uid={uid}: {exc!r}",
                      flush=True)
            return (f"🚫 uid={uid:>10} ({lang}) blocked (DB updated, "
                    f"send rejected by Telegram {resp.get('error_code')})")
        return (f"⚠ uid={uid:>10} ({lang}) DB updated, send failed: "
                f"{str(resp)[:120]}")

    return (f"✓ uid={uid:>10} ({lang}) -> done @ {target_cal} kcal "
            f"(prev step={step})")


def run(rows: Iterable[dict], conn, dry_run: bool) -> None:
    """Process every stuck user. Pacing is global — one delay between
    sends, regardless of dry-run state (dry-runs run instant anyway
    because they don't call send_message)."""
    sent = 0
    for row in rows:
        outcome = _finalise_one(conn, row, dry_run=dry_run)
        print(outcome, flush=True)
        if not dry_run and outcome.startswith("✓"):
            sent += 1
            time.sleep(_SEND_DELAY_S)
    print(f"\nDone. {sent} users finalised + welcomed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen for each stuck user without "
             "writing to the DB or calling Telegram.",
    )
    args = parser.parse_args()

    conn = get_conn()
    try:
        rows = _select_stuck_users(conn)
        if not rows:
            print("0 users at awaiting_confirm / awaiting_custom_cal — "
                  "nothing to backfill.")
            return
        print(f"Found {len(rows)} stuck user(s):")
        for r in rows:
            print(f"  uid={r['user_id']} step={r['step']} lang={r['lang']} "
                  f"weight={r['weight_kg']} goal={r['goal']} "
                  f"rec_cal={r['rec_cal']}")
        print()
        run(rows, conn, dry_run=args.dry_run)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
