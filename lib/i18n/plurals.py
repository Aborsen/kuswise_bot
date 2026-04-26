"""Pluralization for the bot's two first-class locales (F-2b).

Ukrainian uses Slavic-style 1 / 2-4 / 5+ rules with an 11-14 exception
("11 днів", not "11 день"). English uses simple 1 / not-1.

Public surface:
    pluralize(n, lang, singular, few=None, many=None) -> str

Examples:
    pluralize(1,  "uk", "день",  "дні",  "днів")  -> "день"
    pluralize(3,  "uk", "день",  "дні",  "днів")  -> "дні"
    pluralize(5,  "uk", "день",  "дні",  "днів")  -> "днів"
    pluralize(11, "uk", "день",  "дні",  "днів")  -> "днів"
    pluralize(21, "uk", "день",  "дні",  "днів")  -> "день"
    pluralize(1,  "en", "day",   None,   "days")  -> "day"
    pluralize(2,  "en", "day",   None,   "days")  -> "days"
    pluralize(0,  "en", "day",   None,   "days")  -> "days"

This module deliberately ships no dependency on Babel / ICU — for two
languages with well-known rules, a tiny hand-rolled function is cheaper
to maintain and trivially testable.
"""
from __future__ import annotations

from typing import Optional


def pluralize(
    n: int | float,
    lang: str,
    singular: str,
    few: Optional[str] = None,
    many: Optional[str] = None,
) -> str:
    """Pick the correct plural form for ``n`` in ``lang``.

    - For ``"uk"``: ``singular`` (1, 21, 31, …), ``few`` (2-4, 22-24, …),
      ``many`` (0, 5-20, 25-30, …) with the 11-14 exception forcing ``many``.
    - For ``"en"`` (and any other lang): ``singular`` for n == ±1, else
      ``many`` (or ``singular`` if ``many`` was None — pluralized form falls
      back to the lemma).

    ``few`` is required for ``uk`` but optional for ``en`` (English has only
    two forms). ``many`` is recommended for both.
    """
    n_abs = abs(int(n))
    if (lang or "").lower().startswith("uk"):
        if few is None or many is None:
            raise ValueError("uk plurals require both `few` and `many`")
        # 11-14 exception always go to "many".
        if 11 <= (n_abs % 100) <= 14:
            return many
        last = n_abs % 10
        if last == 1:
            return singular
        if 2 <= last <= 4:
            return few
        return many
    # English (and fallback): two forms.
    if n_abs == 1:
        return singular
    return many if many is not None else singular


def pluralize_with_count(
    n: int | float,
    lang: str,
    singular: str,
    few: Optional[str] = None,
    many: Optional[str] = None,
) -> str:
    """Convenience: ``"5 днів"`` / ``"1 day"`` in one call."""
    word = pluralize(n, lang, singular, few, many)
    return f"{int(n)} {word}"
