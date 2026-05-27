"""Postgres (Neon) database layer: connection, schema migration, and CRUD helpers."""
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg

from lib.config import DATABASE_URL, LOCAL_TZ


def get_conn():
    """Return a fresh psycopg3 connection. Call per invocation (serverless)."""
    return psycopg.connect(DATABASE_URL, autocommit=False)


def _now_iso() -> str:
    # Timestamps stay in UTC — absolute time, unambiguous in the DB.
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    # "Today" is a calendar concept — compute from Kyiv local so meals logged
    # between midnight Kyiv and midnight UTC don't fall on the wrong day.
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


# Module-level flag so init_db only runs once per warm Vercel function instance.
# psycopg's CREATE TABLE IF NOT EXISTS / ALTER TABLE … IF NOT EXISTS are
# idempotent but cheap-to-skip — running them on every request is wasteful
# and creates concurrent-DDL load on Neon during deploys.
_SCHEMA_INITIALISED = False


def init_db(conn=None, force: bool = False) -> None:
    """Create tables if they don't exist. Idempotent — but cached after the
    first call within a function instance. Pass force=True to bypass the cache
    (e.g. for local migrations)."""
    global _SCHEMA_INITIALISED
    if _SCHEMA_INITIALISED and not force:
        return

    close_after = False
    if conn is None:
        conn = get_conn()
        close_after = True

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                created_at TEXT
            )
        """)
        # `username` stores the Telegram @handle ONLY (lowercase ASCII /
        # underscores, may be empty); `first_name` stores the Telegram
        # display name. Historically `username` was a misnomer that held
        # whichever of (handle, first_name) was non-empty, which made the
        # admin panel render `@Anna` for users who had no public handle.
        cur.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT ''"
        )
        # F-15 attribution: token captured from Telegram's `/start <token>`
        # deep-link parameter (`t.me/<bot>?start=<token>`). Empty = organic
        # / typed /start. First-write-wins — never overwritten on later
        # /start tapps so repeat-clickers stay attributed to their first
        # arrival surface. `source_seen_at` records when that first-touch
        # landed.
        cur.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT ''"
        )
        cur.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS source_seen_at TEXT"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_logs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                date TEXT,
                total_calories DOUBLE PRECISION DEFAULT 0,
                total_protein_g DOUBLE PRECISION DEFAULT 0,
                total_carbs_g DOUBLE PRECISION DEFAULT 0,
                total_fat_g DOUBLE PRECISION DEFAULT 0,
                total_fiber_g DOUBLE PRECISION DEFAULT 0,
                total_sugar_g DOUBLE PRECISION DEFAULT 0,
                summary_sent INTEGER DEFAULT 0,
                created_at TEXT,
                UNIQUE(user_id, date)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                date TEXT,
                meal_type TEXT,
                description TEXT,
                ingredients TEXT,
                allergen_warnings TEXT,
                crohn_warnings TEXT,
                calories DOUBLE PRECISION,
                protein_g DOUBLE PRECISION,
                carbs_g DOUBLE PRECISION,
                fat_g DOUBLE PRECISION,
                fiber_g DOUBLE PRECISION,
                sugar_g DOUBLE PRECISION,
                photo_file_id TEXT,
                ai_raw_response TEXT,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_recommendations (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                date TEXT,
                recommendation TEXT,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_photos (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                photo_file_id TEXT,
                created_at TEXT
            )
        """)
        cur.execute("ALTER TABLE pending_photos ADD COLUMN IF NOT EXISTS text_description TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_analyses (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                meal_type TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                photo_file_id TEXT,
                text_description TEXT,
                raw_response TEXT,
                awaiting_manual INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                candidates_json TEXT
            )
        """)
        # F-6: in-flight ambiguous-photo candidates list. NULL = unambiguous
        # (use the standard preview flow). Backfilled on first init.
        cur.execute(
            "ALTER TABLE pending_analyses ADD COLUMN IF NOT EXISTS candidates_json TEXT"
        )
        # F-meal-edit: when the /meals → ✏️ Edit path stages a pending
        # analysis, this column carries the meal id we need to delete on
        # confirm. NULL for fresh logs (the common case).
        cur.execute(
            "ALTER TABLE pending_analyses ADD COLUMN IF NOT EXISTS replaces_meal_id BIGINT"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_ts "
            "ON chat_sessions (user_id, created_at)"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id BIGINT PRIMARY KEY,
                age INTEGER,
                sex TEXT,
                weight_kg DOUBLE PRECISION,
                height_cm INTEGER,
                gym_per_week TEXT,
                goal TEXT,
                daily_calorie_target INTEGER,
                recommended_calorie_target INTEGER,
                onboarding_step TEXT DEFAULT 'awaiting_age',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        cur.execute("ALTER TABLE meals ADD COLUMN IF NOT EXISTS is_favorite INTEGER DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS water_logs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount_ml INTEGER NOT NULL,
                logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_water_user_logged "
            "ON water_logs(user_id, logged_at DESC)"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS water_prefs (
                user_id BIGINT PRIMARY KEY,
                target_ml INTEGER NOT NULL DEFAULT 2000,
                target_overridden INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )
        """)
        cur.execute(
            "ALTER TABLE water_prefs ADD COLUMN IF NOT EXISTS target_overridden INTEGER NOT NULL DEFAULT 0"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS awaiting_input_type TEXT"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS weekly_checkin_sent_at TEXT"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS target_weight_kg DOUBLE PRECISION"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS tz TEXT NOT NULL DEFAULT 'Europe/Kyiv'"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'en'"
        )
        # F-2b: timestamp the user explicitly confirmed (or actively chose)
        # their language. NULL means we auto-detected without confirmation —
        # those users get the language-confirm onboarding step zero on next
        # /start so they can override the (possibly wrong) auto-detect.
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS lang_confirmed_at TEXT"
        )
        # F-5: weekly weight-change goal in kg (negative for "lose", positive
        # for "gain"). NULL = use sane defaults derived from goal direction.
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS weekly_delta_kg DOUBLE PRECISION"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_nudge_sent_at TEXT"
        )
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS nudge_optout INTEGER NOT NULL DEFAULT 0"
        )
        # F-16: `blocked_at` is stamped when Telegram returns 400/403 on send
        # (user blocked the bot). Distinct from `nudge_optout` which tracks
        # explicit user muting via `/quiet` or the legacy 🔕 button. Both
        # gates are checked when building any notification cohort. NULL =
        # not blocked; auto-cleared when the user messages the bot again.
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS blocked_at TEXT"
        )
        # F-16: `last_morning_sent_at` mirrors `last_nudge_sent_at` for the
        # daily 8:30-local good-morning cron. Per-user-local-day dedup.
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS last_morning_sent_at TEXT"
        )
        # F-17: never-logger activation funnel state machine.
        # NULL / ''         → no activation message sent yet (day 0–1 cohort)
        # 'demo'            → day-2 first-meal demo card sent
        # 'd4_followup'     → day-4 lighter follow-up sent
        # 'd7_final'        → day-7 final softer message sent
        # 'auto_quieted'    → set to this when day-9 midnight sweep flipped
        #                     nudge_optout=1 (marker only; nudge_optout is
        #                     the gate enforced elsewhere)
        # The column stays NULL forever for users who log their first meal
        # before day 2 — they bypass the funnel entirely.
        cur.execute(
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS activation_step TEXT"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weight_history (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                weight_kg DOUBLE PRECISION NOT NULL,
                source TEXT NOT NULL DEFAULT 'checkin',
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_weight_user_time "
            "ON weight_history(user_id, recorded_at DESC)"
        )
        # Per-user daily action quotas (rate limiting). One row per
        # (user_id, action, day). consume_quota() does an atomic upsert+increment.
        # Old days are pruned by the midnight cron.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_quota (
                user_id BIGINT NOT NULL,
                action TEXT NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, action, day)
            )
        """)
        # Health profile (F-1): allergens + chronic conditions injected into
        # vision/text analysis prompts so the model can flag user-specific
        # triggers (lactose for Crohn's, GI > 70 for diabetes, etc.).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_health_profile (
                user_id BIGINT PRIMARY KEY,
                allergens TEXT[] NOT NULL DEFAULT '{}',
                conditions TEXT[] NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
        """)
        # Engagement streaks (F-4): consecutive-day meal-log streak per user,
        # with 3 monthly "freeze" tokens that absorb a single missed day.
        # Updated on every accepted meal; freezes reset to 3 on UTC day-1.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_streaks (
                user_id BIGINT PRIMARY KEY,
                current_streak INTEGER NOT NULL DEFAULT 0,
                longest_streak INTEGER NOT NULL DEFAULT 0,
                last_log_date TEXT,
                freeze_days_remaining INTEGER NOT NULL DEFAULT 3,
                updated_at TEXT
            )
        """)
        # F-7: audit trail of every analysis the user corrected (manual edit,
        # recalc, picked alternate). Read by /aliases and aggregated into
        # user_food_aliases below.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                source TEXT NOT NULL,
                original_json TEXT NOT NULL,
                corrected_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_corrections_user_time "
            "ON corrections(user_id, created_at DESC)"
        )
        # F-7: per-user food aliases. Maintained as an EWMA of recent accepted
        # meals so the user's "usual" portion drifts with their habits.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_food_aliases (
                user_id BIGINT NOT NULL,
                alias TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                default_grams DOUBLE PRECISION,
                default_kcal DOUBLE PRECISION,
                default_protein_g DOUBLE PRECISION,
                default_fat_g DOUBLE PRECISION,
                default_carbs_g DOUBLE PRECISION,
                sample_count INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT,
                PRIMARY KEY (user_id, alias)
            )
        """)
        # F-9: menu OCR results — short-lived per-user cache. Replaced on every
        # /menu invocation so callbacks can reference a stable list of dishes.
        # Pruned aggressively (1h) by the midnight cron.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS menu_ocr_results (
                user_id BIGINT PRIMARY KEY,
                dishes_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # F-10: 3-day meal plans. Stored as JSON so the renderer + per-meal
        # log callbacks can re-read individual slots by index. Pruned at 90d
        # by the midnight cron (history is useful but not forever).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meal_plans (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_meal_plans_user_time "
            "ON meal_plans(user_id, created_at DESC)"
        )
        # AI menu merge: per-user library of saved recipes. Recipes are
        # generated by /suggest_meal and saved by tapping ⭐ Save on the
        # follow-up keyboard. Separate from `meals` because they lack
        # structured calorie/macro values — they're plaintext recipes,
        # not logged consumption events.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS saved_recipes (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                body TEXT NOT NULL,
                pantry TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_recipes_user_time "
            "ON saved_recipes(user_id, created_at DESC)"
        )
        # F-14: one row per cron invocation. Admin panel reads the most-recent
        # row per `cron_name` to surface last-run status and counts. Each
        # `run_*()` writes a row in its `finally:` block.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cron_runs (
                id BIGSERIAL PRIMARY KEY,
                cron_name TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ,
                status TEXT NOT NULL DEFAULT 'running',
                result_json TEXT,
                error TEXT
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_name_time "
            "ON cron_runs(cron_name, started_at DESC)"
        )
    conn.commit()
    _SCHEMA_INITIALISED = True
    if close_after:
        try:
            conn.close()
        except Exception:
            pass


# ---------- Users ----------

