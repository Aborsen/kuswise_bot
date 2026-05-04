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
    conn.commit()
    _SCHEMA_INITIALISED = True
    if close_after:
        try:
            conn.close()
        except Exception:
            pass


# ---------- Users ----------

def upsert_user(conn, user_id: int, username: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (user_id, username, created_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id, username or "", _now_iso()),
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
    """Kick the user back to the start of the onboarding flow."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET onboarding_step = 'awaiting_age', updated_at = %s "
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
    """Non-destructive read of the user's pending analysis."""
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
    return {
        "id": row[0],
        "meal_type": row[1],
        "analysis": json.loads(row[2]),
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

def get_users_needing_summary(conn) -> list[tuple[int, str]]:
    today = _today_str()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT dl.user_id, dl.date
               FROM daily_logs dl
               WHERE dl.date = %s AND dl.summary_sent = 0
                 AND EXISTS (SELECT 1 FROM meals m WHERE m.user_id = dl.user_id AND m.date = dl.date)
                 AND EXISTS (
                   SELECT 1 FROM user_profiles up
                   WHERE up.user_id = dl.user_id AND up.onboarding_step = 'done'
                 )""",
            (today,),
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
                 AND (weekly_checkin_sent_at IS NULL
                      OR weekly_checkin_sent_at < %s)""",
            (cutoff,),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]


def get_users_to_nudge(conn) -> list[dict]:
    """Onboarded, opted-in users with zero meals today.

    Daily cadence with no cooldown — never-loggers, lapsed users, and dormant
    users all get one warm reminder per day. The only gates are explicit
    `/quiet` (`nudge_optout=1`) and the auto-opt-out triggered by TG 400/403
    in the cron.

    Returns a list of `{user_id, lang}` dicts.
    """
    today = _today_str()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT up.user_id, COALESCE(up.lang, 'en') AS lang
            FROM user_profiles up
            WHERE up.onboarding_step = 'done'
              AND up.daily_calorie_target IS NOT NULL
              AND COALESCE(up.nudge_optout, 0) = 0
              AND NOT EXISTS (
                  SELECT 1 FROM meals m WHERE m.user_id = up.user_id AND m.date = %s
              )
            ORDER BY up.user_id
            """,
            (today,),
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
