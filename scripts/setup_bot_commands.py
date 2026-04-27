#!/usr/bin/env python3
"""Register the bot's `/` autocomplete command list with Telegram.

Run this whenever commands change:

    .venv/bin/python scripts/setup_bot_commands.py

We register the command list across BOTH locales × BOTH scopes:
  1. default scope, no language       → EN (universal fallback)
  2. default scope, language_code=uk  → UA
  3. all_private_chats, no language   → EN
  4. all_private_chats, language_code=uk → UA

Why all four: Telegram's lookup order is
``(scope match) > (language match)``, so a UA user in a DM hits the
more-specific ``all_private_chats`` registration before falling back
to the ``default`` + uk match. Without registration #4, those users
would see English even though we registered Ukrainian at default.

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
    print(f"Registering {len(en_commands)} commands × 2 locales × 2 scopes…")

    ok = True
    # 1. EN at default scope (universal fallback for any non-UK client).
    ok &= _post(token, {"commands": en_commands}, "EN default scope")
    # 2. UK at default scope (Telegram serves to clients with language_code=uk).
    ok &= _post(
        token,
        {"commands": uk_commands, "language_code": "uk"},
        "UK default scope",
    )
    # 3. EN at all_private_chats — same scope as #4 below so the lookup
    #    order doesn't bypass the UA registration for UK users in DMs.
    ok &= _post(
        token,
        {"commands": en_commands, "scope": {"type": "all_private_chats"}},
        "EN private scope",
    )
    # 4. UK at all_private_chats — without this, Telegram's
    #    "more-specific scope wins over language match" rule would serve
    #    EN to UK users in DMs from registration #3.
    ok &= _post(
        token,
        {
            "commands": uk_commands,
            "scope": {"type": "all_private_chats"},
            "language_code": "uk",
        },
        "UK private scope",
    )

    if not ok:
        return 1

    print("Done. Telegram clients should pick up the new list within a few minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