def upsert_user(conn, user_id: int, username: Optional[str],
                first_name: Optional[str] = "",
                source: Optional[str] = "") -> None:
    """Insert or refresh a `users` row.

    `username` must be the bare Telegram @handle (no leading "@"), or empty
    string for users without a public handle. `first_name` is the display
    name. `source` is the sanitised `/start <token>` deep-link parameter
    (F-15 attribution); callers should pre-sanitise to `[A-Za-z0-9_-]{0,64}`.

    Conflicts UPDATE `username` and `first_name` so admin reads stay fresh
    when a user later sets / changes their handle or display name. `source`
    is deliberately NOT updated on conflict — first attribution wins; a
    repeat-tapper of a different tagged link keeps their original source.
    `source_seen_at` is set only on the INSERT path for the same reason.
    """
    src = (source or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO users (user_id, username, first_name, source,
                                  source_seen_at, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   username   = EXCLUDED.username,
                   first_name = EXCLUDED.first_name""",
            (user_id, username or "", first_name or "", src,
             _now_iso() if src else None, _now_iso()),
        )
    conn.commit()


# ---------- User profiles (onboarding + settings) ----------

PROFILE_COLUMNS = [
    "user_id", "age", "sex", "weight_kg", "height_cm", "gym_per_week",
    "goal", "daily_calorie_target", "recommended_calorie_target",
    "onboarding_step", "created_at", "updated_at",
    "awaiting_input_type", "weekly_checkin_sent_at", "target_weight_kg",
    "tz", "lang", "weekly_delta_kg", "lang_confirmed_at",
    "last_nudge_sent_at", "nudge_optout",
    "blocked_at", "last_morning_sent_at", "activation_step",
]


def _profile_row_to_dict(row) -> dict:
    return dict(zip(PROFILE_COLUMNS, row))


def get_profile(conn, user_id: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(PROFILE_COLUMNS)} FROM user_profiles WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    return _profile_row_to_dict(row) if row else None


def ensure_profile_row(conn, user_id: int) -> dict:
    """Create an empty profile row for a new user if missing, return it."""
    profile = get_profile(conn, user_id)
    if profile:
        return profile
    now = _now_iso()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO user_profiles (user_id, onboarding_step, created_at, updated_at)
               VALUES (%s, 'awaiting_age', %s, %s)
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, now, now),
        )
    conn.commit()
    return get_profile(conn, user_id) or {"user_id": user_id, "onboarding_step": "awaiting_age"}


_ALLOWED_PROFILE_FIELDS = {
    "age", "sex", "weight_kg", "height_cm", "gym_per_week", "goal",
    "daily_calorie_target", "recommended_calorie_target", "onboarding_step",
    "awaiting_input_type", "weekly_checkin_sent_at", "target_weight_kg",
    "tz", "lang", "weekly_delta_kg", "lang_confirmed_at",
    "last_nudge_sent_at", "nudge_optout",
    "blocked_at", "last_morning_sent_at", "activation_step",
}


def update_profile(conn, user_id: int, **fields) -> None:
    """Update one or more profile fields. Whitelisted columns only."""
    fields = {k: v for k, v in fields.items() if k in _ALLOWED_PROFILE_FIELDS}
    if not fields:
        return
    fields["updated_at"] = _now_iso()
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [user_id]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE user_profiles SET {sets} WHERE user_id = %s", values)
    conn.commit()


def profile_is_complete(profile: Optional[dict]) -> bool:
    return bool(profile and profile.get("onboarding_step") == "done"
                and profile.get("daily_calorie_target"))


def delete_user_all_data(conn, user_id: int) -> bool:
    """Delete a user and all their data. Returns True if the user existed."""
    with conn.cursor() as cur:
        for table in (
            "pending_photos", "pending_analyses", "chat_sessions",
            "daily_recommendations", "daily_logs", "meals", "user_profiles",
            "water_logs", "water_prefs", "weight_history",
            "usage_quota", "user_health_profile", "user_streaks",
            "corrections", "user_food_aliases", "menu_ocr_results",
            "meal_plans",
        ):
            cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted


def reset_onboarding(conn, user_id: int) -> None:
    """Kick the user back to the start of the onboarding flow.

    Step name must match the current Q1 — the 2026-05 reorder moved
    sex from Q2 to Q1, so this writes ``awaiting_sex``. Drifting from
    the current Q1 step name silently breaks all restart paths:
    `reset_onboarding` writes a stale step, the caller sends the
    correct Q1 prompt, the user taps a button, the callback handler's
    `step == 'awaiting_sex'` gate rejects with `toast.already_answered`,
    and the user is stuck.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET onboarding_step = 'awaiting_sex', updated_at = %s "
            "WHERE user_id = %s",
            (_now_iso(), user_id),
        )
    conn.commit()


def list_onboarded_user_ids(conn) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM user_profiles WHERE onboarding_step = 'done'")
        rows = cur.fetchall()
    return [r[0] for r in rows]


# ---------- Pending photos ----------

def save_pending_photo(conn, user_id: int, photo_file_id: str) -> None:
    # DELETE+INSERT must commit atomically; psycopg connections default to
    # autocommit=False so the implicit transaction covers both statements
    # before the single conn.commit().
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_photos WHERE user_id = %s", (user_id,))
            cur.execute(
                "INSERT INTO pending_photos (user_id, photo_file_id, text_description, created_at) "
                "VALUES (%s, %s, NULL, %s)",
                (user_id, photo_file_id, _now_iso()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def save_pending_text(conn, user_id: int, text_description: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_photos WHERE user_id = %s", (user_id,))
            cur.execute(
                "INSERT INTO pending_photos (user_id, photo_file_id, text_description, created_at) "
                "VALUES (%s, NULL, %s, %s)",
                (user_id, text_description, _now_iso()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def pop_pending_entry(conn, user_id: int) -> Optional[tuple[Optional[str], Optional[str]]]:
    """Return (photo_file_id, text_description) then delete all pending for user."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT photo_file_id, text_description FROM pending_photos "
            "WHERE user_id = %s ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        file_id, text = row[0], row[1]
        cur.execute("DELETE FROM pending_photos WHERE user_id = %s", (user_id,))
    conn.commit()
    return (file_id, text)


def cleanup_stale_pending(conn, minutes: int = 10) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pending_photos WHERE created_at < %s", (cutoff,))
    conn.commit()


# ---------- Pending analyses (moderation step) ----------

def save_pending_analysis(
    conn,
    user_id: int,
    meal_type: str,
    analysis: dict,
    photo_file_id: Optional[str],
    text_description: Optional[str],
    raw_response: str,
    candidates: Optional[list] = None,
    replaces_meal_id: Optional[int] = None,
) -> None:
    """Store an AI analysis for user review. One row per user (replaces previous).

    ``candidates`` (F-6) is the optional top_guesses list when the photo is
    ambiguous. NULL = use the standard moderation keyboard.

    ``replaces_meal_id`` is set by the /meals → ✏️ Edit flow so the confirm
    handler knows which old meal row to delete after the new one is
    inserted. NULL for fresh logs.

    DELETE + INSERT run in a single autocommit=False transaction so concurrent
    callers can never see "deleted but not yet inserted" state.
    """
    try:
        candidates_json = (
            json.dumps(candidates, ensure_ascii=False) if candidates else None
        )
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_analyses WHERE user_id = %s", (user_id,))
            cur.execute(
                """INSERT INTO pending_analyses
                   (user_id, meal_type, analysis_json, photo_file_id, text_description,
                    raw_response, awaiting_manual, created_at, candidates_json, replaces_meal_id)
                   VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s)""",
                (
                    user_id,
                    meal_type,
                    json.dumps(analysis, ensure_ascii=False),
                    photo_file_id,
                    text_description,
                    raw_response,
                    _now_iso(),
                    candidates_json,
                    replaces_meal_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_pending_analysis(conn, user_id: int) -> Optional[dict]:
    """Non-destructive read of the user's pending analysis.

    Defensive against a corrupt `analysis_json` row: if the JSON can't be
    parsed (NULL, malformed, truncated write) we drop the row and return
    None instead of raising. Without this guard one corrupt pending row
    would block all meal-logging for that user until manual intervention.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, meal_type, analysis_json, photo_file_id, text_description,
                      raw_response, awaiting_manual, created_at, candidates_json,
                      replaces_meal_id
               FROM pending_analyses WHERE user_id = %s ORDER BY id DESC LIMIT 1""",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    candidates = []
    if row[8]:
        try:
            candidates = json.loads(row[8]) or []
        except (TypeError, ValueError):
            candidates = []
    try:
        analysis_value = json.loads(row[2]) if row[2] else {}
    except (TypeError, ValueError):
        # Drop the corrupt row in a fresh cursor and surface to Sentry.
        # Returning None puts the user back in a clean state — they can
        # re-send the meal photo / text without admin intervention.
        from lib.log import error as _log_error
        _log_error("pending_analysis_corrupt_json",
                   user_id=user_id, row_id=row[0])
        with conn.cursor() as cur_drop:
            cur_drop.execute("DELETE FROM pending_analyses WHERE id = %s", (row[0],))
        conn.commit()
        return None
    return {
        "id": row[0],
        "meal_type": row[1],
        "analysis": analysis_value,
        "photo_file_id": row[3],
        "text_description": row[4],
        "raw_response": row[5],
        "awaiting_manual": bool(row[6]),
        "created_at": row[7],
        "candidates": candidates,
        "replaces_meal_id": row[9],
    }


def pop_pending_analysis(conn, user_id: int) -> Optional[dict]:
    """Read + delete the user's pending analysis."""
    entry = get_pending_analysis(conn, user_id)
    if entry is None:
        return None
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pending_analyses WHERE user_id = %s", (user_id,))
    conn.commit()
    return entry


def set_awaiting_manual(conn, user_id: int, meal_type: Optional[str] = None) -> None:
    """Flag the user's pending analysis as awaiting manual text input."""
    with conn.cursor() as cur:
        if meal_type:
            cur.execute(
                "UPDATE pending_analyses SET awaiting_manual = 1, meal_type = %s WHERE user_id = %s",
                (meal_type, user_id),
            )
        else:
            cur.execute(
                "UPDATE pending_analyses SET awaiting_manual = 1 WHERE user_id = %s",
                (user_id,),
            )
    conn.commit()


def cleanup_stale_analyses(conn, minutes: int = 10) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pending_analyses WHERE created_at < %s", (cutoff,))
    conn.commit()


# ---------- Chat sessions (multi-turn /ask history) ----------

def get_chat_history(conn, user_id: int, limit: int = 10, minutes: int = 60) -> list[dict]:
    """Return the user's recent chat messages (within `minutes`), oldest first.

    Shape matches what OpenAI expects: [{"role": "user"|"assistant", "content": "..."}].
    Rows older than `minutes` are treated as a new conversation.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT role, content FROM chat_sessions
               WHERE user_id = %s AND created_at >= %s
               ORDER BY id DESC LIMIT %s""",
            (user_id, cutoff, limit),
        )
        rows = cur.fetchall()
    # Fetched newest-first for the LIMIT; flip to chronological order for the LLM.
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


# Cap on stored chat content. Bounds the size of every replay into the LLM
# context and the storage footprint per row. Anything longer is truncated
# with an ellipsis so the model still sees the start of the message.
_MAX_CHAT_CONTENT_CHARS = 2000


def append_chat_message(conn, user_id: int, role: str, content: str) -> None:
    safe_content = content or ""
    if len(safe_content) > _MAX_CHAT_CONTENT_CHARS:
        safe_content = safe_content[: _MAX_CHAT_CONTENT_CHARS - 1] + "…"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_sessions (user_id, role, content, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, role, safe_content, _now_iso()),
        )
    conn.commit()


def cleanup_stale_chat(conn, minutes: int = 60) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chat_sessions WHERE created_at < %s", (cutoff,))
    conn.commit()


def count_chat_messages(conn, user_id: int, minutes: int = 60) -> int:
    """Count chat-session rows in the last N minutes for thread indicator."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM chat_sessions "
            "WHERE user_id = %s AND created_at >= %s",
            (user_id, cutoff),
        )
        n = cur.fetchone()[0]
    return int(n or 0)


def clear_chat_history(conn, user_id: int) -> int:
    """Delete every chat-session row for `user_id`. Returns rows deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM chat_sessions WHERE user_id = %s",
            (user_id,),
        )
        deleted = cur.rowcount
    conn.commit()
    return int(deleted or 0)


# ---------- Saved AI recipes ----------

def save_recipe(conn, user_id: int, body: str, pantry: str = "") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO saved_recipes (user_id, body, pantry, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (user_id, body, pantry, _now_iso()),
        )
        new_id = cur.fetchone()[0]
    conn.commit()
    return int(new_id)


def list_recipes(conn, user_id: int, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, body, pantry, created_at FROM saved_recipes "
            "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "body": r[1], "pantry": r[2] or "", "created_at": r[3]}
        for r in rows
    ]


def get_recipe(conn, user_id: int, recipe_id: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, body, pantry, created_at FROM saved_recipes "
            "WHERE id = %s AND user_id = %s",
            (recipe_id, user_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "body": row[1], "pantry": row[2] or "", "created_at": row[3]}


def delete_recipe(conn, user_id: int, recipe_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM saved_recipes WHERE id = %s AND user_id = %s",
            (recipe_id, user_id),
        )
        deleted = cur.rowcount
    conn.commit()
    return bool(deleted and deleted > 0)


# ---------- Meals ----------

def save_meal(
    conn,
    user_id: int,
    meal_type: str,
    analysis: dict,
    photo_file_id: str,
    raw_response: str,
) -> int:
    nutrition = analysis.get("nutrition", {}) or {}
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO meals (
                user_id, date, meal_type, description, ingredients,
                allergen_warnings, crohn_warnings,
                calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
                photo_file_id, ai_raw_response, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                user_id,
                _today_str(),
                meal_type,
                analysis.get("description") or analysis.get("dish_name", ""),
                json.dumps(analysis.get("ingredients", []), ensure_ascii=False),
                json.dumps(analysis.get("allergen_flags", []), ensure_ascii=False),
                json.dumps(analysis.get("crohn_flags", []), ensure_ascii=False),
                float(nutrition.get("calories", 0) or 0),
                float(nutrition.get("protein_g", 0) or 0),
                float(nutrition.get("carbs_g", 0) or 0),
                float(nutrition.get("fat_g", 0) or 0),
                float(nutrition.get("fiber_g", 0) or 0),
                float(nutrition.get("sugar_g", 0) or 0),
                photo_file_id,
                raw_response,
                _now_iso(),
            ),
        )
        meal_id = cur.fetchone()[0]
    conn.commit()
    return meal_id


def get_meals_for_day(conn, user_id: int, date: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, meal_type, description, ingredients, allergen_warnings, crohn_warnings,
                      calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g, created_at
               FROM meals WHERE user_id = %s AND date = %s ORDER BY id ASC""",
            (user_id, date),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "meal_type": r[1],
            "description": r[2],
            "ingredients": json.loads(r[3] or "[]"),
            "allergen_warnings": json.loads(r[4] or "[]"),
            "crohn_warnings": json.loads(r[5] or "[]"),
            "calories": r[6] or 0,
            "protein_g": r[7] or 0,
            "carbs_g": r[8] or 0,
            "fat_g": r[9] or 0,
            "fiber_g": r[10] or 0,
            "sugar_g": r[11] or 0,
            "created_at": r[12],
        }
        for r in rows
    ]


def delete_meal_admin(conn, meal_id: int) -> Optional[dict]:
    """Delete any meal by ID (admin use, no user check). Returns meal data or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT meal_type, description, date, calories, user_id FROM meals WHERE id = %s",
            (meal_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        data = {"meal_type": row[0], "description": row[1], "date": row[2],
                "calories": row[3] or 0, "user_id": row[4]}
        cur.execute("DELETE FROM meals WHERE id = %s", (meal_id,))
    conn.commit()
    return data


def delete_meal(conn, meal_id: int, user_id: int) -> Optional[dict]:
    """Delete a meal by ID (must belong to user). Returns its data for confirmation, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT meal_type, description, date, calories FROM meals WHERE id = %s AND user_id = %s",
            (meal_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        data = {"meal_type": row[0], "description": row[1], "date": row[2], "calories": row[3] or 0}
        cur.execute("DELETE FROM meals WHERE id = %s AND user_id = %s", (meal_id, user_id))
    conn.commit()
    return data


def recalc_daily_log(conn, user_id: int, date: str) -> None:
    """Recompute ``daily_logs`` for ``(user_id, date)`` from the meals table.

    Idempotent UPSERT — when no meals remain on this date, the row is
    deleted; otherwise it's INSERT-or-UPDATEd to the SUM of remaining
    meals. The INSERT side matters for the favorites / recent re-log
    path: ``clone_meal_for_today`` calls this after inserting a cloned
    meal, and the user may have no fresh-log row yet for that date
    (a fresh log goes through ``upsert_daily_log_from_meal`` which has
    its own INSERT). Without the INSERT side here, a string of
    favorites-relogs without any fresh log silently leaves
    ``daily_logs`` empty — the dashboard then shows 0 / a stale total
    even though the meals exist.
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
        else:
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
    conn.commit()


# ---------- Daily logs ----------

def upsert_daily_log_from_meal(conn, user_id: int, analysis: dict) -> None:
    """Insert today's row if needed, then increment totals from this meal."""
    today = _today_str()
    nutrition = analysis.get("nutrition", {}) or {}
    cal = float(nutrition.get("calories", 0) or 0)
    p = float(nutrition.get("protein_g", 0) or 0)
    c = float(nutrition.get("carbs_g", 0) or 0)
    f = float(nutrition.get("fat_g", 0) or 0)
    fib = float(nutrition.get("fiber_g", 0) or 0)
    sug = float(nutrition.get("sugar_g", 0) or 0)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO daily_logs (user_id, date, total_calories, total_protein_g,
                                       total_carbs_g, total_fat_g, total_fiber_g, total_sugar_g,
                                       summary_sent, created_at)
               VALUES (%s, %s, 0, 0, 0, 0, 0, 0, 0, %s)
               ON CONFLICT (user_id, date) DO NOTHING""",
            (user_id, today, _now_iso()),
        )
        cur.execute(
            """UPDATE daily_logs
               SET total_calories = total_calories + %s,
                   total_protein_g = total_protein_g + %s,
                   total_carbs_g = total_carbs_g + %s,
                   total_fat_g = total_fat_g + %s,
                   total_fiber_g = total_fiber_g + %s,
                   total_sugar_g = total_sugar_g + %s
               WHERE user_id = %s AND date = %s""",
            (cal, p, c, f, fib, sug, user_id, today),
        )
    conn.commit()


def get_log_for_date(conn, user_id: int, date: str) -> dict:
    """Return the daily log for any date. Same shape as get_today_log."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT total_calories, total_protein_g, total_carbs_g, total_fat_g,
                      total_fiber_g, total_sugar_g
               FROM daily_logs WHERE user_id = %s AND date = %s""",
            (user_id, date),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM meals WHERE user_id = %s AND date = %s",
            (user_id, date),
        )
        meal_count = (cur.fetchone() or (0,))[0]
    if not row:
        return {
            "date": date, "calories": 0, "protein": 0, "carbs": 0,
            "fat": 0, "fiber": 0, "sugar": 0, "meal_count": meal_count,
        }
    return {
        "date": date,
        "calories": row[0] or 0,
        "protein": row[1] or 0,
        "carbs": row[2] or 0,
        "fat": row[3] or 0,
        "fiber": row[4] or 0,
        "sugar": row[5] or 0,
        "meal_count": meal_count,
    }


def get_today_log(conn, user_id: int) -> dict:
    return get_log_for_date(conn, user_id, _today_str())


def get_history(conn, user_id: int, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT date, total_calories, total_protein_g, total_carbs_g, total_fat_g
               FROM daily_logs WHERE user_id = %s
               ORDER BY date DESC LIMIT %s""",
            (user_id, days),
        )
        rows = cur.fetchall()
    return [
        {
            "date": r[0],
            "calories": r[1] or 0,
            "protein": r[2] or 0,
            "carbs": r[3] or 0,
            "fat": r[4] or 0,
        }
        for r in rows
    ]


# ---------- Summaries / recommendations ----------

def get_users_needing_summary(conn, summary_hour: int = 22) -> list[tuple[int, str]]:
    """Users due an end-of-day AI summary right now.

    Per-user timing: a user is in the cohort when their local clock falls
    inside the `summary_hour` (default 22) AND today's `daily_logs` row is
    not yet marked sent AND there's at least one meal logged for that
    user-local day.

    Designed for the hourly `cron_daily_summary` — each user matches for
    exactly one UTC hour per day (their local 22:00).
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT dl.user_id, dl.date
               FROM daily_logs dl
               JOIN user_profiles up ON up.user_id = dl.user_id
               WHERE up.onboarding_step = 'done'
                 AND up.blocked_at IS NULL
                 AND EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))::int = %s
                 AND dl.date = TO_CHAR(NOW() AT TIME ZONE up.tz, 'YYYY-MM-DD')
                 AND dl.summary_sent = 0
                 AND EXISTS (
                     SELECT 1 FROM meals m
                     WHERE m.user_id = dl.user_id AND m.date = dl.date
                 )""",
            (summary_hour,),
        )
        rows = cur.fetchall()
    return [(r[0], r[1]) for r in rows]


def save_recommendation(conn, user_id: int, date: str, text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO daily_recommendations (user_id, date, recommendation, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, date, text, _now_iso()),
        )
    conn.commit()


def get_latest_recommendation(conn, user_id: int) -> Optional[dict]:
    """Most recent end-of-day coach note for the user, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date, recommendation FROM daily_recommendations "
            "WHERE user_id = %s ORDER BY date DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"date": row[0], "recommendation": row[1]}


def mark_summary_sent(conn, user_id: int, date: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE daily_logs SET summary_sent = 1 WHERE user_id = %s AND date = %s",
            (user_id, date),
        )
    conn.commit()


def mark_all_previous_summaries_sent(conn) -> None:
    today = _today_str()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE daily_logs SET summary_sent = 1 WHERE date < %s AND summary_sent = 0",
            (today,),
        )
    conn.commit()


# ---------- Favorites + Recent ----------

def toggle_favorite(conn, meal_id: int, user_id: int) -> Optional[bool]:
    """Flip is_favorite for a meal. Returns new bool state, or None if not owned."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_favorite FROM meals WHERE id = %s AND user_id = %s",
            (meal_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        new_val = 0 if row[0] else 1
        cur.execute(
            "UPDATE meals SET is_favorite = %s WHERE id = %s AND user_id = %s",
            (new_val, meal_id, user_id),
        )
    conn.commit()
    return bool(new_val)


def set_favorite(conn, meal_id: int, user_id: int, is_fav: bool) -> bool:
    """Set favorite flag explicitly. Returns True if the meal existed."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE meals SET is_favorite = %s WHERE id = %s AND user_id = %s",
            (1 if is_fav else 0, meal_id, user_id),
        )
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def get_meal_by_id(conn, meal_id: int, user_id: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, meal_type, description, ingredients, allergen_warnings, crohn_warnings,
                      calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
                      photo_file_id, ai_raw_response, is_favorite, date, created_at
               FROM meals WHERE id = %s AND user_id = %s""",
            (meal_id, user_id),
        )
        r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "meal_type": r[1], "description": r[2],
        "ingredients": json.loads(r[3] or "[]"),
        "allergen_warnings": json.loads(r[4] or "[]"),
        "crohn_warnings": json.loads(r[5] or "[]"),
        "calories": r[6] or 0, "protein_g": r[7] or 0, "carbs_g": r[8] or 0,
        "fat_g": r[9] or 0, "fiber_g": r[10] or 0, "sugar_g": r[11] or 0,
        "photo_file_id": r[12], "ai_raw_response": r[13],
        "is_favorite": bool(r[14]), "date": r[15], "created_at": r[16],
    }


def get_recent_meals(conn, user_id: int, limit: int = 10) -> list[dict]:
    """Return up to `limit` most recent meals, deduplicated by lowercased description."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (LOWER(COALESCE(description, '')))
                      id, meal_type, description, calories, protein_g, carbs_g, fat_g,
                      is_favorite, created_at
               FROM meals
               WHERE user_id = %s AND description IS NOT NULL AND description != ''
               ORDER BY LOWER(COALESCE(description, '')), created_at DESC""",
            (user_id,),
        )
        rows = cur.fetchall()
    results = [
        {
            "id": r[0], "meal_type": r[1], "description": r[2],
            "calories": r[3] or 0, "protein_g": r[4] or 0,
            "carbs_g": r[5] or 0, "fat_g": r[6] or 0,
            "is_favorite": bool(r[7]), "created_at": r[8],
        }
        for r in rows
    ]
    results.sort(key=lambda m: m["created_at"] or "", reverse=True)
    return results[:limit]


