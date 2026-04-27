"""Telegram WebApp barcode scanner page (F-8).

Serves a self-contained HTML page that:

1. Reads ``Telegram.WebApp.initData`` from the surrounding WebApp shell.
2. Loads ``html5-qrcode`` from a CDN (universal scanner).
3. **Waits for an explicit user tap** before requesting camera access — iOS
   WKWebView (Telegram on iPhone) only fires the camera permission prompt
   when ``getUserMedia()`` is called from a direct user-initiated event
   handler. Auto-starting from a setTimeout chain silently fails on iOS.
4. POSTs the decoded ``ean`` + the original ``initData`` to ``/api/barcode``.
5. Closes the WebApp once the bot has sent the portion-picker message.

The page also surfaces a clear "Type EAN manually" link as a fallback for
devices/permissions that block the camera path entirely.

F-2b Chunk 7: locale (uk / en) is read from the ``?lang=`` query string and
piped to all UA / EN labels rendered by the page. The bot's
``scanner_inline_keyboard`` already appends the user's locale to the URL.
"""
from __future__ import annotations

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs

import sys
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.log import setup_sentry, http_handler
from lib.i18n import t as _i18n_t

setup_sentry("scan")


# Pin html5-qrcode to a known-good version. ~25 KB gzipped on the wire.
_HTML5QR_CDN = "https://cdn.jsdelivr.net/npm/html5-qrcode@2.3.8/html5-qrcode.min.js"


# Keys whose values are loaded into the page's ``window._L`` JS object so the
# inline script can render dynamic banners / errors in the user's locale.
# Server-side static labels (header, splash hint, button text) are interpolated
# via ``__LABEL_FOO__`` placeholders below.
_SCAN_JS_KEYS = (
    "scan.banner_preparing",
    "scan.banner_ready",
    "scan.banner_no_init",
    "scan.manual_prompt",
    "scan.invalid_digits",
    "scan.searching",
    "scan.done",
    "scan.unrecognized",
    "scan.network_error",
    "scan.scanner_failed",
    "scan.allow_camera",
    "scan.not_a_barcode",
    "scan.searching_in_frame",
    "scan.cam_denied",
    "scan.cam_unavailable",
    "scan.cam_unsupported",
    "scan.cam_other",
)


def _locale_from_query(path: str) -> str:
    """Pick a supported locale from the URL's ``?lang=`` query, defaulting to en."""
    try:
        qs = urlsplit(path).query
        params = parse_qs(qs)
        candidate = (params.get("lang") or [""])[0].lower()
        if candidate in ("uk", "en"):
            return candidate
    except Exception:
        pass
    return "en"


def _build_js_labels(locale: str) -> str:
    """Serialize the JS-side labels dict as a JSON string ready to inject."""
    labels = {}
    for key in _SCAN_JS_KEYS:
        short = key.split(".", 1)[1]
        labels[short] = _i18n_t(key, locale=locale)
    return json.dumps(labels, ensure_ascii=False)


