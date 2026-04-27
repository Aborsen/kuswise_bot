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


# F-2b: locale-aware human-readable dates.
# We don't pull in Babel for two locales — the month tables are tiny.
_MONTHS_UK: tuple[str, ...] = (  # noqa: i18n
    "січня", "лютого", "березня", "квітня", "травня", "червня",  # noqa: i18n
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",  # noqa: i18n
)
_MONTHS_EN: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def format_date_long(dt: datetime, lang: str = "en") -> str:
    """Render a date as UK form (e.g. 26 + month-genitive) or EN (April 26).

    No year — typical use is a screen header where the year is implicit.
    Add the year separately when needed.
    """
    if (lang or "").lower().startswith("uk"):
        return f"{dt.day} {_MONTHS_UK[dt.month - 1]}"
    return f"{_MONTHS_EN[dt.month - 1]} {dt.day}"


def format_date_with_year(dt: datetime, lang: str = "en") -> str:
    """Same as ``format_date_long`` but with the year appended (``2026-09-15``)."""
    base = format_date_long(dt, lang)
    return f"{base} {dt.year}"


# Onboarding presets shown in the timezone keyboard. Keep this list in sync
# with ``lib.telegram_helpers.tz_keyboard``.
# UA-flavored display labels are kept here as the *fallback* used by callers
# that don't pass a locale. The locale-aware tz_keyboard reads from the
# tz_keyboard.* dict keys (see lib/i18n/dict_*.json); these strings only
# show up if a caller hardcodes the fallback. Keep both for compatibility.
TZ_PRESETS: tuple[tuple[str, str], ...] = (
    ("Europe/Kyiv",          "🇺🇦 Київ"),  # noqa: i18n
    ("Europe/London",        "🇬🇧 Лондон"),  # noqa: i18n
    ("Europe/Berlin",        "🇩🇪 Берлін / Варшава"),  # noqa: i18n
    ("America/New_York",     "🇺🇸 Нью-Йорк"),  # noqa: i18n
    ("America/Los_Angeles",  "🇺🇸 Лос-Анджелес"),  # noqa: i18n
    ("Asia/Dubai",           "🇦🇪 Дубай"),  # noqa: i18n
)