def get_favorites(conn, user_id: int, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (LOWER(COALESCE(description, '')))
                      id, meal_type, description, calories, protein_g, carbs_g, fat_g,
                      is_favorite, created_at
               FROM meals
               WHERE user_id = %s AND is_favorite = 1
                 AND description IS NOT NULL AND description != ''
               ORDER BY LOWER(COALESCE(description, '')), created_at DESC""",
            (user_id,),
        )
        rows = cur.fetchall()
    results = [
        {
            "id": r[0], "meal_type": r[1], "description": r[2],
            "calories": r[3] or 0, "protein_g": r[4] or 0,
            "carbs_g": r[5] or 0, "fat_g": r[6] or 0,
            "is_favorite": True, "created_at": r[8],
        }
        for r in rows
    ]
    results.sort(key=lambda m: m["created_at"] or "", reverse=True)
    return results[:limit]


def clone_meal_for_today(conn, meal_id: int, user_id: int, meal_type: str) -> Optional[int]:
    """Copy an existing meal into today with a new meal_type. Returns new id."""
    src = get_meal_by_id(conn, meal_id, user_id)
    if not src:
        return None
    today = _today_str()
    now = _now_iso()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO meals (
                user_id, date, meal_type, description, ingredients,
                allergen_warnings, crohn_warnings,
                calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
                photo_file_id, ai_raw_response, is_favorite, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                user_id, today, meal_type, src["description"],
                json.dumps(src["ingredients"], ensure_ascii=False),
                json.dumps(src["allergen_warnings"], ensure_ascii=False),
                json.dumps(src["crohn_warnings"], ensure_ascii=False),
                src["calories"], src["protein_g"], src["carbs_g"],
                src["fat_g"], src["fiber_g"], src["sugar_g"],
                src["photo_file_id"], src["ai_raw_response"],
                0, now,
            ),
        )
        new_id = cur.fetchone()[0]
    conn.commit()
    recalc_daily_log(conn, user_id, today)
    return new_id


# ---------- Water tracking ----------

WATER_PRESETS = (200, 250, 300, 500, 750)


def _clamp_water_target(ml: int) -> int:
    return max(1500, min(4000, int(ml)))


def estimate_water_target_from_weight(weight_kg: float) -> int:
    """30 ml per kg, rounded to nearest 50 ml, clamped to [1500, 4000]."""
    raw = float(weight_kg) * 30
    rounded = int(round(raw / 50) * 50)
    return _clamp_water_target(rounded)


def get_water_target(conn, user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT target_ml FROM water_prefs WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 2000


def get_water_prefs(conn, user_id: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target_ml, target_overridden FROM water_prefs WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"target_ml": int(row[0]), "target_overridden": bool(row[1])}


def set_water_target(conn, user_id: int, target_ml: int, overridden: bool = True) -> None:
    clamped = _clamp_water_target(target_ml)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO water_prefs (user_id, target_ml, target_overridden, updated_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   target_ml = EXCLUDED.target_ml,
                   target_overridden = EXCLUDED.target_overridden,
                   updated_at = EXCLUDED.updated_at""",
            (user_id, clamped, 1 if overridden else 0, _now_iso()),
        )
    conn.commit()


