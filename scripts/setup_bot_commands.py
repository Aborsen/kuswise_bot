#!/usr/bin/env python3
"""Register the bot's command list with Telegram so the `/` autocomplete
in Telegram clients shows them.

Run this whenever commands change:

    .venv/bin/python scripts/setup_bot_commands.py

It POSTs to ``setMyCommands`` (and optionally ``setChatMenuButton``).
Idempotent — safe to re-run.

Reads ``TELEGRAM_BOT_TOKEN`` from environment / ``.env``.
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


# Each entry shows up in the `/` menu in this exact order. Descriptions
# are clipped to 256 chars by Telegram. Keep them short + scannable.
COMMANDS: list[dict] = [
    {"command": "today",       "description": "📊 Прогрес за сьогодні"},
    {"command": "yesterday",   "description": "📆 Вчорашній день"},
    {"command": "history",     "description": "📈 Історія за 7 днів"},
    {"command": "streak",      "description": "🔥 Серія логів і заморозки"},
    {"command": "goals",       "description": "🎯 Цілі та прогноз досягнення"},
    {"command": "recap",       "description": "📸 Тижнева PNG-картка"},
    {"command": "scan",        "description": "🔢 Сканер штрих-кодів"},
    {"command": "menu",        "description": "📋 OCR меню в кафе/ресторані"},
    {"command": "plan",        "description": "🗓 3-денний план"},
    {"command": "suggest_meal","description": "🍽️ Ідея страви на основі цілей"},
    {"command": "meals",       "description": "📋 Список страв (видалити / змінити)"},
    {"command": "fav",         "description": "⭐ Улюблені страви"},
    {"command": "recent",      "description": "🕘 Останні страви"},
    {"command": "water",       "description": "💧 Облік води"},
    {"command": "aliases",     "description": "📚 Звичні страви (бот вчиться)"},
    {"command": "ask",         "description": "🤖 Запитати ШІ про їжу"},
    {"command": "health",      "description": "🩺 Алергени + хронічні стани"},
    {"command": "language",    "description": "🌐 Мова інтерфейсу"},
    {"command": "timezone",    "description": "🕒 Часовий пояс"},
    {"command": "profile",     "description": "⚙️ Профіль (вага, ціль, вода)"},
    {"command": "cancel",      "description": "❌ Скасувати поточну дію"},
    {"command": "help",        "description": "ℹ️ Допомога"},
    {"command": "start",       "description": "👋 Привітання + меню"},
]


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in env / .env", file=sys.stderr)
        return 1

    base = f"https://api.telegram.org/bot{token}"
    print(f"Registering {len(COMMANDS)} commands…")

    # Register for the default scope (all private + group chats).
    resp = httpx.post(
        f"{base}/setMyCommands",
        json={"commands": COMMANDS},
        timeout=10,
    )
    body = resp.json()
    if not body.get("ok"):
        print(f"setMyCommands failed: {body}", file=sys.stderr)
        return 1
    print(f"  default scope: ok ({body.get('result')})")

    # Also lock the scope explicitly to private chats so DMs see this list
    # even if a future call sets a different default scope.
    resp = httpx.post(
        f"{base}/setMyCommands",
        json={"commands": COMMANDS, "scope": {"type": "all_private_chats"}},
        timeout=10,
    )
    body = resp.json()
    if not body.get("ok"):
        print(f"setMyCommands (private scope) failed: {body}", file=sys.stderr)
        return 1
    print(f"  private scope: ok ({body.get('result')})")

    print("Done. Telegram clients should pick up the new list within a few minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
