"""Lightweight i18n shim (F-2).

Strings live in JSON dictionaries under ``lib/i18n/dict_<lang>.json``::

    t("welcome", lang="en", name="Vic")  # "Welcome, Vic!"

Lookup order: requested ``lang`` → primary (``uk``) → key itself.

This module is intentionally minimal — no compiled message catalogs, no
plural support, no gettext. The bot has ~hundreds of strings; this is the
framework for migration. Drop in ``gettext`` later if scale demands.
"""
import json
import os
from typing import Any

_HERE = os.path.dirname(__file__)
_PRIMARY = "uk"      # legacy strings live here
_SUPPORTED = ("en", "uk")
_dictionaries: dict[str, dict[str, str]] = {}


def _load(lang: str) -> dict[str, str]:
    if lang in _dictionaries:
        return _dictionaries[lang]
    path = os.path.join(_HERE, f"dict_{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            _dictionaries[lang] = json.load(f) or {}
    except FileNotFoundError:
        _dictionaries[lang] = {}
    return _dictionaries[lang]


def t(key: str, locale: str | None = None, **kwargs: Any) -> str:
    """Look up ``key`` in ``locale``, falling back to the primary lang then key.

    ``locale`` is the user's language ('en' or 'uk'). It's named ``locale``
    rather than ``lang`` so a template can include a ``{lang}`` placeholder
    without colliding with the function's own keyword argument.
    """
    locale = locale or _PRIMARY
    candidates = (locale, _PRIMARY) if locale != _PRIMARY else (_PRIMARY,)
    for cand in candidates:
        d = _load(cand)
        if key in d:
            template = d[key]
            try:
                return template.format(**kwargs) if kwargs else template
            except (KeyError, IndexError):
                return template
    return key


def supported_langs() -> tuple[str, ...]:
    return _SUPPORTED


def normalize_lang(code: str | None) -> str:
    """Map a Telegram ``language_code`` to a supported ``lang``.

    UK / RU / BE → ``uk``; everything else → ``en``. Empty / None → ``en``.
    """
    if not code:
        return "en"
    base = code.lower().split("-", 1)[0].split("_", 1)[0]
    if base in ("uk", "ua", "ru", "be"):
        return "uk"
    return "en"