def upsert_water_target_from_profile(conn, user_id: int, weight_kg: float) -> int:
    """Compute estimated target; upsert only if row missing or not user-overridden."""
    if not weight_kg:
        return get_water_target(conn, user_id)
    estimated = estimate_water_target_from_weight(weight_kg)
    prefs = get_water_prefs(conn, user_id)
    if prefs and prefs["target_overridden"]:
        return prefs["target_ml"]
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO water_prefs (user_id, target_ml, target_overridden, updated_at)
               VALUES (%s, %s, 0, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   target_ml = EXCLUDED.target_ml,
                   updated_at = EXCLUDED.updated_at
               WHERE water_prefs.target_overridden = 0""",
            (user_id, estimated, _now_iso()),
        )
    conn.commit()
    return estimated


def add_water(conn, user_id: int, amount_ml: int) -> int:
    """Insert a water log entry, return today's new total in ml."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO water_logs (user_id, amount_ml) VALUES (%s, %s)",
            (user_id, int(amount_ml)),
        )
    conn.commit()
    return get_water_today(conn, user_id)


def remove_last_water_today(conn, user_id: int) -> Optional[int]:
    """Delete the most recent water entry logged today (Kyiv). Return new total, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM water_logs
               WHERE id = (
                 SELECT id FROM water_logs
                 WHERE user_id = %s
                   AND (logged_at AT TIME ZONE 'Europe/Kyiv')::date
                       = (now() AT TIME ZONE 'Europe/Kyiv')::date
                 ORDER BY logged_at DESC LIMIT 1
               )""",
            (user_id,),
        )
        deleted = cur.rowcount > 0
    conn.commit()
    if not deleted:
        return None
    return get_water_today(conn, user_id)


def get_water_today(conn, user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(amount_ml), 0) FROM water_logs
               WHERE user_id = %s
                 AND (logged_at AT TIME ZONE 'Europe/Kyiv')::date
                     = (now() AT TIME ZONE 'Europe/Kyiv')::date""",
            (user_id,),
        )
        row = cur.fetchone()
    return int(row[0] or 0)


def get_water_for_date(conn, user_id: int, date_str: str) -> int:
    """Total water (ml) for a specific Kyiv-local calendar date (YYYY-MM-DD)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(amount_ml), 0) FROM water_logs
               WHERE user_id = %s
                 AND (logged_at AT TIME ZONE 'Europe/Kyiv')::date = %s::date""",
            (user_id, date_str),
        )
        row = cur.fetchone()
    return int(row[0] or 0)


# ---------- Weight history + weekly check-in ----------

def insert_weight(conn, user_id: int, weight_kg: float, source: str = "manual") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO weight_history (user_id, weight_kg, source) VALUES (%s, %s, %s)",
            (user_id, float(weight_kg), source),
        )
    conn.commit()


def get_weight_history(conn, user_id: int, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT weight_kg, source, recorded_at FROM weight_history
               WHERE user_id = %s
               ORDER BY recorded_at DESC LIMIT %s""",
            (user_id, int(limit)),
        )
        rows = cur.fetchall()
    return [{"weight_kg": r[0], "source": r[1], "recorded_at": r[2]} for r in rows]


def set_awaiting_input(conn, user_id: int, input_type: Optional[str]) -> None:
    """Flag the user as awaiting a specific free-text input. None clears it."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET awaiting_input_type = %s, updated_at = %s "
            "WHERE user_id = %s",
            (input_type, _now_iso(), user_id),
        )
    conn.commit()


def mark_weekly_checkin_sent(conn, user_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET weekly_checkin_sent_at = %s, updated_at = %s "
            "WHERE user_id = %s",
            (_now_iso(), _now_iso(), user_id),
        )
    conn.commit()


def get_users_due_weekly_checkin(conn, min_days_since_last: int = 6) -> list[int]:
    """Onboarded users who haven't received a weekly weight check-in in N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_days_since_last)).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT user_id FROM user_profiles
               WHERE onboarding_step = 'done'
                 AND blocked_at IS NULL
                 AND (weekly_checkin_sent_at IS NULL
                      OR weekly_checkin_sent_at < %s)""",
            (cutoff,),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]


def get_users_to_nudge(conn, summary_hour: int = 22) -> list[dict]:
    """Onboarded, opted-in users due a daily zero-meal nudge right now.

    Per-user timing: a user is in the cohort when their local clock is in
    the `summary_hour` (default 22) AND they have zero meals on their local
    `today` AND `last_nudge_sent_at` is older than the start of that local
    day (one nudge per user-local calendar day, max).

    Designed for the hourly `cron_daily_summary` — each user matches for
    exactly one UTC hour per day (their local 22:00). The dedup gate
    prevents repeat sends if the cron retries / fires twice in the hour.

    Returns a list of `{user_id, lang}` dicts.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang
            FROM user_profiles up
            WHERE up.onboarding_step = 'done'
              AND up.daily_calorie_target IS NOT NULL
              AND COALESCE(up.nudge_optout, 0) = 0
              AND up.blocked_at IS NULL
              AND EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))::int = %s
              AND NOT EXISTS (
                  SELECT 1 FROM meals m
                  WHERE m.user_id = up.user_id
                    AND m.date = TO_CHAR(NOW() AT TIME ZONE up.tz, 'YYYY-MM-DD')
              )
              AND (
                  up.last_nudge_sent_at IS NULL
                  OR (up.last_nudge_sent_at::timestamptz AT TIME ZONE up.tz)::date
                     < (NOW() AT TIME ZONE up.tz)::date
              )
            ORDER BY up.user_id
            """,
            (summary_hour,),
        )
        rows = cur.fetchall()
    return [{"user_id": r[0], "lang": r[1]} for r in rows]


def mark_nudge_sent(conn, user_id: int) -> None:
    """Stamp `last_nudge_sent_at = now()` to start the stale-tier cooldown."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET last_nudge_sent_at = %s, updated_at = %s "
            "WHERE user_id = %s",
            (_now_iso(), _now_iso(), user_id),
        )
    conn.commit()


