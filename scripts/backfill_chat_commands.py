#!/usr/bin/env python3
"""One-shot: pin a chat-scoped `/` command menu for every existing user.

Why this exists:
- setMyCommands at chat-scope overrides Telegram's client-UI
  ``language_code`` lookup, so the menu matches the user's bot-side
  language no matter what UI their phone is in.
- ``handle_language_callback`` and the F-2b onboarding confirm now do
  this on every language pick, but users who picked their language
  *before* that code shipped never triggered it. Their slash menu still
  falls back to the global ``language_code`` registration — which serves
  English for any client UI outside ``{uk, ru, be}``.

This script walks ``user_profiles`` once and posts the right chat-scoped
command list for each row. Idempotent — safe to re-run.

Usage::

    .venv/bin/python scripts/backfill_chat_commands.py
    .venv/bin/python scripts/backfill_chat_commands.py --dry-run

Reads ``TELEGRAM_BOT_TOKEN`` and ``DATABASE_URL`` from env / ``.env``.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

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
from lib.database import get_conn


def _post(token: str, payload: dict) -> dict:
    base = f"https://api.telegram.org/bot{token}"
    try:
        resp = httpx.post(f"{base}/setMyCommands", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _is_unreachable(body: dict) -> bool:
    """True if Telegram says we can't talk to this user (blocked, deleted,
    chat gone). Treat as ``skipped`` rather than ``failed`` — these aren't
    bugs, just stale rows that the bot can no longer reach."""
    if body.get("ok"):
        return False
    desc = (body.get("description") or "").lower()
    return any(
        marker in desc
        for marker in (
            "forbidden",
            "bot was blocked",
            "user is deactivated",
            "chat not found",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List rows we would update without calling Telegram.",
    )
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and not args.dry_run:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in env / .env", file=sys.stderr)
        return 1

    # Render the command list per locale once, not per user.
    payload_for_lang: dict[str, list[dict]] = {}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, lang FROM user_profiles ORDER BY user_id"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    print(
        f"Found {len(rows)} user_profiles rows; "
        f"backfilling chat-scoped /command menus…"
    )

    ok_count = 0
    skipped_count = 0
    failed_count = 0

    for user_id, lang in rows:
        # Defensive: any unexpected value in the lang column → 'en'.
        normalized_lang = lang if lang in ("en", "uk") else "en"
        if normalized_lang not in payload_for_lang:
            payload_for_lang[normalized_lang] = build_commands(
                locale=normalized_lang
            )

        payload = {
            "commands": payload_for_lang[normalized_lang],
            "scope": {"type": "chat", "chat_id": int(user_id)},
        }

        if args.dry_run:
            print(f"  [dry-run] user_id={user_id} lang={normalized_lang}")
            ok_count += 1
            continue

        body = _post(token, payload)
        if body.get("ok"):
            ok_count += 1
        elif _is_unreachable(body):
            skipped_count += 1
            print(
                f"  user_id={user_id}: skipped — {body.get('description')}"
            )
        else:
            failed_count += 1
            print(f"  user_id={user_id}: FAILED — {body}", file=sys.stderr)

        # Stay under Telegram's 30-req/sec global ceiling. ~25 req/s
        # leaves headroom for any concurrent webhook traffic.
        time.sleep(0.04)

    print()
    print(
        f"Done. OK: {ok_count}  "
        f"SKIPPED (unreachable): {skipped_count}  "
        f"FAILED: {failed_count}"
    )
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
