"""Telegram WebApp barcode scanner page (F-8).

Serves a tiny self-contained HTML page that:

1. Reads ``Telegram.WebApp.initData`` from the surrounding WebApp shell.
2. Loads ``html5-qrcode`` from a CDN (universal scanner — Chromium uses
   the native ``BarcodeDetector`` under the hood, Safari falls back to a
   pure-JS jsQR loop).
3. Asks for camera permission, opens the rear camera, and decodes EAN-8/13
   + UPC-A/E (the formats html5-qrcode supports natively).
4. POSTs the decoded ``ean`` + the original ``initData`` to ``/api/barcode``
   so the server can verify and look up Open Food Facts.
5. Closes the WebApp once the bot has sent the portion-picker message back
   to the chat.

The HTML is deliberately tiny + framework-free so cold starts stay cheap.
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
                "connect-src 'self' https://telegram.org; "
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
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: var(--tg-theme-bg-color, #111);
    color: var(--tg-theme-text-color, #eee);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    overflow: hidden;
  }
  #wrap { display: flex; flex-direction: column; height: 100%; }
  header { padding: 12px; text-align: center; font-weight: 600; }
  #reader { flex: 1; min-height: 0; position: relative; }
  #reader video { width: 100% !important; height: 100% !important; object-fit: cover; }
  footer { padding: 12px; text-align: center; }
  #status { padding: 8px 12px; font-size: 14px; opacity: 0.85; min-height: 1.4em; text-align: center; }
  button {
    background: var(--tg-theme-button-color, #2481cc);
    color: var(--tg-theme-button-text-color, #fff);
    border: 0; border-radius: 8px; padding: 10px 16px; font-size: 15px;
  }
  button[disabled] { opacity: 0.5; }
  .err { color: #ff6b6b; }
</style>
</head>
<body>
<div id="wrap">
  <header>📷 Наведи на штрих-код</header>
  <div id="reader"></div>
  <div id="status">Завантажую сканер…</div>
  <footer>
    <button id="cancel">Скасувати</button>
  </footer>
</div>

<script src="__CDN__" nonce="__NONCE__"></script>
<script nonce="__NONCE__">
(function () {
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }

  var statusEl = document.getElementById('status');
  var cancelBtn = document.getElementById('cancel');
  cancelBtn.addEventListener('click', function () {
    if (tg) tg.close();
  });

  function setStatus(msg, isErr) {
    statusEl.textContent = msg;
    statusEl.className = isErr ? 'err' : '';
  }

  function getInitData() {
    if (tg && tg.initData && tg.initData.length > 0) return tg.initData;
    return null;
  }

  var initData = getInitData();
  var attempts = 0;
  function waitForInit() {
    initData = getInitData();
    if (initData) { startScanner(); return; }
    attempts++;
    if (attempts > 20) {
      setStatus('Не вдалось отримати ідентифікатор. Спробуй перезайти в бота.', true);
      return;
    }
    setTimeout(waitForInit, 100);
  }
  waitForInit();

  var sent = false;
  function postEan(ean) {
    if (sent) return; sent = true;
    setStatus('✓ Знайдено: ' + ean + '. Шукаю продукт…');
    var form = new FormData();
    form.append('ean', ean);
    form.append('initData', initData || '');
    fetch('/api/barcode', { method: 'POST', body: form })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (j) {
        if (j && j.ok) {
          setStatus('✓ Готово — продовжуй у чаті бота.');
        } else {
          setStatus(j && j.error ? j.error : 'Не зміг розпізнати продукт. Спробуй ще раз або введи назву в боті.', true);
          sent = false; // allow retry
          return;
        }
        if (tg) setTimeout(function () { tg.close(); }, 800);
      })
      .catch(function () {
        setStatus('Помилка мережі. Спробуй ще раз.', true);
        sent = false;
      });
  }

  function startScanner() {
    if (typeof Html5Qrcode === 'undefined') {
      setStatus('Не вдалось завантажити сканер.', true);
      return;
    }
    setStatus('Дозволь камеру у Telegram.');
    var scanner = new Html5Qrcode("reader");
    var formats = [
      Html5QrcodeSupportedFormats && Html5QrcodeSupportedFormats.EAN_13,
      Html5QrcodeSupportedFormats && Html5QrcodeSupportedFormats.EAN_8,
      Html5QrcodeSupportedFormats && Html5QrcodeSupportedFormats.UPC_A,
      Html5QrcodeSupportedFormats && Html5QrcodeSupportedFormats.UPC_E,
    ].filter(Boolean);

    scanner.start(
      { facingMode: 'environment' },
      { fps: 10, qrbox: { width: 240, height: 140 }, formatsToSupport: formats },
      function (decodedText) {
        scanner.stop().catch(function () {});
        var clean = String(decodedText || '').replace(/\D/g, '');
        if (!/^\d{8,13}$/.test(clean)) {
          setStatus('Це не схоже на штрих-код товару.', true);
          return;
        }
        postEan(clean);
      },
      function (_err) { /* ignore per-frame decode misses */ }
    ).then(function () {
      setStatus('Шукаю штрих-код…');
    }).catch(function (err) {
      setStatus('Камера недоступна: ' + (err && err.message || err), true);
    });
  }
})();
</script>
</body>
</html>
"""
