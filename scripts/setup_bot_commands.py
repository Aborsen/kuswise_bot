#!/usr/bin/env python3
"""Register the bot's `/` autocomplete command list with Telegram.

Run this whenever commands change:

    .venv/bin/python scripts/setup_bot_commands.py

It POSTs to ``setMyCommands`` once for the EN default scope and once
with ``language_code="uk"`` so UA-locale Telegram clients pick up
Ukrainian descriptions while everyone else sees English. Idempotent —
safe to re-run.

Reads ``TELEGRAM_BOT_TOKEN`` from environment / ``.env``. Source of
truth for the command list itself is ``lib/bot_commands.py``.
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
    print(f"Registering {len(en_commands)} commands per locale (EN + UK)…")

    ok = True
    # 1. EN default scope (covers every user whose Telegram client isn't UK).
    ok &= _post(token, {"commands": en_commands}, "EN default scope")
    # 2. EN explicit, all-private-chats scope. Locks the DM list against future
    #    default-scope edits.
    ok &= _post(
        token,
        {"commands": en_commands, "scope": {"type": "all_private_chats"}},
        "EN private scope",
    )
    # 3. UK localized list (Telegram serves it to clients with language_code=uk).
    ok &= _post(
        token,
        {"commands": uk_commands, "language_code": "uk"},
        "UK locale",
    )

    if not ok:
        return 1

    print("Done. Telegram clients should pick up the new list within a few minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
