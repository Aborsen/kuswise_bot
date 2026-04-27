"""Per-user / per-day rate limits for OpenAI-billed actions.

This is *not* a paywall — it's a uniform abuse + cost cap. Limits live in env
vars so we can tune them without redeploying. The schema lives in
``usage_counters`` (created by ``lib.database.init_db``).
"""
import os
from datetime import datetime
from typing import Optional

from lib.config import LOCAL_TZ


# Map "kind" of action → counter column. Multiple kinds can share a column
# (e.g. text + photo + voice all count toward "photos").
_KIND_TO_COL = {
    "photo": "photos",
    "text": "photos",
    "voice": "photos",
    "ask": "asks",
    "ocr": "ocr",
    "plan": "plans",
}


def _limit(kind: str, default: int) -> int:
    return int(os.environ.get(f"LIMIT_{kind.upper()}_PER_DAY", default))


# Per-column daily caps. Tune from telemetry; raise once we have data.
LIMITS = {
    "photos":   _limit("photo",    50),
    "asks":     _limit("ask",      20),
    "ocr":      _limit("ocr",       5),
    "plans":    _limit("plan",      3),
    "total_ai": _limit("total_ai", 100),
}

# Hard ceiling on per-user OpenAI spend per day, USD. 0 disables the budget
# guard (counters still apply). Used by callers that pass an estimated cost.
DAILY_OPENAI_USD_CAP = float(os.environ.get("DAILY_OPENAI_USD_CAP", "2.0"))


def _today_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def check_and_increment(conn, user_id: int, kind: str) -> tuple[bool, int]:
    """Atomic counter check + increment.

    Returns ``(allowed, remaining_after_call)``. When ``allowed`` is False,
    no counter is mutated; ``remaining`` is the headroom for ``kind``.
    """
    col = _KIND_TO_COL.get(kind, "total_ai")
    kind_limit = LIMITS.get(col, LIMITS["total_ai"])
    total_limit = LIMITS["total_ai"]
    today = _today_str()

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO usage_counters (user_id, day, photos, asks, ocr, plans, total_ai)
               VALUES (%s, %s, 0, 0, 0, 0, 0)
               ON CONFLICT (user_id, day) DO NOTHING""",
            (user_id, today),
        )
        cur.execute(
            f"SELECT {col}, total_ai FROM usage_counters WHERE user_id = %s AND day = %s",
            (user_id, today),
        )
        row = cur.fetchone() or (0, 0)
        cur_kind, cur_total = int(row[0]), int(row[1])
        if cur_kind >= kind_limit or cur_total >= total_limit:
            conn.commit()
            return False, max(0, kind_limit - cur_kind)
        cur.execute(
            f"""UPDATE usage_counters
                SET {col} = {col} + 1, total_ai = total_ai + 1
                WHERE user_id = %s AND day = %s""",
            (user_id, today),
        )
    conn.commit()
    return True, max(0, kind_limit - (cur_kind + 1))


def get_today_counters(conn, user_id: int) -> dict:
    """Diagnostic read: today's counters for a user. Zeros when no row yet."""
    today = _today_str()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT photos, asks, ocr, plans, total_ai FROM usage_counters "
            "WHERE user_id = %s AND day = %s",
            (user_id, today),
        )
        row = cur.fetchone()
    if not row:
        return {"photos": 0, "asks": 0, "ocr": 0, "plans": 0, "total_ai": 0}
    return dict(zip(("photos", "asks", "ocr", "plans", "total_ai"), row))


def limit_reached_message(kind: str = "photo", locale: str = "en") -> str:
    """User-facing throttle message. Reads from lib/i18n dictionaries."""
    from lib.i18n import t
    return t("rate_limit.daily_quota_hit", locale=locale)
