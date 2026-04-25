"""Structured JSON logging + Sentry wiring for serverless handlers.

Usage in an api/*.py:

    from lib.log import setup_sentry, http_handler, info, error
    setup_sentry("webhook")  # at module load

    class handler(BaseHTTPRequestHandler):
        @http_handler("webhook")
        def do_POST(self):
            info("webhook_received", path=self.path)
            ...

The decorator generates a request_id, runs the body, captures uncaught
exceptions to Sentry + structured logs, and sends a generic 500.
"""
import json
import os
import sys
import time
import traceback
import uuid
from contextvars import ContextVar
from typing import Any, Optional


_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_sentry_initialized: bool = False


def new_request_id() -> str:
    """Generate a fresh request ID, store it in the contextvar, return it."""
    rid = uuid.uuid4().hex[:12]
    _request_id_var.set(rid)
    return rid


def get_request_id() -> str:
    return _request_id_var.get()


def setup_sentry(component: str) -> None:
    """Initialise Sentry once per process. Safe to call repeatedly.

    No-op when SENTRY_DSN is unset (local dev) or sentry-sdk isn't installed.
    """
    global _sentry_initialized
    if _sentry_initialized:
        return
    _sentry_initialized = True

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            environment=os.environ.get("VERCEL_ENV", "development"),
            release=os.environ.get("VERCEL_GIT_COMMIT_SHA"),
            send_default_pii=False,
        )
        sentry_sdk.set_tag("component", component)
    except Exception:
        # Never let observability break the request path.
        pass


def _emit(level: str, event: str, fields: dict) -> None:
    payload = {
        "ts": time.time(),
        "level": level,
        "event": event,
        "request_id": _request_id_var.get(),
        **fields,
    }
    try:
        sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def info(event: str, **fields: Any) -> None:
    _emit("info", event, fields)


def warn(event: str, **fields: Any) -> None:
    _emit("warn", event, fields)


def error(event: str, exc: Optional[BaseException] = None, **fields: Any) -> None:
    """Log an error and (when given an exception) capture it to Sentry."""
    if exc is not None:
        fields.setdefault("error_type", type(exc).__name__)
        fields.setdefault("error", str(exc) or repr(exc))
        fields.setdefault(
            "traceback",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        _capture_exception(exc, event, fields)
    _emit("error", event, fields)


def _capture_exception(exc: BaseException, event: str, fields: dict) -> None:
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        # sentry-sdk 2.x uses `new_scope()` instead of the deprecated `push_scope()`.
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("event", event)
            rid = _request_id_var.get()
            if rid:
                scope.set_tag("request_id", rid)
            for k, v in fields.items():
                if k in ("error", "error_type", "traceback"):
                    continue
                try:
                    scope.set_extra(k, v)
                except Exception:
                    pass
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def http_handler(component: str):
    """Decorator for BaseHTTPRequestHandler.do_GET / do_POST methods.

    - Generates a fresh request_id for the call.
    - Runs the inner handler.
    - On uncaught exception: logs + Sentry-captures, then sends a 500 with
      a small JSON body and an X-Request-Id header so support can correlate.
    """
    def decorator(func):
        def wrapper(self, *args, **kwargs):
            new_request_id()
            try:
                return func(self, *args, **kwargs)
            except Exception as exc:
                error(
                    f"{component}_uncaught",
                    exc=exc,
                    method=func.__name__,
                )
                try:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-Request-Id", get_request_id())
                    self.end_headers()
                    self.wfile.write(b'{"ok":false,"error":"internal_error"}')
                except Exception:
                    pass
        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = getattr(func, "__qualname__", func.__name__)
        wrapper.__wrapped__ = func
        return wrapper
    return decorator
