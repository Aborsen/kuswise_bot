"""Canonical command-list registry for ``setMyCommands`` calls.

Single source of truth for the slash-command menu Telegram clients show
when the user types ``/`` in chat. Both setup scripts
(scripts/set_webhook.py and scripts/setup_bot_commands.py) read from
here.

Display order matches the order in ``COMMAND_NAMES``. Descriptions live
in lib/i18n/dict_<locale>.json under ``cmd.<name>`` so the same list
renders correctly per locale via ``setMyCommands(language_code=…)``.
"""
from __future__ import annotations

from lib.i18n import t


# Display order in the Telegram `/` autocomplete. Trim or reorder here
# rather than in the setup scripts.
COMMAND_NAMES: tuple[str, ...] = (
    "start",
    "today",
    "yesterday",
    "history",
    "streak",
    "goals",
    "recap",
    "scan",
    "menu",
    "plan",
    "suggest_meal",
    "fav",
    "recent",
    "water",
    "aliases",
    "ai",
    "ask",
    "ask_new",
    "recipes",
    "health",
    "language",
    "timezone",
    "profile",
    "quiet",
    "cancel",
    "help",
)


def build_commands(locale: str = "en") -> list[dict]:
    """Render the command list for one locale, ready to ship to setMyCommands.

    Each entry is ``{"command": "<name>", "description": "<localized>"}``.
    """
    return [
        {"command": name, "description": t(f"cmd.{name}", locale=locale)}
        for name in COMMAND_NAMES
    ]
