#!/usr/bin/env python3
"""Register the bot's `/` autocomplete command list with Telegram.

Run this whenever commands change:

    .venv/bin/python scripts/setup_bot_commands.py

We register the command list across BOTH scopes (default + all_private_chats):
  - EN with no ``language_code``  → universal fallback for any client
  - UK under ``language_code`` of "uk", "ru", "be"  → matches every
    Slavic Telegram-UI code that ``lib.i18n.normalize_lang`` collapses
    to bot locale "uk", so a Ukrainian user whose Telegram client is
    in Russian still gets the Ukrainian menu.

Why both scopes: Telegram's lookup order is
``(scope match) > (language match)``, so a UA user in a DM hits the
more-specific ``all_private_chats`` registration before falling back
to ``default``. Without the private-scope UK registrations, those
users would see English even though we registered Ukrainian at default.

Idempotent — safe to re-run. Reads ``TELEGRAM_BOT_TOKEN`` from env /
``.env``. Source of truth for the list itself is ``lib/bot_commands.py``.
"""
from __future__ import annotations

import os
import sys

# Make ``lib.*`` importable when run from anywhere in the repo.
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from lib.bot_commands import build_commands


def _post(token: str, payload: dict, label: str) -> bool:
    base = f"https://api.telegram.org/bot{token}"
    resp = httpx.post(f"{base}/setMyCommands", json=payload, timeout=10)
    body = resp.json()
    if not body.get("ok"):
        print(f"  {label}: FAILED — {body}", file=sys.stderr)
        return False
    print(f"  {label}: ok")
    return True


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in env / .env", file=sys.stderr)
        return 1

    en_commands = build_commands(locale="en")
    uk_commands = build_commands(locale="uk")

    # Slavic Telegram-UI codes that lib/i18n/__init__.py:normalize_lang()
    # collapses to bot locale "uk". "ua" is dropped — it's a country code,
    # not an ISO 639-1 language tag, so no real Telegram client sends it.
    uk_language_codes = ("uk", "ru", "be")
    scopes = (
        (None, "default scope"),
        ({"type": "all_private_chats"}, "private scope"),
    )

    total = len(scopes) * (1 + len(uk_language_codes))
    print(
        f"Registering {len(en_commands)} commands across {total} "
        f"(scope × language) registrations…"
    )

    ok = True
    for scope, scope_label in scopes:
        en_payload: dict = {"commands": en_commands}
        if scope:
            en_payload["scope"] = scope
        ok &= _post(token, en_payload, f"EN {scope_label}")

        for lang_code in uk_language_codes:
            uk_payload: dict = {
                "commands": uk_commands,
                "language_code": lang_code,
            }
            if scope:
                uk_payload["scope"] = scope
            ok &= _post(token, uk_payload, f"UK ({lang_code}) {scope_label}")

    if not ok:
        return 1

    print("Done. Telegram clients should pick up the new list within a few minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
