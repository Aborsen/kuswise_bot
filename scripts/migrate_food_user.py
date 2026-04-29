#!/usr/bin/env python3
"""One-shot: migrate user 699256397's data from the legacy `food` bot to KusWise.

What gets moved:
- Meals: every row from Food's `meals` table for the user, copied into
  KusWise's `meals` with ``photo_file_id`` stripped to NULL (Telegram
  file_ids are bot-bound and won't resolve in KusWise).
- Health profile: writes ``user_health_profile.allergens`` (8 canonical IDs),
  ``conditions`` (``["crohns"]``), and ``notes`` (audit trail of the original
  natural-language list from Food's hardcoded ``USER_PROFILE``).
- ``daily_logs``: NOT copied directly. Recomputed via ``recalc_daily_log()``
  per affected date so totals reflect any prior KusWise activity correctly.

What's NOT moved:
- KusWise ``user_profiles`` row — preserved as-is (lang, weight, goal, …).
- ``daily_recommendations`` (re-derivable from meals).
- ``chat_sessions`` / ``pending_*`` (transient state).
- Photo bytes — Telegram file_ids are bot-scoped (rejected re-upload).

Idempotent: dedups meals on ``(user_id, date, meal_type, description,
calories)``. Health profile is UPSERT — re-runs overwrite the same values.

Usage::

    .venv/bin/python scripts/migrate_food_user.py [--dry-run] [--user-id 699256397]

Env::

    DATABASE_URL       KusWise's Postgres (loaded from ./.env via load_dotenv).
    FOOD_DATABASE_URL  Food's Postgres. If unset, falls back to reading
                       ``DATABASE_URL`` from $FOOD_ENV_PATH (default
                       ``~/Desktop/Claude Code/Food/.env.local``).
    FOOD_ENV_PATH      Override path to Food's .env.local.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# Make ``lib.*`` importable when run from anywhere in the repo.
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg

try:
    from dotenv import load_dotenv, dotenv_values
    load_dotenv()  # loads KusWise's .env (repo root)
except ImportError:  # pragma: no cover — dev-only dependency
    dotenv_values = None  # type: ignore[assignment]

from lib.database import get_conn


# ---- Migration constants ----------------------------------------------------

# Crohn's disease — canonical KusWise condition ID (lib/health.py:24).
CONDITIONS_TO_SET: list[str] = ["crohns"]

# Mapping from Food's USER_PROFILE.allergies_and_intolerances
# natural-language items → canonical allergen IDs from lib/health.py.
# The four ``tomato/emmental/rye/rapeseed`` IDs were added in Phase A
# of this migration; the rest already existed in the EFSA-14 list.
ALLERGENS_TO_SET: list[str] = [
    "gluten",     # "gluten"
    "egg",        # "eggs"
    "mustard",    # "mustard"
    "tree_nut",   # "cashews" + "pistachios"
    "tomato",     # "tomatoes"
    "emmental",   # "emmental cheese" (kept specific, not collapsed to dairy)
    "rye",        # "rye" (kept specific, not collapsed to gluten)
    "rapeseed",   # "rapeseed (canola oil)"
]

NOTES_AUDIT = (
    "Migrated from Food bot 2026-04-28. Original list: tomatoes, gluten, "
    "eggs, mustard, emmental cheese, rye, rapeseed (canola oil), cashews, "
    "pistachios."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_daily_log(conn, user_id: int, date: str) -> bool:
    """Recompute ``daily_logs`` for ``(user_id, date)`` from the meals table.

    Mirrors ``lib.database.recalc_daily_log`` but uses INSERT…ON CONFLICT
    instead of plain UPDATE — needed for migrations where the row doesn't
    exist yet. Returns True if a daily_logs row now exists for this date,
    False if there are no meals on the date and we deleted any stub.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein_g),0),
                      COALESCE(SUM(carbs_g),0),  COALESCE(SUM(fat_g),0),
                      COALESCE(SUM(fiber_g),0),  COALESCE(SUM(sugar_g),0),
                      COUNT(*)
               FROM meals WHERE user_id = %s AND date = %s""",
            (user_id, date),
        )
        row = cur.fetchone()
        if not row or row[6] == 0:
            cur.execute(
                "DELETE FROM daily_logs WHERE user_id = %s AND date = %s",
                (user_id, date),
            )
            return False
        cur.execute(
            """INSERT INTO daily_logs (
                   user_id, date,
                   total_calories, total_protein_g, total_carbs_g,
                   total_fat_g, total_fiber_g, total_sugar_g,
                   summary_sent, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s)
               ON CONFLICT (user_id, date) DO UPDATE SET
                   total_calories  = EXCLUDED.total_calories,
                   total_protein_g = EXCLUDED.total_protein_g,
                   total_carbs_g   = EXCLUDED.total_carbs_g,
                   total_fat_g     = EXCLUDED.total_fat_g,
                   total_fiber_g   = EXCLUDED.total_fiber_g,
                   total_sugar_g   = EXCLUDED.total_sugar_g""",
            (
                user_id, date,
                row[0], row[1], row[2], row[3], row[4], row[5],
                _now_iso(),
            ),
        )
        return True


def _resolve_food_url() -> str | None:
    """Find Food's DATABASE_URL — env var first, then Food's .env.local."""
    url = os.environ.get("FOOD_DATABASE_URL", "").strip()
    if url:
        return url
    if dotenv_values is None:
        return None
    food_env = os.environ.get(
        "FOOD_ENV_PATH",
        os.path.expanduser("~/Desktop/Claude Code/Food/.env.local"),
    )
    if not os.path.exists(food_env):
        return None
    vals = dotenv_values(food_env)
    return (vals.get("DATABASE_URL") or "").strip() or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate one user's data from Food bot → KusWise.",
    )
    parser.add_argument("--user-id", type=int, default=699256397)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe + print plan; make no DB writes.",
    )
    args = parser.parse_args()

    food_url = _resolve_food_url()
    if not food_url:
        print(
            "ERROR: cannot find Food's DATABASE_URL. Set $FOOD_DATABASE_URL "
            "or ensure ~/Desktop/Claude Code/Food/.env.local has it.",
            file=sys.stderr,
        )
        return 1

    # 1. Verify KusWise has a profile row for the target user — abort if not
    #    (we never create new profiles here; that would orphan the data and
    #    bypass the F-2b onboarding flow that sets defaults correctly).
    kw_conn = get_conn()
    try:
        with kw_conn.cursor() as cur:
            cur.execute(
                "SELECT lang, weight_kg, goal, daily_calorie_target "
                "FROM user_profiles WHERE user_id = %s",
                (args.user_id,),
            )
            profile = cur.fetchone()
        if not profile:
            print(
                f"ERROR: user_id={args.user_id} has no user_profiles row in "
                f"KusWise. Aborting (would orphan migrated data).",
                file=sys.stderr,
            )
            return 1
        print(
            f"KusWise profile (preserved): "
            f"lang={profile[0]} weight={profile[1]} "
            f"goal={profile[2]} cal_target={profile[3]}"
        )

        # 2. Pull all meals from Food in insertion order (id ASC preserves the
        #    original chronological logging sequence on dates with multiple
        #    meals — the BIGSERIAL on the destination side gets fresh IDs).
        food_conn = psycopg.connect(food_url)
        try:
            with food_conn.cursor() as cur:
                cur.execute(
                    """SELECT date, meal_type, description, ingredients,
                              allergen_warnings, crohn_warnings,
                              calories, protein_g, carbs_g, fat_g,
                              fiber_g, sugar_g,
                              ai_raw_response, created_at
                       FROM meals WHERE user_id = %s
                       ORDER BY id ASC""",
                    (args.user_id,),
                )
                food_meals = cur.fetchall()
        finally:
            food_conn.close()

        print(f"Food source: {len(food_meals)} meal rows for user_id={args.user_id}")

        # 3. Health profile — single atomic UPSERT (so partial state can't
        #    leak if the run is interrupted mid-write).
        if args.dry_run:
            print(
                f"[dry-run] would UPSERT user_health_profile: "
                f"allergens={ALLERGENS_TO_SET} conditions={CONDITIONS_TO_SET} "
                f"notes={NOTES_AUDIT[:50]!r}…"
            )
        else:
            with kw_conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO user_health_profile
                           (user_id, allergens, conditions, notes, updated_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                           allergens  = EXCLUDED.allergens,
                           conditions = EXCLUDED.conditions,
                           notes      = EXCLUDED.notes,
                           updated_at = EXCLUDED.updated_at""",
                    (
                        args.user_id,
                        ALLERGENS_TO_SET,
                        CONDITIONS_TO_SET,
                        NOTES_AUDIT,
                        _now_iso(),
                    ),
                )
            kw_conn.commit()
            print(
                f"Health profile written: "
                f"{len(ALLERGENS_TO_SET)} allergens, "
                f"{len(CONDITIONS_TO_SET)} conditions"
            )

        # 4. Insert meals — dedup against existing KusWise rows on a tuple
        #    that's stable enough to identify "the same meal" across re-runs:
        #    (user_id, date, meal_type, description, calories). Two meals
        #    with identical text + nutrition on the same date are extremely
        #    unlikely to be intentional duplicates.
        affected_dates: set[str] = set()
        inserted = 0
        skipped_dup = 0
        for row in food_meals:
            (
                date,
                meal_type,
                description,
                ingredients,
                allergen_warnings,
                crohn_warnings,
                calories,
                protein,
                carbs,
                fat,
                fiber,
                sugar,
                ai_raw,
                created_at,
            ) = row

            with kw_conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM meals
                       WHERE user_id = %s AND date = %s AND meal_type = %s
                         AND description = %s AND calories = %s
                       LIMIT 1""",
                    (args.user_id, date, meal_type, description, calories),
                )
                # Track every date we encountered, even on dedup-skip — a
                # re-run with all meals already imported should still refresh
                # daily_logs (e.g. to recover from a prior partial run).
                affected_dates.add(date)
                if cur.fetchone():
                    skipped_dup += 1
                    continue

                if args.dry_run:
                    inserted += 1
                    continue

                cur.execute(
                    """INSERT INTO meals (
                           user_id, date, meal_type, description, ingredients,
                           allergen_warnings, crohn_warnings,
                           calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
                           photo_file_id, ai_raw_response, created_at
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           NULL, %s, %s
                       )""",
                    (
                        args.user_id,
                        date,
                        meal_type,
                        description,
                        ingredients,
                        allergen_warnings,
                        crohn_warnings,
                        calories,
                        protein,
                        carbs,
                        fat,
                        fiber,
                        sugar,
                        ai_raw,
                        created_at,
                    ),
                )
                inserted += 1

        if not args.dry_run:
            kw_conn.commit()

        # 5. Recompute daily_logs for every date we touched. Reads the
        #    meals table for the truth, so it correctly merges Food's
        #    history with anything KusWise already had on the same date.
        if not args.dry_run:
            for date in sorted(affected_dates):
                _upsert_daily_log(kw_conn, args.user_id, date)
            kw_conn.commit()

        prefix = "[dry-run] " if args.dry_run else ""
        print(
            f"{prefix}meals: inserted={inserted} skipped_dup={skipped_dup}  "
            f"dates_recomputed={len(affected_dates)}"
        )
        return 0

    finally:
        kw_conn.close()


if __name__ == "__main__":
    sys.exit(main())
