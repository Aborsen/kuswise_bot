"""Per-user timezone helpers (F-3).

This module provides helpers for callers that have a user profile in scope.
Today's "today" boundary, the meal-type-by-hour heuristic, and date deltas
should all use the user's local timezone — not the bot-wide ``LOCAL_TZ``.

Phase 1 (this commit): wire the helpers into user-facing call sites in
``api/webhook.py``. The internal date helpers in ``lib/database.py`` and the
cron schedules still use ``LOCAL_TZ``; that migration is F-3 Phase 2.
"""
from datetime import datetime
from typing import Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib.config import LOCAL_TZ


ProfileOrTz = Union[dict, str, None]


def user_tz(profile_or_str: ProfileOrTz) -> ZoneInfo:
    """Resolve a profile dict (or a tz string) to a ``ZoneInfo``.

    Falls back to the bot's default ``Europe/Kyiv`` when the value is empty,
    malformed, or names an unknown zone. The fallback keeps existing logs
    valid even if a stale tz string sneaks in from an old client or fixture.
    """
    if isinstance(profile_or_str, dict):
        tz = (profile_or_str.get("tz") or "").strip()
    else:
        tz = (profile_or_str or "").strip()
    if not tz:
        return LOCAL_TZ
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        # Python 3.13+ raises ValueError for malformed keys like "Europe/" or
        # paths with traversal components.
        return LOCAL_TZ


def is_valid_tz(tz: str | None) -> bool:
    """True iff `tz` is a non-empty IANA zone the host can resolve."""
    if not tz or not isinstance(tz, str):
        return False
    try:
        ZoneInfo(tz.strip())
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def now_user(profile_or_str: ProfileOrTz) -> datetime:
    """Current ``datetime`` in the user's timezone."""
    return datetime.now(user_tz(profile_or_str))


def today_str_user(profile_or_str: ProfileOrTz) -> str:
    """User-local YYYY-MM-DD calendar date."""
    return now_user(profile_or_str).strftime("%Y-%m-%d")


# Onboarding presets shown in the timezone keyboard. Keep this list in sync
# with ``lib.telegram_helpers.tz_keyboard``.
TZ_PRESETS: tuple[tuple[str, str], ...] = (
    ("Europe/Kyiv",          "🇺🇦 Київ"),
    ("Europe/London",        "🇬🇧 Лондон"),
    ("Europe/Berlin",        "🇩🇪 Берлін / Варшава"),
    ("America/New_York",     "🇺🇸 Нью-Йорк"),
    ("America/Los_Angeles",  "🇺🇸 Лос-Анджелес"),
    ("Asia/Dubai",           "🇦🇪 Дубай"),
)