def set_nudge_optout(conn, user_id: int, optout: bool) -> None:
    """Toggle whether the user receives inactivity nudges."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET nudge_optout = %s, updated_at = %s "
            "WHERE user_id = %s",
            (1 if optout else 0, _now_iso(), user_id),
        )
    conn.commit()


def set_blocked(conn, user_id: int, blocked: bool) -> None:
    """Stamp / clear `blocked_at` when Telegram tells us the user blocked
    the bot (or when they message us again, indicating unblock).

    Distinct from `nudge_optout`: nudge_optout means the user explicitly
    muted via `/quiet` or the legacy 🔕 button; blocked_at means Telegram
    returned 400/403 on a send attempt. Both gate all notification cohorts.
    """
    val = _now_iso() if blocked else None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET blocked_at = %s, updated_at = %s "
            "WHERE user_id = %s",
            (val, _now_iso(), user_id),
        )
    conn.commit()


def get_users_due_morning_greeting(conn, morning_hour: int = 8) -> list[dict]:
    """Users due the daily morning greeting right now.

    Per-user timing: a user matches when their local clock is in
    `morning_hour` (default 8) AND `last_morning_sent_at` is older than
    user-local today. Opt-outs (`nudge_optout=1` or `blocked_at IS NOT
    NULL`) excluded. Onboarding-incomplete users ARE included — they
    get a "come back and finish" variant from the caller.

    Designed for an hourly cron firing at :30 UTC; each user matches
    exactly one UTC fire per day (their local 8:30).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang,
                   up.onboarding_step
            FROM user_profiles up
            WHERE COALESCE(up.nudge_optout, 0) = 0
              AND up.blocked_at IS NULL
              AND EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))::int = %s
              AND (
                  up.last_morning_sent_at IS NULL
                  OR (up.last_morning_sent_at::timestamptz AT TIME ZONE up.tz)::date
                     < (NOW() AT TIME ZONE up.tz)::date
              )
            ORDER BY up.user_id
            """,
            (morning_hour,),
        )
        rows = cur.fetchall()
    return [{"user_id": r[0], "lang": r[1], "onboarding_step": r[2]}
            for r in rows]


# ---------- F-17: never-logger activation funnel ----------
#
# State machine on `user_profiles.activation_step`:
#
#     NULL / ''         day 0–1     no message sent yet
#     'demo'            day 2+      first-meal demo card sent
#     'd4_followup'     day 4+      lighter "still no log?" sent
#     'd7_final'        day 7+      final softer "no pressure" sent
#     'auto_quieted'    day 9+      nudge_optout=1, no more pings
#
# Each `get_users_for_<step>` helper returns the cohort eligible to
# *transition into* that state on the next morning cron fire. All of
# them gate on local-hour=8 + dedup against `last_morning_sent_at`, so
# integrating into `cron_good_morning` is just a branch above the
# existing greeting logic.
#
# Critical safety: every cohort gate filters `NOT EXISTS (... FROM
# meals)`. The moment a user logs their first meal anywhere, they drop
# out of every funnel cohort. Engaged users are never touched.


def _activation_base_filters() -> str:
    """The shared WHERE clauses used by every activation-funnel cohort:
    profile complete + opt-out gates + per-user-tz morning hour + dedup
    against today's morning send + lifetime zero meals.

    Returned as a SQL fragment for in-place interpolation. Trusted-input
    only — no user-supplied values land here."""
    return """
        up.onboarding_step = 'done'
        AND COALESCE(up.nudge_optout, 0) = 0
        AND up.blocked_at IS NULL
        AND EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))::int = %s
        AND (
            up.last_morning_sent_at IS NULL
            OR (up.last_morning_sent_at::timestamptz AT TIME ZONE up.tz)::date
               < (NOW() AT TIME ZONE up.tz)::date
        )
        AND NOT EXISTS (
            SELECT 1 FROM meals m WHERE m.user_id = up.user_id
        )
    """


def get_users_for_first_meal_demo(conn, morning_hour: int = 8) -> list[dict]:
    """Day 2+ never-loggers due the activation demo card.

    Cohort: profile done, 0 lifetime meals, signed up ≥24h ago,
    `activation_step` not yet set. Returns one dict per user with the
    fields the caller needs to render the personalised demo:
    `{user_id, lang, first_name, daily_calorie_target}`.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang,
                   COALESCE(u.first_name, '') AS first_name,
                   COALESCE(up.daily_calorie_target, 2000) AS cal
            FROM user_profiles up
            JOIN users u ON u.user_id = up.user_id
            WHERE {_activation_base_filters()}
              AND COALESCE(up.activation_step, '') = ''
              AND u.created_at IS NOT NULL
              AND (NOW() AT TIME ZONE 'UTC') - u.created_at::timestamptz
                  >= INTERVAL '24 hours'
            ORDER BY up.user_id
            """,
            (morning_hour,),
        )
        rows = cur.fetchall()
    return [
        {"user_id": r[0], "lang": r[1], "first_name": r[2], "cal": int(r[3])}
        for r in rows
    ]


def get_users_for_d4_followup(conn, morning_hour: int = 8) -> list[dict]:
    """Day 4+ never-loggers who got the demo but still haven't logged.

    Cohort: same base filters, plus `activation_step = 'demo'` and
    signed up ≥3 days ago. Sent the lighter "still no log?" follow-up.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang,
                   COALESCE(u.first_name, '') AS first_name
            FROM user_profiles up
            JOIN users u ON u.user_id = up.user_id
            WHERE {_activation_base_filters()}
              AND up.activation_step = 'demo'
              AND u.created_at IS NOT NULL
              AND (NOW() AT TIME ZONE 'UTC') - u.created_at::timestamptz
                  >= INTERVAL '3 days'
            ORDER BY up.user_id
            """,
            (morning_hour,),
        )
        rows = cur.fetchall()
    return [{"user_id": r[0], "lang": r[1], "first_name": r[2]} for r in rows]


def get_users_for_d7_final(conn, morning_hour: int = 8) -> list[dict]:
    """Day 7+ never-loggers who got the follow-up but still haven't logged.

    Final softer message before the day-9 auto-quiet sweep.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang,
                   COALESCE(u.first_name, '') AS first_name
            FROM user_profiles up
            JOIN users u ON u.user_id = up.user_id
            WHERE {_activation_base_filters()}
              AND up.activation_step = 'd4_followup'
              AND u.created_at IS NOT NULL
              AND (NOW() AT TIME ZONE 'UTC') - u.created_at::timestamptz
                  >= INTERVAL '6 days'
            ORDER BY up.user_id
            """,
            (morning_hour,),
        )
        rows = cur.fetchall()
    return [{"user_id": r[0], "lang": r[1], "first_name": r[2]} for r in rows]


def get_users_to_auto_quiet(conn, days: int = 9) -> list[dict]:
    """Never-loggers who signed up ≥`days` days ago and still haven't
    logged a meal. Called from the midnight cron — silences them via
    `nudge_optout=1` so they don't keep getting pinged forever.

    Does NOT gate on `activation_step` — even if the morning cron never
    fired (missed Vercel delivery, etc.), 0 meals on day 9 is the
    universal signal to stop. Returns `[{user_id, lang}]` so the caller
    can send a one-line "no more pings" notice.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang
            FROM user_profiles up
            JOIN users u ON u.user_id = up.user_id
            WHERE up.onboarding_step = 'done'
              AND COALESCE(up.nudge_optout, 0) = 0
              AND up.blocked_at IS NULL
              AND COALESCE(up.activation_step, '') != 'auto_quieted'
              AND u.created_at IS NOT NULL
              AND (NOW() AT TIME ZONE 'UTC') - u.created_at::timestamptz
                  >= INTERVAL '{int(days)} days'
              AND NOT EXISTS (
                  SELECT 1 FROM meals m WHERE m.user_id = up.user_id
              )
            ORDER BY up.user_id
            """
        )
        rows = cur.fetchall()
    return [{"user_id": r[0], "lang": r[1]} for r in rows]


def set_activation_step(conn, user_id: int, step: str) -> None:
    """Advance the F-17 activation-funnel state machine for a user.

    Valid steps: 'demo' / 'd4_followup' / 'd7_final' / 'auto_quieted'.
    Caller decides which step to advance to based on the cohort it
    fetched the user from.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET activation_step = %s, updated_at = %s "
            "WHERE user_id = %s",
            (step, _now_iso(), user_id),
        )
    conn.commit()


def mark_morning_sent(conn, user_id: int) -> None:
    """Stamp `last_morning_sent_at = now()` so today's greeting is gated."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET last_morning_sent_at = %s, updated_at = %s "
            "WHERE user_id = %s",
            (_now_iso(), _now_iso(), user_id),
        )
    conn.commit()


def finalize_stuck_tz_users(conn, max_age_hours: int = 12) -> list[dict]:
    """Find users stranded on `awaiting_tz` or `awaiting_tz_custom` for
    longer than `max_age_hours` and force-finalize them. `tz` is left at
    the existing value (defaults to `Europe/Kyiv` via the schema).

    Returns a list of `{user_id, lang}` dicts so the cron caller can send
    each freed user a "we set you to Kyiv by default" notice.

    Called from the midnight reset cron — addresses the
    `awaiting_tz_custom` bottleneck where a user taps "Other zone" but
    never types an IANA name, leaving them stranded.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT up.user_id, COALESCE(up.lang, 'en') AS lang
               FROM user_profiles up
               WHERE up.onboarding_step IN ('awaiting_tz', 'awaiting_tz_custom')
                 AND up.updated_at < %s""",
            (cutoff,),
        )
        stuck = cur.fetchall()
        if stuck:
            cur.execute(
                """UPDATE user_profiles
                   SET onboarding_step = 'done', updated_at = %s
                   WHERE user_id = ANY(%s)""",
                (_now_iso(), [r[0] for r in stuck]),
            )
        conn.commit()
    return [{"user_id": r[0], "lang": r[1]} for r in stuck]


# ---------- Usage quota (per-user daily rate limit) ----------

def consume_quota(conn, user_id: int, action: str) -> int:
    """Atomically increment today's quota counter for (user_id, action) and
    return the new total. Caller compares against the per-action limit."""
    today = _today_str()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO usage_quota (user_id, action, day, count)
               VALUES (%s, %s, %s, 1)
               ON CONFLICT (user_id, action, day) DO UPDATE
                   SET count = usage_quota.count + 1
               RETURNING count""",
            (user_id, action, today),
        )
        new_count = cur.fetchone()[0]
    conn.commit()
    return int(new_count)


def cleanup_old_quotas(conn, keep_days: int = 7) -> None:
    """Delete quota rows older than `keep_days` days. Called from midnight cron."""
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=keep_days)).isoformat()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM usage_quota WHERE day < %s", (cutoff,))
    conn.commit()


# ---------- Health profile (F-1) ----------

def get_health_profile(conn, user_id: int) -> Optional[dict]:
    """Return ``{allergens: [...], conditions: [...], notes, updated_at}`` or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT allergens, conditions, notes, updated_at "
            "FROM user_health_profile WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "allergens":  list(row[0] or []),
        "conditions": list(row[1] or []),
        "notes":      row[2] or "",
        "updated_at": row[3],
    }


def set_health_allergens(conn, user_id: int, allergens: list[str]) -> None:
    """Replace the user's allergen list. Empty list = clear."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO user_health_profile (user_id, allergens, conditions, notes, updated_at)
               VALUES (%s, %s, '{}', '', %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   allergens = EXCLUDED.allergens,
                   updated_at = EXCLUDED.updated_at""",
            (user_id, allergens, _now_iso()),
        )
    conn.commit()


def set_health_conditions(conn, user_id: int, conditions: list[str]) -> None:
    """Replace the user's chronic-condition list. Empty list = clear."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO user_health_profile (user_id, allergens, conditions, notes, updated_at)
               VALUES (%s, '{}', %s, '', %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   conditions = EXCLUDED.conditions,
                   updated_at = EXCLUDED.updated_at""",
            (user_id, conditions, _now_iso()),
        )
    conn.commit()


def clear_health_profile(conn, user_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_health_profile WHERE user_id = %s", (user_id,))
    conn.commit()


def get_history_range(conn, user_id: int, start_date: str, end_date: str) -> list[dict]:
    """Daily log aggregates + meal_count for each date in [start, end] (inclusive), ASC."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT dl.date, dl.total_calories, dl.total_protein_g,
                      dl.total_carbs_g, dl.total_fat_g,
                      (SELECT COUNT(*) FROM meals m
                       WHERE m.user_id = dl.user_id AND m.date = dl.date) AS mc
               FROM daily_logs dl
               WHERE dl.user_id = %s AND dl.date BETWEEN %s AND %s
               ORDER BY dl.date ASC""",
            (user_id, start_date, end_date),
        )
        rows = cur.fetchall()
    return [
        {
            "date": str(r[0]),
            "calories": r[1] or 0,
            "protein": r[2] or 0,
            "carbs": r[3] or 0,
            "fat": r[4] or 0,
            "meal_count": r[5] or 0,
        }
        for r in rows
    ]


def get_adherence_stats(conn, user_id: int) -> dict:
    """All-time per-macro averages, ``avg_pct`` of goal, and current streak.

    ``avg_pct`` is the average daily total expressed as a percentage of the
    user's target — sub-100 means undershooting on average, over-100 means
    overshooting. We deliberately do NOT cap at 100: hiding overshoot would
    erase the most actionable signal (e.g. a `lose` user pulling 130% on
    fat). The dashboard caps the visual bar fill but keeps the real value
    in the numeric label.

    Counted across days with ``meal_count > 0`` (a logged day).
    """
    from datetime import date as _date, timedelta as _td
    from lib.config import (
        macro_gram_targets,
        macro_gram_targets_from_profile,
    )

    profile = get_profile(conn, user_id) or {}
    cal_target = int(profile.get("daily_calorie_target") or 2000)
    weight = profile.get("weight_kg")
    goal = profile.get("goal")
    if weight and goal:
        macros = macro_gram_targets_from_profile(float(weight), goal)
    else:
        macros = macro_gram_targets(cal_target)
    p_target = macros.get("protein") or 1
    c_target = macros.get("carbs") or 1
    f_target = macros.get("fat") or 1

    with conn.cursor() as cur:
        cur.execute(
            """SELECT dl.date, dl.total_calories, dl.total_protein_g,
                      dl.total_carbs_g, dl.total_fat_g,
                      (SELECT COUNT(*) FROM meals m
                       WHERE m.user_id = dl.user_id AND m.date = dl.date) AS mc
               FROM daily_logs dl
               WHERE dl.user_id = %s
               ORDER BY dl.date DESC""",
            (user_id,),
        )
        rows = cur.fetchall()

    logged = [r for r in rows if (r[5] or 0) > 0]
    n = len(logged)
    if n == 0:
        return {
            "logged_days": 0,
            "avg_calories": None, "avg_protein_g": None,
            "avg_carbs_g": None, "avg_fat_g": None,
            "calories_avg_pct": None, "protein_avg_pct": None,
            "carbs_avg_pct": None, "fat_avg_pct": None,
            "current_streak": 0,
            "cal_target": cal_target,
            "p_target": p_target, "c_target": c_target, "f_target": f_target,
        }

    def _avg(col_idx):
        return round(sum((r[col_idx] or 0) for r in logged) / n, 1)

    def _avg_pct(col_idx, target):
        if not target:
            return None
        avg = sum((r[col_idx] or 0) for r in logged) / n
        return round(avg / target * 100)

    logged_dates = {str(r[0]) for r in logged}
    today_str = _today_str()
    d = _date.fromisoformat(today_str)
    if str(d) not in logged_dates:
        d = d - _td(days=1)
    streak = 0
    while str(d) in logged_dates:
        streak += 1
        d = d - _td(days=1)

    return {
        "logged_days": n,
        "avg_calories": _avg(1),
        "avg_protein_g": _avg(2),
        "avg_carbs_g": _avg(3),
        "avg_fat_g": _avg(4),
        "calories_avg_pct": _avg_pct(1, cal_target),
        "protein_avg_pct":  _avg_pct(2, p_target),
        "carbs_avg_pct":    _avg_pct(3, c_target),
        "fat_avg_pct":      _avg_pct(4, f_target),
        "current_streak": streak,
        "cal_target": cal_target,
        "p_target": p_target,
        "c_target": c_target,
        "f_target": f_target,
    }


# ---------- Engagement streaks (F-4) ----------

def get_streak(conn, user_id: int) -> Optional[dict]:
    """Return the user's streak row or None if they've never logged a meal.

    Shape: ``{current_streak, longest_streak, last_log_date, freeze_days_remaining, updated_at}``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_streak, longest_streak, last_log_date, "
            "freeze_days_remaining, updated_at "
            "FROM user_streaks WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "current_streak":          int(row[0] or 0),
        "longest_streak":          int(row[1] or 0),
        "last_log_date":           row[2],
        "freeze_days_remaining":   int(row[3] or 0),
        "updated_at":              row[4],
    }


def update_streak_for_meal(conn, user_id: int, today_local: str) -> dict:
    """Apply F-4 streak transition for an accepted meal.

    ``today_local`` is the user's local date as ``YYYY-MM-DD`` (use
    ``lib.datehelpers.today_str_user(profile)`` at the call site).

    Logic:
      - No row exists → INSERT with current=1, longest=1, freezes=3, last=today_local
      - gap_days = (today_local - last_log_date)
        - gap <= 0  → no-op (idempotent on multiple meals same day)
        - gap == 1  → current += 1, last = today_local
        - gap == 2 and freezes > 0 → freezes -= 1, last = today_local (streak unchanged)
        - else      → current = 1, last = today_local (broken streak)
      - Always: longest = max(longest, current); updated_at = now_iso

    Returns the post-update streak row (same shape as ``get_streak``).
    """
    from datetime import date as _date

    def _parse_date(s: Optional[str]) -> Optional[_date]:
        if not s:
            return None
        try:
            return _date.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    today_d = _parse_date(today_local)
    if today_d is None:
        # Caller passed garbage — bail out without touching state.
        existing = get_streak(conn, user_id)
        if existing is not None:
            return existing
        return {
            "current_streak": 0,
            "longest_streak": 0,
            "last_log_date": None,
            "freeze_days_remaining": 3,
            "updated_at": None,
        }

    now = _now_iso()
    existing = get_streak(conn, user_id)

    if existing is None:
        # First meal ever — seed the row.
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_streaks
                   (user_id, current_streak, longest_streak, last_log_date,
                    freeze_days_remaining, updated_at)
                   VALUES (%s, 1, 1, %s, 3, %s)""",
                (user_id, today_local, now),
            )
        conn.commit()
        return {
            "current_streak": 1,
            "longest_streak": 1,
            "last_log_date": today_local,
            "freeze_days_remaining": 3,
            "updated_at": now,
        }

    last_d = _parse_date(existing["last_log_date"])
    current = existing["current_streak"]
    longest = existing["longest_streak"]
    freezes = existing["freeze_days_remaining"]

    if last_d is None:
        # Row exists but last_log_date is null/garbage — treat as fresh start.
        gap = None
    else:
        gap = (today_d - last_d).days

    if gap is not None and gap <= 0:
        # Same day or earlier (clock skew) — no-op.
        return existing

    if gap == 1:
        current = current + 1
    elif gap == 2 and freezes > 0:
        freezes = freezes - 1
        # current unchanged
    else:
        # gap is None / >=3 / (gap==2 with no freezes) → streak resets
        current = 1

    longest = max(longest, current)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE user_streaks SET
                   current_streak = %s,
                   longest_streak = %s,
                   last_log_date = %s,
                   freeze_days_remaining = %s,
                   updated_at = %s
               WHERE user_id = %s""",
            (current, longest, today_local, freezes, now, user_id),
        )
    conn.commit()

    return {
        "current_streak": current,
        "longest_streak": longest,
        "last_log_date": today_local,
        "freeze_days_remaining": freezes,
        "updated_at": now,
    }


