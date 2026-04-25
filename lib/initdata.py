"""Telegram WebApp initData verification.

Telegram signs every WebApp launch payload with HMAC-SHA256 keyed by the bot
token. Validating it server-side is the only way to trust the user_id we
read from the Mini App — without this check anyone could POST a forged
``initData`` and act on behalf of any user_id they want.

Used by:
    - api/dashboard.py    (existing)
    - api/barcode.py      (F-8 — scanned EAN)
    - api/scan.py         (F-8 — scanner Mini App page)

Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Optional

from lib.config import TELEGRAM_BOT_TOKEN


# How old a Telegram-signed initData is allowed to be before we reject it.
# 24h matches the existing dashboard handler.
INIT_DATA_MAX_AGE = 60 * 60 * 24


def verify_init_data(init_data: str) -> Optional[dict]:
    """Validate Telegram WebApp initData. Returns the parsed user dict on
    success, or ``None`` for any verification failure.

    The returned dict is whatever Telegram packed into the ``user`` field —
    typically ``{id, first_name, last_name?, username?, language_code?, ...}``.
    Callers should treat ``id`` as the source of truth for the user.
    """
    if not init_data or not TELEGRAM_BOT_TOKEN:
        return None

    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    params = dict(pairs)

    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    try:
        auth_date = int(params.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > INIT_DATA_MAX_AGE:
        return None

    data_check_string = "\n".join(
        f"{k}={params[k]}" for k in sorted(params.keys())
    )

    secret_key = hmac.new(
        b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_json = params.get("user", "")
    if not user_json:
        return None

    try:
        user = json.loads(user_json)
    except (TypeError, ValueError):
        return None

    if not isinstance(user, dict) or not user.get("id"):
        return None

    return user
