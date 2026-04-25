"""Pytest config for kuswise_bot.

Adds the repo root to sys.path so tests can ``import lib`` directly without
installing the project. Provides a few light helpers used by smoke tests.
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# Don't let an inherited DATABASE_URL accidentally point tests at a real DB.
# Tests should never need it; failing imports loudly is better than silent
# leaks against staging.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("CRON_SECRET", "test-cron-secret")