def reset_monthly_freezes(conn) -> int:
    """Restore ``freeze_days_remaining`` to 3 for every existing streak row.

    Called by the daily UTC midnight cron when ``date.today().day == 1``.
    Returns the number of rows updated.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_streaks SET freeze_days_remaining = 3, updated_at = %s",
            (_now_iso(),),
        )
        n = cur.rowcount
    conn.commit()
    return int(n or 0)


# ---------- Menu OCR results (F-9) ----------

def save_menu_ocr_result(conn, user_id: int, dishes: list[dict]) -> None:
    """Persist a fresh menu-OCR result, replacing any previous one.

    One row per user. The midnight cron prunes rows > 1h old so abandoned
    menus don't accumulate.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO menu_ocr_results (user_id, dishes_json, created_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                   dishes_json = EXCLUDED.dishes_json,
                   created_at  = EXCLUDED.created_at""",
            (user_id, json.dumps(dishes, ensure_ascii=False), _now_iso()),
        )
    conn.commit()


def get_menu_ocr_result(conn, user_id: int) -> Optional[list[dict]]:
    """Return the user's most recent menu-OCR dishes list, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dishes_json FROM menu_ocr_results WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return data


def cleanup_old_menu_ocr_results(conn, max_age_hours: int = 1) -> None:
    """Drop menu OCR rows older than ``max_age_hours``. Run from midnight cron."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM menu_ocr_results WHERE created_at < %s", (cutoff,))
    conn.commit()


# ---------- Meal plans (F-10) ----------

def save_meal_plan(conn, user_id: int, plan: dict) -> int:
    """Persist a 3-day meal plan dict. Returns the new row id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meal_plans (user_id, plan_json, created_at) "
            "VALUES (%s, %s, %s) RETURNING id",
            (user_id, json.dumps(plan, ensure_ascii=False), _now_iso()),
        )
        plan_id = cur.fetchone()[0]
    conn.commit()
    return int(plan_id)


def get_meal_plan(conn, plan_id: int, user_id: int) -> Optional[dict]:
    """Return a saved meal plan if it belongs to the user. None otherwise.

    Scoping by user_id keeps a malicious / stale callback from leaking
    another user's plan.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT plan_json FROM meal_plans WHERE id = %s AND user_id = %s",
            (plan_id, user_id),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def get_latest_meal_plan(conn, user_id: int) -> Optional[tuple[int, dict]]:
    """Return ``(id, plan_dict)`` for the user's most recent plan, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, plan_json FROM meal_plans WHERE user_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    try:
        return int(row[0]), json.loads(row[1])
    except (TypeError, ValueError):
        return None


def cleanup_old_meal_plans(conn, max_age_days: int = 90) -> None:
    """Drop meal plans older than ``max_age_days``. Run from midnight cron."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM meal_plans WHERE created_at < %s", (cutoff,))
    conn.commit()


def get_meals_in_range(
    conn,
    user_id: int,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """All meals for ``user_id`` between ``start_date`` and ``end_date`` (inclusive).

    Used by F-12 weekly recap aggregation. Dates are 'YYYY-MM-DD' strings
    (matching how ``meals.date`` is stored). Returns oldest-first.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT date, meal_type, description, calories, protein_g, carbs_g, fat_g, created_at
               FROM meals
               WHERE user_id = %s AND date >= %s AND date <= %s
               ORDER BY date ASC, id ASC""",
            (user_id, start_date, end_date),
        )
        rows = cur.fetchall()
    return [{
        "date":         r[0],
        "meal_type":    r[1],
        "description":  r[2],
        "calories":     r[3] or 0,
        "protein_g":    r[4] or 0,
        "carbs_g":      r[5] or 0,
        "fat_g":        r[6] or 0,
        "created_at":   r[7],
    } for r in rows]


# ---------- F-14: Admin analytics helpers ----------

def get_retention_cohorts(conn, weeks: int = 12) -> list[dict]:
    """D1 / D7 / D30 retention by signup week, last `weeks` weeks.

    Returns rows with `cohort_week` (date), `size`, `d1`, `d7`, `d30` counts.
    A user is "retained at day N" iff they logged a meal whose `created_at`
    falls within [signup_date + N-1d, signup_date + N+1d] window for D7/D30
    (1-day slack absorbs timezone noise), or +24h for D1.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH cohorts AS (
              SELECT u.user_id,
                     DATE_TRUNC('week', u.created_at::timestamp) AS cohort_week,
                     u.created_at::timestamp AS signup_at
              FROM users u
              WHERE u.created_at IS NOT NULL
                AND u.created_at::timestamp > NOW() - INTERVAL '{int(weeks)} weeks'
            )
            SELECT
              c.cohort_week,
              COUNT(DISTINCT c.user_id) AS size,
              COUNT(DISTINCT c.user_id) FILTER (WHERE EXISTS (
                SELECT 1 FROM meals m
                WHERE m.user_id = c.user_id
                  AND m.created_at IS NOT NULL
                  AND m.created_at::timestamp BETWEEN c.signup_at + INTERVAL '0 days'
                                                  AND c.signup_at + INTERVAL '2 days'
              )) AS d1,
              COUNT(DISTINCT c.user_id) FILTER (WHERE EXISTS (
                SELECT 1 FROM meals m
                WHERE m.user_id = c.user_id
                  AND m.created_at IS NOT NULL
                  AND m.created_at::timestamp BETWEEN c.signup_at + INTERVAL '6 days'
                                                  AND c.signup_at + INTERVAL '8 days'
              )) AS d7,
              COUNT(DISTINCT c.user_id) FILTER (WHERE EXISTS (
                SELECT 1 FROM meals m
                WHERE m.user_id = c.user_id
                  AND m.created_at IS NOT NULL
                  AND m.created_at::timestamp BETWEEN c.signup_at + INTERVAL '29 days'
                                                  AND c.signup_at + INTERVAL '31 days'
              )) AS d30
            FROM cohorts c
            GROUP BY c.cohort_week
            ORDER BY c.cohort_week DESC
            """
        )
        rows = cur.fetchall()
    return [
        {"cohort_week": r[0], "size": r[1], "d1": r[2], "d7": r[3], "d30": r[4]}
        for r in rows
    ]


def get_daily_trends(conn, days: int = 30) -> list[dict]:
    """Daily totals for the last `days` days (inclusive of today).

    Returns rows with `day` (date), `new_users`, `dau` (distinct loggers),
    `meals` (total meals logged that day). Dense — every day in range is
    present even if zero.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH days AS (
              SELECT generate_series(CURRENT_DATE - {int(days) - 1},
                                     CURRENT_DATE, '1 day')::date AS d
            )
            SELECT
              d.d,
              (SELECT COUNT(*) FROM users u
                WHERE u.created_at IS NOT NULL
                  AND u.created_at::date = d.d) AS new_users,
              (SELECT COUNT(DISTINCT m.user_id) FROM meals m
                WHERE m.created_at IS NOT NULL
                  AND m.created_at::date = d.d) AS dau,
              (SELECT COUNT(*) FROM meals m
                WHERE m.created_at IS NOT NULL
                  AND m.created_at::date = d.d) AS meals
            FROM days d
            ORDER BY d.d
            """
        )
        rows = cur.fetchall()
    return [
        {"day": r[0], "new_users": r[1], "dau": r[2], "meals": r[3]}
        for r in rows
    ]


# Canonical onboarding-step order. Used to render a left-to-right funnel that
# shows how many users currently sit at each step (and so how many dropped off
# between each pair). Mirrors the FSM in `api/webhook.py`.
ONBOARDING_STEPS = (
    "awaiting_lang_confirm",
    "awaiting_age",
    "awaiting_sex",
    "awaiting_weight",
    "awaiting_height",
    "awaiting_gym",
    "awaiting_goal",
    "awaiting_target_weight",
    "awaiting_confirm",
    "awaiting_tz",
    "awaiting_tz_custom",
    "awaiting_custom_cal",
    "done",
)


def get_onboarding_funnel(conn) -> list[tuple[str, int]]:
    """Count of users currently at each `onboarding_step`, in canonical order.

    Returns a list of `(step_name, count)` tuples covering every step in
    `ONBOARDING_STEPS` (zero for any step with no users), so the admin
    funnel always renders a stable left-to-right shape.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT onboarding_step, COUNT(*) FROM user_profiles GROUP BY onboarding_step"
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
    return [(step, int(counts.get(step, 0))) for step in ONBOARDING_STEPS]