class handler(BaseHTTPRequestHandler):
    @http_handler("scan")
    def do_GET(self):
        nonce = secrets.token_urlsafe(16)
        locale = _locale_from_query(self.path)
        body = (
            _SCAN_HTML
            .replace("__NONCE__", nonce)
            .replace("__CDN__", _HTML5QR_CDN)
            .replace("__LANG__", locale)
            .replace("__LABEL_TITLE__",        _i18n_t("scan.title",        locale=locale))
            .replace("__LABEL_HEADER__",       _i18n_t("scan.header",       locale=locale))
            .replace("__LABEL_BANNER_INIT__",  _i18n_t("scan.banner_preparing", locale=locale))
            .replace("__LABEL_SPLASH_HINT__",  _i18n_t("scan.splash_hint",  locale=locale))
            .replace("__LABEL_START_BTN__",    _i18n_t("scan.start_btn",    locale=locale))
            .replace("__LABEL_MANUAL_LINK__",  _i18n_t("scan.manual_link",  locale=locale))
            .replace("__LABEL_CANCEL_BTN__",   _i18n_t("scan.cancel_btn",   locale=locale))
            .replace("__JS_LABELS__",          _build_js_labels(locale))
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # CSP: lock script execution to our nonce + the html5-qrcode CDN.
        # `connect-src` allows POSTing to /api/barcode (same-origin).
        self.send_header(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https:; "
                "media-src 'self' blob:; "
                "connect-src 'self' https://cdn.jsdelivr.net https://telegram.org; "
                "frame-ancestors 'self' https://web.telegram.org https://telegram.org;"
            ),
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


_SCAN_HTML = r"""<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__LABEL_TITLE__</title>
<link rel="icon" type="image/png" href="/logo.png">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #111);
    --fg: var(--tg-theme-text-color, #eee);
    --btn: var(--tg-theme-button-color, #2481cc);
    --btn-fg: var(--tg-theme-button-text-color, #fff);
    --link: var(--tg-theme-link-color, #5eb1ff);
    --hint: var(--tg-theme-hint-color, #999);
  }
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: var(--bg); color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    overflow: hidden;
  }
  #wrap { display: flex; flex-direction: column; height: 100%; }
  header { padding: 12px; text-align: center; font-weight: 600; }
  /* Status banner — FULL-width, high-contrast, top-of-camera. */
  #banner {
    padding: 10px 14px; font-size: 14px; line-height: 1.35;
    text-align: center; min-height: 1.4em;
    background: rgba(255,255,255,0.06); color: var(--fg);
  }
  #banner.err { background: #5a1f1f; color: #ffd7d7; }
  #banner.ok  { background: #1f4a1f; color: #d2ffd2; }
  /* Camera / start-screen container */
  #stage { flex: 1; min-height: 0; position: relative; display: flex; align-items: center; justify-content: center; }
  #reader { width: 100%; height: 100%; }
  #reader video { width: 100% !important; height: 100% !important; object-fit: cover; }
  /* The "tap to start" splash. Removed once camera starts. */
  #splash {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; padding: 24px; gap: 16px;
    text-align: center;
  }
  #splash p { margin: 0; max-width: 320px; opacity: 0.85; }
  .btn-primary {
    background: var(--btn); color: var(--btn-fg);
    border: 0; border-radius: 10px; padding: 14px 22px;
    font-size: 17px; font-weight: 600; cursor: pointer;
    min-width: 240px;
  }
  .btn-primary[disabled] { opacity: 0.5; }
  footer { padding: 10px 14px; display: flex; gap: 8px; justify-content: space-between; align-items: center; }
  #manualLink { color: var(--link); text-decoration: underline; font-size: 14px; cursor: pointer; background: none; border: 0; padding: 0; }
  #cancel { background: transparent; color: var(--fg); border: 0; font-size: 14px; cursor: pointer; opacity: 0.7; }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div id="wrap">
  <header>__LABEL_HEADER__</header>
  <div id="banner">__LABEL_BANNER_INIT__</div>

  <div id="stage">
    <div id="reader"></div>
    <div id="splash">
      <p id="splashHint">__LABEL_SPLASH_HINT__</p>
      <button id="startBtn" class="btn-primary" disabled>__LABEL_START_BTN__</button>
      <button id="manualLink">__LABEL_MANUAL_LINK__</button>
    </div>
  </div>

  <footer>
    <button id="cancel">__LABEL_CANCEL_BTN__</button>
  </footer>
</div>

<script src="__CDN__" nonce="__NONCE__"></script>
<script nonce="__NONCE__">
(function () {
  var L = __JS_LABELS__;
  function fmt(s, vars) {
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, function (_, k) {
      return vars[k] !== undefined ? vars[k] : ('{' + k + '}');
    });
  }

  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { try { tg.ready(); tg.expand(); } catch (_) {} }

  var qs = (window.location.search || "");
  var debugMode = qs.indexOf("diag=1") !== -1;

  var els = {
    banner:     document.getElementById('banner'),
    stage:      document.getElementById('stage'),
    splash:     document.getElementById('splash'),
    splashHint: document.getElementById('splashHint'),
    startBtn:   document.getElementById('startBtn'),
    manualLink: document.getElementById('manualLink'),
    cancelBtn:  document.getElementById('cancel'),
    reader:     document.getElementById('reader'),
  };

  function setBanner(msg, kind) {
    els.banner.textContent = msg;
    els.banner.className = kind || '';
  }
  function debugAlert(msg) {
    if (debugMode) { try { window.alert(msg); } catch (_) {} }
  }

  // ---------- Init data wait ----------
  var initData = null;
  var initAttempts = 0;
  function pollInit() {
    if (tg && tg.initData && tg.initData.length > 0) {
      initData = tg.initData;
      els.startBtn.disabled = false;
      setBanner(L.banner_ready, 'ok');
      return;
    }
    initAttempts++;
    if (initAttempts > 30) {  // ~3s
      setBanner(L.banner_no_init, 'err');
      return;
    }
    setTimeout(pollInit, 100);
  }
  pollInit();

  // ---------- Cancel button ----------
  els.cancelBtn.addEventListener('click', function () {
    if (tg) { try { tg.close(); } catch (_) {} }
    else { window.history.back(); }
  });

  // ---------- Manual entry ----------
  els.manualLink.addEventListener('click', function () {
    var ean = window.prompt(L.manual_prompt, '');
    if (ean == null) return;
    var clean = String(ean).replace(/\D/g, '');
    if (!/^\d{8,13}$/.test(clean)) {
      setBanner(L.invalid_digits, 'err');
      return;
    }
    postEan(clean);
  });

  // ---------- POST to /api/barcode ----------
  var sent = false;
  function postEan(ean) {
    if (sent) return; sent = true;
    setBanner(fmt(L.searching, {ean: ean}), 'ok');
    if (!initData) {
      setBanner(L.banner_no_init, 'err');
      sent = false;
      return;
    }
    // Use URL-encoded form (parse_qs server-side) instead of multipart/FormData
    // — multipart parsing was occasionally returning empty `ean`, which the
    // server then rejected as "Bad EAN".
    var body = 'ean=' + encodeURIComponent(ean) +
               '&initData=' + encodeURIComponent(initData);
    fetch('/api/barcode', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: body,
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; });
      })
      .then(function (j) {
        if (j && j.ok) {
          setBanner(L.done, 'ok');
          if (tg) setTimeout(function () { try { tg.close(); } catch (_) {} }, 800);
          return;
        }
        var msg = (j && j.error) ? j.error : L.unrecognized;
        setBanner(msg, 'err');
        sent = false;  // allow retry
      })
      .catch(function (e) {
        setBanner(L.network_error, 'err');
        debugAlert('fetch error: ' + (e && e.message || e));
        sent = false;
      });
  }

  // ---------- Start camera (user-gesture handler) ----------
  // Critical: scanner.start() MUST be called inside this synchronous click
  // handler so iOS WKWebView treats it as a user-initiated gesture. Don't
  // wrap it in a Promise / setTimeout / requestAnimationFrame — the gesture
  // token gets lost across an async boundary.
  els.startBtn.addEventListener('click', function () {
    if (typeof Html5Qrcode === 'undefined') {
      setBanner(L.scanner_failed, 'err');
      debugAlert('Html5Qrcode is undefined — CDN load failed');
      return;
    }
    els.startBtn.disabled = true;
    setBanner(L.allow_camera, '');

    var formats = (typeof Html5QrcodeSupportedFormats !== 'undefined') ? [
      Html5QrcodeSupportedFormats.EAN_13,
      Html5QrcodeSupportedFormats.EAN_8,
      Html5QrcodeSupportedFormats.UPC_A,
      Html5QrcodeSupportedFormats.UPC_E,
    ] : undefined;

    var scanner = new Html5Qrcode("reader");
    scanner.start(
      { facingMode: 'environment' },
      { fps: 10, qrbox: { width: 240, height: 140 }, formatsToSupport: formats },
      function (decodedText) {
        scanner.stop().catch(function () {});
        var clean = String(decodedText || '').replace(/\D/g, '');
        if (!/^\d{8,13}$/.test(clean)) {
          setBanner(L.not_a_barcode, 'err');
          els.startBtn.disabled = false;
          return;
        }
        postEan(clean);
      },
      function (_decodeErr) { /* per-frame decode misses are normal — ignore */ }
    ).then(function () {
      els.splash.classList.add('hidden');
      setBanner(L.searching_in_frame, '');
    }).catch(function (err) {
      var raw = (err && err.message) || (err && err.name) || String(err);
      var hint;
      if (/NotAllowedError|Permission|denied/i.test(raw)) {
        hint = L.cam_denied;
      } else if (/NotFoundError|NotReadableError|OverconstrainedError/i.test(raw)) {
        hint = L.cam_unavailable;
      } else if (/NotSupportedError|secure context/i.test(raw)) {
        hint = L.cam_unsupported;
      } else {
        hint = fmt(L.cam_other, {raw: raw});
      }
      setBanner(hint, 'err');
      els.startBtn.disabled = false;
      debugAlert('camera error: ' + raw);
    });
  });

  // Surface unhandled JS errors in debug mode.
  window.addEventListener('error', function (e) {
    debugAlert('window error: ' + (e.message || e.error));
  });
  window.addEventListener('unhandledrejection', function (e) {
    debugAlert('unhandled: ' + (e.reason && e.reason.message || e.reason));
  });
})();
</script>
</body>
</html>
"""
