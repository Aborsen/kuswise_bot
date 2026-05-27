#!/usr/bin/env python3
"""One-shot: post the missed admin-channel notifications for the 7 users
finalised today by `backfill_finish_onboarding.py`.

Why this exists
---------------
The earlier finish-line backfill (commit 4978600) replicated the
user-side welcome message but never called `_notify_admin_new_user`.
Hence: the 7 users got their "🎉 All set!" messages, but the
ADMIN_NOTIFY_CHAT_ID channel never saw their notifications.

This script targets those 7 user_ids specifically and posts the
standard `format_new_user_notification` for each, then stamps the new
`admin_notified_at` column so re-runs are no-ops. Going forward the
column is populated by `_finalize_onboarding` on the normal happy
path, so this script is a one-time bridge.

Usage::

    .venv/bin/python scripts/backfill_admin_notifications.py
    .venv/bin/python scripts/backfill_admin_notifications.py --dry-run

Reads ``TELEGRAM_BOT_TOKEN``, ``ADMIN_NOTIFY_CHAT_ID``, and
``DATABASE_URL`` from env / ``.env``.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

from lib.config import ADMIN_NOTIFY_CHAT_ID
from lib.database import get_conn, get_profile, update_profile
from lib.formatters import format_new_user_notification
from lib.telegram_helpers import send_message


# The 7 users finalised by backfill_finish_onboarding.py on 2026-05-27
# whose admin-channel notification never fired. Hard-coded rather than
# computed from a query because the cohort is closed — anyone newer goes
# through the normal `_finalize_onboarding` path which stamps
# `admin_notified_at` correctly.
_BACKFILL_USER_IDS = [
    636601703,   # Olimp🏆     @Olimpic103   site_banner_home_uk  uk  2330 kcal
    669699156,   # Анютка       @Ank_O6        (organic)            en  2970 kcal
    884541258,   # Veronika    @nniikson     site_banner_home_uk  uk  2231 kcal
    973072558,   # Trueyoung   @Yarik341     (organic)            uk  3098 kcal
    1334393182,  # Nina        —             (organic)            uk  2231 kcal
    1385497508,  # Богдан       @dbodik       (organic)            uk  4006 kcal
    5520842561,  # Оля          @ollishka99   (organic)            uk  2408 kcal
]

# Inter-send delay — 7 messages, Telegram cap is 30 msg/s. Matches
# pacing of the morning + finish-line backfill scripts.
_SEND_DELAY_S = 0.04


def _fetch_username(conn, user_id: int) -> str:
    """Telegram handle without the @ prefix. Empty when the user
    didn't set one."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE user_id = %s",
                        (user_id,))
            row = cur.fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _fetch_first_name(conn, user_id: int) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT first_name FROM users WHERE user_id = %s",
                        (user_id,))
            row = cur.fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


def _post_one(conn, user_id: int, dry_run: bool) -> str:
    """Post the admin-channel notification for one user. Returns a
    one-line operator log string."""
    profile = get_profile(conn, user_id)
    if not profile:
        return f"! uid={user_id:>10} skipped (no profile row)"

    if profile.get("admin_notified_at"):
        return (f"⏭ uid={user_id:>10} already notified at "
                f"{profile['admin_notified_at']}")

    username = _fetch_username(conn, user_id)
    first_name = _fetch_first_name(conn, user_id)

    if dry_run:
        return (f"DRY uid={user_id:>10} ({profile.get('lang')}) would "
                f"post {first_name!r} @{username!r}")

    if not ADMIN_NOTIFY_CHAT_ID:
        return f"! uid={user_id:>10} skipped (ADMIN_NOTIFY_CHAT_ID unset)"
    try:
        chat_id = int(ADMIN_NOTIFY_CHAT_ID)
    except (TypeError, ValueError):
        return f"! uid={user_id:>10} skipped (invalid ADMIN_NOTIFY_CHAT_ID)"

    text = format_new_user_notification(profile, username, first_name)
    resp = send_message(chat_id, text)
    if not (isinstance(resp, dict) and resp.get("ok")):
        return (f"⚠ uid={user_id:>10} send failed: "
                f"{str(resp)[:120]}")

    # Mark notified so re-runs are no-ops.
    update_profile(conn, user_id,
                   admin_notified_at=datetime.now(timezone.utc).isoformat())
    return f"✓ uid={user_id:>10} ({profile.get('lang')}) posted {first_name!r}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be posted without actually sending or "
             "stamping the DB.",
    )
    args = parser.parse_args()

    conn = get_conn()
    posted = 0
    try:
        for uid in _BACKFILL_USER_IDS:
            outcome = _post_one(conn, uid, dry_run=args.dry_run)
            print(outcome, flush=True)
            if outcome.startswith("✓"):
                posted += 1
                time.sleep(_SEND_DELAY_S)
        print(f"\nDone. {posted} admin notifications posted.")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