def get_user_breakdowns(conn) -> dict[str, list[tuple[str, int]]]:
    """Distribution of users across lang / tz / sex / goal / source for
    Overview cards.

    Returns `{dim: [(value, count), …]}` sorted by count desc. Empty strings
    and NULLs collapse to '—'. `source` lives on the `users` table (F-15
    attribution); the four profile dims live on `user_profiles`.
    """
    out: dict[str, list[tuple[str, int]]] = {}
    with conn.cursor() as cur:
        for dim in ("lang", "tz", "sex", "goal"):
            cur.execute(
                f"""
                SELECT COALESCE(NULLIF({dim}, ''), '—') AS v, COUNT(*) AS c
                FROM user_profiles
                GROUP BY v
                ORDER BY c DESC
                """
            )
            out[dim] = [(r[0], int(r[1])) for r in cur.fetchall()]
        # source lives on `users`, not `user_profiles`. Empty source
        # collapses to the human-friendly label 'organic'.
        cur.execute(
            """
            SELECT COALESCE(NULLIF(source, ''), 'organic') AS v, COUNT(*) AS c
            FROM users
            GROUP BY v
            ORDER BY c DESC
            """
        )
        out["source"] = [(r[0], int(r[1])) for r in cur.fetchall()]
        # F-16 status: derived per-user state. Precedence blocked > quiet
        # > active so a user with both flags counts only as blocked.
        cur.execute(
            """
            SELECT
                CASE
                    WHEN blocked_at IS NOT NULL          THEN 'blocked'
                    WHEN COALESCE(nudge_optout, 0) = 1   THEN 'quiet'
                    ELSE                                      'active'
                END AS v,
                COUNT(*) AS c
            FROM user_profiles
            GROUP BY v
            ORDER BY c DESC
            """
        )
        out["status"] = [(r[0], int(r[1])) for r in cur.fetchall()]
    return out


def record_cron_run(conn, cron_name: str, status: str,
                    result: Optional[dict] = None, error: Optional[str] = None) -> None:
    """Append one row to `cron_runs` describing this invocation.

    Called from each cron's `finally:` block so the admin dashboard can show
    the latest run timestamp + status + result counts per cron. Best-effort —
    callers should swallow exceptions from this so cron-status logging never
    masks the real run outcome.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cron_runs (cron_name, finished_at, status, result_json, error)
            VALUES (%s, now(), %s, %s, %s)
            """,
            (cron_name, status, json.dumps(result) if result is not None else None, error),
        )
    conn.commit()


def get_user_activity_30d(conn, days: int = 30) -> dict[int, list[int]]:
    """For each user with logs in the last `days`, a dense array of per-day
    meal counts oriented oldest-first.

    Returns `{user_id: [count_d-(days-1), …, count_d0]}` so callers can render
    inline sparklines without further bookkeeping. Users with no recent
    activity are simply absent from the dict (caller draws nothing).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT user_id,
                   (CURRENT_DATE - m.day) AS days_ago,
                   m.cnt
            FROM (
              SELECT user_id, created_at::date AS day, COUNT(*) AS cnt
              FROM meals
              WHERE created_at IS NOT NULL
                AND created_at::date > CURRENT_DATE - {int(days)}
              GROUP BY user_id, created_at::date
            ) m
            """
        )
        rows = cur.fetchall()
    out: dict[int, list[int]] = {}
    for uid, days_ago, cnt in rows:
        arr = out.setdefault(uid, [0] * days)
        idx = (days - 1) - int(days_ago)
        if 0 <= idx < days:
            arr[idx] = int(cnt)
    return out


def get_attribution_breakdown(conn) -> list[dict]:
    """F-15 attribution drill-down for the admin Analytics tab.

    Returns one dict per distinct `source` value with quality metrics:
    `{source, total, uk_count, en_count, done_count, logged_count}`.
    Caller derives onboarding-completion % and first-meal % from those
    counts. Empty `source` (organic) collapses to the literal 'organic'.
    Ordered by total descending so the busiest acquisition surfaces
    surface first; ties broken alphabetically for stable rendering.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COALESCE(NULLIF(u.source, ''), 'organic') AS source,
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE p.lang = 'uk') AS uk_count,
              COUNT(*) FILTER (WHERE p.lang = 'en') AS en_count,
              COUNT(*) FILTER (WHERE p.onboarding_step = 'done') AS done_count,
              COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM meals m WHERE m.user_id = u.user_id
              )) AS logged_count
            FROM users u
            LEFT JOIN user_profiles p ON p.user_id = u.user_id
            GROUP BY 1
            ORDER BY total DESC, source
            """
        )
        rows = cur.fetchall()
    return [
        {
            "source":       r[0],
            "total":        int(r[1] or 0),
            "uk_count":     int(r[2] or 0),
            "en_count":     int(r[3] or 0),
            "done_count":   int(r[4] or 0),
            "logged_count": int(r[5] or 0),
        }
        for r in rows
    ]


def get_nudge_effectiveness(conn, days: int = 30) -> dict:
    """Crude conversion: of users with `last_nudge_sent_at` in the last `days`,
    how many logged a meal within 24h after that stamp?

    Important caveat: `last_nudge_sent_at` is overwritten on each nudge, so
    this only sees the *most recent* nudge per user. The admin UI must
    surface this limitation in a footnote.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              COUNT(*) FILTER (
                WHERE up.last_nudge_sent_at::timestamptz > NOW() - INTERVAL '{int(days)} days'
              ) AS sent,
              COUNT(*) FILTER (
                WHERE up.last_nudge_sent_at::timestamptz > NOW() - INTERVAL '{int(days)} days'
                  AND EXISTS (
                    SELECT 1 FROM meals m
                    WHERE m.user_id = up.user_id
                      AND m.created_at IS NOT NULL
                      AND m.created_at::timestamptz BETWEEN up.last_nudge_sent_at::timestamptz
                                                        AND up.last_nudge_sent_at::timestamptz + INTERVAL '24 hours'
                  )
              ) AS converted
            FROM user_profiles up
            WHERE up.last_nudge_sent_at IS NOT NULL
            """
        )
        row = cur.fetchone()
    sent = int(row[0] or 0)
    converted = int(row[1] or 0)
    pct = round(converted / sent * 100, 1) if sent else 0.0
    return {"sent": sent, "converted": converted, "pct": pct}


