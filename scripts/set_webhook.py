"""One-time setup: register the Telegram webhook and the bot's command menu.

Run locally after deploying to Vercel:
    python scripts/set_webhook.py

Requires these in .env:
    TELEGRAM_BOT_TOKEN
    WEBHOOK_SECRET
    VERCEL_URL          (e.g. your-app.vercel.app — no https://, no trailing slash)
"""
import os
import sys

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Make ``lib.*`` importable when run from anywhere in the repo.
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.bot_commands import build_commands


COMMANDS_EN = build_commands(locale="en")
COMMANDS_UA = build_commands(locale="uk")


def _post(token: str, method: str, payload: dict) -> dict:
    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=15,
    )
    return resp.json()


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    secret = os.getenv("WEBHOOK_SECRET")
    vercel_url = os.getenv("VERCEL_URL")

    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": token,
        "WEBHOOK_SECRET": secret,
        "VERCEL_URL": vercel_url,
    }.items() if not v]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    vercel_url = vercel_url.replace("https://", "").replace("http://", "").rstrip("/")
    webhook_url = f"https://{vercel_url}/api/webhook"

    # 1. Register webhook
    wh = _post(token, "setWebhook", {
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
    })
    print(f"→ setWebhook to {webhook_url}")
    print(" ", wh)
    if not wh.get("ok"):
        return 2

    # 2. Register command menus (Ukrainian + English fallback)
    cm_ua = _post(token, "setMyCommands", {"commands": COMMANDS_UA, "language_code": "uk"})
    print("→ setMyCommands (uk)")
    print(" ", cm_ua)

    cm_default = _post(token, "setMyCommands", {"commands": COMMANDS_EN})
    print("→ setMyCommands (default / English fallback)")
    print(" ", cm_default)

    if not cm_ua.get("ok") or not cm_default.get("ok"):
        return 3

    # 3. Register the Mini App chat menu button (persistent; replaces '/' menu).
    # This is the only launch mode that provides signed initData for user auth.
    sha = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:8]
    suffix = f"?v={sha}" if sha else ""
    dashboard_url = f"https://{vercel_url}/api/dashboard{suffix}"
    mb = _post(token, "setChatMenuButton", {
        "menu_button": {
            "type": "web_app",
            "text": "📱 Dashboard",
            "web_app": {"url": dashboard_url},
        }
    })
    print("→ setChatMenuButton (Dashboard Mini App)")
    print(" ", mb)
    if not mb.get("ok"):
        return 4

    print("\n✅ Готово! У Telegram натисни кнопку «Меню» зліва знизу — команди відобразяться.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
