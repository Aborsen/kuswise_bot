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
"""
from __future__ import annotations

import os
import secrets
from http.server import BaseHTTPRequestHandler

import sys
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.log import setup_sentry, http_handler

setup_sentry("scan")


# Pin html5-qrcode to a known-good version. ~25 KB gzipped on the wire.
_HTML5QR_CDN = "https://cdn.jsdelivr.net/npm/html5-qrcode@2.3.8/html5-qrcode.min.js"


class handler(BaseHTTPRequestHandler):
    @http_handler("scan")
    def do_GET(self):
        nonce = secrets.token_urlsafe(16)
        body = _SCAN_HTML.replace("__NONCE__", nonce).replace("__CDN__", _HTML5QR_CDN)
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
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>KusWise — Сканер</title>
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
  <header>📷 Сканер штрих-кодів</header>
  <div id="banner">Готую сканер…</div>

  <div id="stage">
    <div id="reader"></div>
    <div id="splash">
      <p id="splashHint">
        Натисни кнопку нижче — Telegram попросить дозвіл на камеру.
        Наведи на штрих-код товару, я знайду його у Open Food Facts.
      </p>
      <button id="startBtn" class="btn-primary" disabled>📷 Увімкнути камеру</button>
      <button id="manualLink">✏️ Ввести цифри вручну</button>
    </div>
  </div>

  <footer>
    <button id="cancel">Закрити</button>
  </footer>
</div>

<script src="__CDN__" nonce="__NONCE__"></script>
<script nonce="__NONCE__">
(function () {
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
      setBanner('Готово — натисни «Увімкнути камеру».', 'ok');
      return;
    }
    initAttempts++;
    if (initAttempts > 30) {  // ~3s
      setBanner('Не отримав ідентифікатор від Telegram. Закрий і відкрий сканер ще раз.', 'err');
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
    var ean = window.prompt('Введи штрих-код (8-13 цифр):', '');
    if (ean == null) return;
    var clean = String(ean).replace(/\D/g, '');
    if (!/^\d{8,13}$/.test(clean)) {
      setBanner('Штрих-код має бути 8-13 цифр.', 'err');
      return;
    }
    postEan(clean);
  });

  // ---------- POST to /api/barcode ----------
  var sent = false;
  function postEan(ean) {
    if (sent) return; sent = true;
    setBanner('Знайшов: ' + ean + '. Шукаю продукт…', 'ok');
    if (!initData) {
      setBanner('Не отримав ідентифікатор від Telegram. Закрий і відкрий ще раз.', 'err');
      sent = false;
      return;
    }
    var form = new FormData();
    form.append('ean', ean);
    form.append('initData', initData);
    fetch('/api/barcode', { method: 'POST', body: form })
      .then(function (r) {
        return r.json().catch(function () { return {}; });
      })
      .then(function (j) {
        if (j && j.ok) {
          setBanner('✓ Готово — продовжуй у чаті бота.', 'ok');
          if (tg) setTimeout(function () { try { tg.close(); } catch (_) {} }, 800);
          return;
        }
        var msg = (j && j.error) ? j.error : 'Не зміг розпізнати продукт. Спробуй ще раз або введи назву в боті.';
        setBanner(msg, 'err');
        sent = false;  // allow retry
      })
      .catch(function (e) {
        setBanner('Помилка мережі. Спробуй ще раз.', 'err');
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
      setBanner('Сканер не завантажився. Спробуй ще раз або введи код вручну.', 'err');
      debugAlert('Html5Qrcode is undefined — CDN load failed');
      return;
    }
    els.startBtn.disabled = true;
    setBanner('Дозволь доступ до камери у спливаючому вікні…', '');

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
          setBanner('Це не схоже на штрих-код товару — спробуй ще раз.', 'err');
          els.startBtn.disabled = false;
          return;
        }
        postEan(clean);
      },
      function (_decodeErr) { /* per-frame decode misses are normal — ignore */ }
    ).then(function () {
      els.splash.classList.add('hidden');
      setBanner('Шукаю штрих-код у кадрі…', '');
    }).catch(function (err) {
      var raw = (err && err.message) || (err && err.name) || String(err);
      var hint;
      if (/NotAllowedError|Permission|denied/i.test(raw)) {
        hint = 'Доступ до камери заборонено. iOS: Налаштування → Telegram → Камера. Або введи код цифрами вручну.';
      } else if (/NotFoundError|NotReadableError|OverconstrainedError/i.test(raw)) {
        hint = 'Камера недоступна на цьому пристрої. Введи код цифрами вручну.';
      } else if (/NotSupportedError|secure context/i.test(raw)) {
        hint = 'Цей браузер не підтримує камеру. Введи код цифрами вручну.';
      } else {
        hint = 'Камера: ' + raw + '. Спробуй ще раз або введи цифри вручну.';
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