def get_ai_cost_estimate(conn, days: int = 30, rates: Optional[dict] = None) -> dict:
    """Estimated OpenAI cost from `usage_quota` × per-action rate map.

    Returns `{total_usd, by_action, by_day, top_spenders}`.
    """
    from lib.config import COST_RATES as _DEFAULT_RATES
    rates = rates or _DEFAULT_RATES
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT day, action, user_id, count
            FROM usage_quota
            WHERE day >= TO_CHAR(CURRENT_DATE - {int(days) - 1}, 'YYYY-MM-DD')
            """
        )
        rows = cur.fetchall()
    total = 0.0
    by_action: dict[str, float] = {}
    by_day: dict[str, float] = {}
    by_user: dict[int, float] = {}
    for day, action, uid, count in rows:
        rate = float(rates.get(action, 0.0))
        cost = rate * int(count or 0)
        total += cost
        by_action[action] = by_action.get(action, 0.0) + cost
        by_day[day] = by_day.get(day, 0.0) + cost
        by_user[uid] = by_user.get(uid, 0.0) + cost
    top_spenders = sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "total_usd":    round(total, 2),
        "by_action":    {a: round(v, 2) for a, v in by_action.items()},
        "by_day":       {d: round(v, 2) for d, v in sorted(by_day.items())},
        "top_spenders": [(uid, round(v, 2)) for uid, v in top_spenders],
    }


def get_weight_outcomes(conn, days: int = 30) -> dict:
    """For users with a target weight + lose/gain goal, bucket by 30-day trend.

    Returns `{on_track: [...], stalled: [...], regressing: [...]}` where
    each list contains `{user_id, goal, target_weight_kg, weight_kg,
    delta_30d_kg}` dicts.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH series AS (
              SELECT user_id,
                     REGR_SLOPE(weight_kg, EXTRACT(EPOCH FROM recorded_at)) AS slope_per_sec
              FROM weight_history
              WHERE recorded_at > NOW() - INTERVAL '{int(days)} days'
              GROUP BY user_id
              HAVING COUNT(*) >= 2
            )
            SELECT s.user_id, p.goal, p.target_weight_kg, p.weight_kg,
                   s.slope_per_sec * 86400 * {int(days)} AS delta_kg
            FROM series s
            JOIN user_profiles p ON p.user_id = s.user_id
            WHERE p.target_weight_kg IS NOT NULL
              AND p.goal IN ('lose', 'gain')
            """
        )
        rows = cur.fetchall()
    buckets: dict[str, list[dict]] = {"on_track": [], "stalled": [], "regressing": []}
    for uid, goal, target, weight, delta in rows:
        d = float(delta or 0)
        rec = {
            "user_id":          uid,
            "goal":             goal,
            "target_weight_kg": target,
            "weight_kg":        weight,
            "delta_30d_kg":     round(d, 2),
        }
        if abs(d) <= 0.2:
            buckets["stalled"].append(rec)
        elif (goal == "lose" and d < -0.2) or (goal == "gain" and d > 0.2):
            buckets["on_track"].append(rec)
        else:
            buckets["regressing"].append(rec)
    return buckets


def get_recent_events(conn, limit: int = 50) -> list[dict]:
    """Synthesised event feed: most-recent N entries across signups, meals,
    weight logs, and (latest-only) nudges. UNION ALL with per-source LIMITs
    keeps the result snappy without scanning whole tables.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            ( SELECT 'signup'::text AS kind, user_id, created_at::timestamptz AS ts,
                     ''::text AS extra
              FROM users WHERE created_at IS NOT NULL
              ORDER BY created_at::timestamptz DESC LIMIT {int(limit)} )
            UNION ALL
            ( SELECT 'meal'::text, user_id, created_at::timestamptz,
                     COALESCE(LEFT(description, 60), '')
              FROM meals WHERE created_at IS NOT NULL
              ORDER BY created_at::timestamptz DESC LIMIT {int(limit)} )
            UNION ALL
            ( SELECT 'weight'::text, user_id, recorded_at,
                     weight_kg::text
              FROM weight_history
              ORDER BY recorded_at DESC LIMIT {int(limit)} )
            UNION ALL
            ( SELECT 'nudge'::text, user_id, last_nudge_sent_at::timestamptz,
                     ''::text
              FROM user_profiles WHERE last_nudge_sent_at IS NOT NULL
              ORDER BY last_nudge_sent_at::timestamptz DESC LIMIT {int(limit)} )
            ORDER BY ts DESC NULLS LAST LIMIT {int(limit)}
            """
        )
        rows = cur.fetchall()
    return [
        {"kind": r[0], "user_id": r[1], "ts": r[2], "extra": r[3]}
        for r in rows
    ]


def get_latest_cron_runs(conn) -> list[dict]:
    """One most-recent row per `cron_name`, ordered alphabetically by name.

    Returns dicts with `cron_name`, `started_at`, `finished_at`, `status`,
    `result_json` (parsed dict or None), `error`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (cron_name)
              cron_name, started_at, finished_at, status, result_json, error
            FROM cron_runs
            ORDER BY cron_name, started_at DESC
            """
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        result = None
        if r[4]:
            try:
                result = json.loads(r[4])
            except Exception:
                result = None
        out.append({
            "cron_name":   r[0],
            "started_at":  r[1],
            "finished_at": r[2],
            "status":      r[3],
            "result":      result,
            "error":       r[5],
        })
    return out


# ---------- Daily health monitor helpers ----------
# Each helper below returns plain Python primitives so the monitor can compose
# them without any DB-specific knowledge of its own. The monitor module
# decides what counts as "alert" vs "ok" — these only return facts.


def count_cron_runs_24h(conn, cron_name: str) -> int:
    """How many `cron_runs` rows for ``cron_name`` finished in the last 24h.

    Counts only finished invocations (``finished_at`` set) — the monitor
    treats unfinished rows as inflight rather than as fires that landed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM cron_runs
            WHERE cron_name = %s
              AND finished_at IS NOT NULL
              AND finished_at >= NOW() - INTERVAL '24 hours'
            """,
            (cron_name,),
        )
        row = cur.fetchone()
    return int(row[0] or 0)


def get_cron_errors_24h(conn) -> list[dict]:
    """Every `cron_runs` row in the last 24h whose cron itself errored
    (``status = 'error'`` or non-empty ``error`` column).

    Returns `[{cron_name, finished_at, error}]` ordered newest first.
    The health monitor pages a one-line alert per row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cron_name, finished_at, error
            FROM cron_runs
            WHERE finished_at IS NOT NULL
              AND finished_at >= NOW() - INTERVAL '24 hours'
              AND (status = 'error' OR (error IS NOT NULL AND error <> ''))
            ORDER BY finished_at DESC
            """
        )
        rows = cur.fetchall()
    return [{"cron_name": r[0], "finished_at": r[1], "error": r[2]} for r in rows]


def get_user_errors_in_cron_runs_24h(conn) -> list[dict]:
    """Surface per-user errors that the crons swallowed into
    ``result_json.errors``.

    Crons keep running through per-user failures (so one bad row doesn't
    abort the whole cohort) and append `{user_id, error, ...}` records
    into the ``errors`` array in their `result_json`. This helper unpacks
    those across all 24h runs so the health monitor can see them.

    Returns `[{cron_name, finished_at, errors_count, sample}]` with one
    row per cron run that had any per-user errors. ``sample`` is the
    first error dict from that run (used to render a one-line hint).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cron_name, finished_at, result_json
            FROM cron_runs
            WHERE finished_at IS NOT NULL
              AND finished_at >= NOW() - INTERVAL '24 hours'
              AND status = 'ok'
              AND result_json IS NOT NULL
            ORDER BY finished_at DESC
            """
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for cron_name, finished_at, result_json in rows:
        try:
            parsed = json.loads(result_json) if result_json else {}
        except Exception:
            continue
        errs = parsed.get("errors") or []
        if not isinstance(errs, list) or not errs:
            continue
        sample = errs[0] if isinstance(errs[0], dict) else {"error": str(errs[0])}
        out.append({
            "cron_name":     cron_name,
            "finished_at":   finished_at,
            "errors_count":  len(errs),
            "sample":        sample,
        })
    return out


def sum_counters_24h(conn, cron_name: str, keys: list[str]) -> dict[str, int]:
    """Sum each named counter in ``cron_runs.result_json`` across the
    last 24h of successful runs of ``cron_name``.

    Used by the health monitor to roll per-hour cron output (e.g.
    `cron_good_morning` runs 24×, each with a small `activation_sent_demo`
    integer) up to a single daily total per counter.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT result_json
            FROM cron_runs
            WHERE cron_name = %s
              AND finished_at IS NOT NULL
              AND finished_at >= NOW() - INTERVAL '24 hours'
              AND status = 'ok'
              AND result_json IS NOT NULL
            """,
            (cron_name,),
        )
        rows = cur.fetchall()
    totals = {k: 0 for k in keys}
    for (result_json,) in rows:
        try:
            parsed = json.loads(result_json) if result_json else {}
        except Exception:
            continue
        for k in keys:
            v = parsed.get(k)
            try:
                totals[k] += int(v or 0)
            except (TypeError, ValueError):
                pass
    return totals


def count_users_logged_yesterday_utc(conn) -> int:
    """Distinct users who logged ≥1 meal where ``created_at::date`` was
    yesterday (UTC).

    Health monitor cross-references this against the sum of
    ``sent_summary`` across yesterday's ``cron_daily_summary`` runs to
    detect "users logged but didn't receive their evening summary". Not
    timezone-aware on purpose — each cron fire already gates on per-user
    tz, so the UTC-day comparison is a coarse-grained sanity check, not
    an exact accounting.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM meals
            WHERE created_at IS NOT NULL
              AND created_at::date = (CURRENT_DATE - INTERVAL '1 day')::date
            """
        )
        row = cur.fetchone()
    return int(row[0] or 0)


def count_new_blocks(conn, hours: int = 24) -> int:
    """Distinct users who had `blocked_at` stamped in the last ``hours``.

    Telegram returning 400/403 stamps `blocked_at` (see
    ``_send_with_autoblock`` / ``_send_with_autoptout``). A spike vs. the
    7-day baseline means a recent send annoyed a cohort.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM user_profiles
            WHERE blocked_at IS NOT NULL
              AND blocked_at::timestamptz >= NOW() - INTERVAL '{int(hours)} hours'
            """
        )
        row = cur.fetchone()
    return int(row[0] or 0)


def avg_daily_blocks(conn, days: int = 7) -> float:
    """Average new `blocked_at` per day over the last ``days``.

    Used as the baseline for ``count_new_blocks`` — if today's count is
    more than 2× this, the monitor pages an alert.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)::float / {int(days)}
            FROM user_profiles
            WHERE blocked_at IS NOT NULL
              AND blocked_at::timestamptz >= NOW() - INTERVAL '{int(days)} days'
            """
        )
        row = cur.fetchone()
    return float(row[0] or 0.0)


def get_users_stuck_in_activation_step(
    conn, step: str, min_days: int
) -> list[int]:
    """Onboarded never-loggers whose ``activation_step = step`` was set
    ≥``min_days`` days ago (proxied by ``user_profiles.updated_at``).

    Used by the funnel-progression check — e.g. a user sitting in
    `'demo'` for >3 days should already have advanced to `'d4_followup'`
    or logged a meal. Anyone stuck signals the morning cron isn't
    processing that rung correctly.

    Skips users with ``nudge_optout = 1`` (they explicitly muted) and
    users with any logged meal (they're no longer in the funnel).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT up.user_id
            FROM user_profiles up
            WHERE up.activation_step = %s
              AND COALESCE(up.nudge_optout, 0) = 0
              AND up.blocked_at IS NULL
              AND up.updated_at::timestamptz
                  <= NOW() - INTERVAL '{int(min_days)} days'
              AND NOT EXISTS (
                  SELECT 1 FROM meals m WHERE m.user_id = up.user_id
              )
            """,
            (step,),
        )
        rows = cur.fetchall()
    return [int(r[0]) for r in rows]


def count_signups_24h(conn) -> dict[str, int]:
    """Signups in the last 24h, broken into `done` (finished onboarding)
    vs. `mid` (still in flow). Used in the daily-activity report line.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE COALESCE(p.onboarding_step, '') = 'done') AS done_count,
              COUNT(*) FILTER (WHERE COALESCE(p.onboarding_step, '') <> 'done') AS mid_count
            FROM users u
            LEFT JOIN user_profiles p ON p.user_id = u.user_id
            WHERE u.created_at IS NOT NULL
              AND u.created_at::timestamptz >= NOW() - INTERVAL '24 hours'
            """
        )
        row = cur.fetchone()
    return {
        "done": int((row[0] if row else 0) or 0),
        "mid":  int((row[1] if row else 0) or 0),
    }


def count_meals_and_active_users_24h(conn) -> dict[str, int]:
    """Total meals logged + distinct active users in the last 24h."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT user_id)
            FROM meals
            WHERE created_at IS NOT NULL
              AND created_at::timestamptz >= NOW() - INTERVAL '24 hours'
            """
        )
        row = cur.fetchone()
    return {
        "meals":        int((row[0] if row else 0) or 0),
        "active_users": int((row[1] if row else 0) or 0),
    }


def count_first_meal_logs_today(conn) -> int:
    """Users whose *first lifetime meal* landed in the last 24h.

    Together with the demo-card counter, this answers "did the F-17
    activation funnel convert anyone yesterday?" without coupling to
    any single morning send — first-meal is the only success metric
    that matters.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT user_id, MIN(created_at::timestamptz) AS first_log
              FROM meals
              WHERE created_at IS NOT NULL
              GROUP BY user_id
            ) firsts
            WHERE first_log >= NOW() - INTERVAL '24 hours'
            """
        )
        row = cur.fetchone()
    return int((row[0] if row else 0) or 0)


# ---------- Cron-run lifecycle (start / finish bracket) ----------
# The existing `record_cron_run` writes one row at the end of each cron
# via `finally:`. That's invisible to crashes: if a fire dies before its
# finally runs, we see nothing — identical to "Vercel never invoked us".
#
# The helpers below split the lifecycle:
#   * start_cron_run  → INSERT status='running' at top of handler
#   * finish_cron_run → UPDATE that row with the final outcome
# A row that stays `running` after 24h is a fire that crashed mid-flight,
# distinct from "Vercel never invoked" (zero rows). That's the
# diagnostic.
#
# Both helpers are defensive: they swallow their own errors so that
# observability NEVER blocks the cron's actual work. The worst case is
# a missing row, not a missing notification.


def start_cron_run(conn, cron_name: str) -> Optional[int]:
    """INSERT a `'running'` row at the top of each cron handler. Returns
    the new row id, or `None` on any DB error.

    ``None`` is the explicit signal to ``finish_cron_run`` that we lost
    the start row — observability must never block the actual cron.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cron_runs (cron_name, status) "
                "VALUES (%s, 'running') RETURNING id",
                (cron_name,),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row else None
    except Exception:
        return None


def finish_cron_run(conn, run_id: Optional[int], status: str,
                    result: Optional[dict] = None,
                    error: Optional[str] = None) -> None:
    """UPDATE the row created by ``start_cron_run`` with the outcome.

    Safe to call unconditionally in a `finally:` block:
      * ``run_id is None`` (start failed) → falls back to a one-shot
        ``record_cron_run`` insert with cron_name='<orphan>' so we
        still leave SOME audit trace.
      * any DB error during the UPDATE is swallowed — the cron's actual
        outcome (notifications sent etc.) must never be masked.
    """
    if run_id is None:
        try:
            record_cron_run(conn, "<orphan>", status, result, error)
        except Exception:
            pass
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE cron_runs
                      SET finished_at = now(),
                          status = %s,
                          result_json = %s,
                          error = %s
                    WHERE id = %s""",
                (status,
                 json.dumps(result) if result is not None else None,
                 error,
                 run_id),
            )
        conn.commit()
    except Exception:
        pass  # observability must never mask the cron outcome


def count_cron_runs_24h_by_status(conn, cron_name: str) -> dict:
    """Per-status breakdown of the last 24h of ``cron_name`` invocations.

    Returns ``{started, finished_ok, errored, running_unfinished}``.

    The ``running_unfinished`` bucket is the diagnostic — rows that
    inserted at start but never got updated, meaning Vercel killed the
    function mid-flight (timeout / OOM) or the `finally` block itself
    crashed. Distinguishes "Vercel didn't invoke" (zero rows) from
    "function crashed" (rows exist but never finalised).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*)                                              AS started,
              COUNT(*) FILTER (WHERE finished_at IS NOT NULL
                                 AND status = 'ok')                 AS finished_ok,
              COUNT(*) FILTER (WHERE status = 'error')              AS errored,
              COUNT(*) FILTER (WHERE finished_at IS NULL
                                 AND status = 'running')            AS running_unfinished
            FROM cron_runs
            WHERE cron_name = %s
              AND started_at >= NOW() - INTERVAL '24 hours'
            """,
            (cron_name,),
        )
        row = cur.fetchone()
    return {
        "started":            int(row[0] or 0) if row else 0,
        "finished_ok":        int(row[1] or 0) if row else 0,
        "errored":            int(row[2] or 0) if row else 0,
        "running_unfinished": int(row[3] or 0) if row else 0,
    }
