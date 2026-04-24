"""Telegram Mini App: read-only per-user dashboard.

Served at /api/dashboard. Opened from the bot's reply-keyboard "Dashboard" button.
Telegram passes signed `initData` which this handler verifies via HMAC-SHA256.

Layout: bottom-nav with 3 tabs (Overview / Meals / Profile), day spinner at the
top for picking a historical date. The page follows Telegram's current theme
(dark or light) via Telegram.WebApp.themeParams.
"""
import hashlib
import hmac
import html
import json
import os
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, date as _date
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import (
    ALLOWED_USER_IDS,
    LOCAL_TZ,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_BOT_USERNAME,
    macro_gram_targets,
    macro_gram_targets_from_profile,
)
from lib.database import (
    get_conn,
    init_db,
    get_profile,
    get_log_for_date,
    get_meals_for_day,
    get_water_for_date,
    get_water_target,
    get_history,
    get_adherence_stats,
    add_water,
    remove_last_water_today,
)


INIT_DATA_MAX_AGE = 24 * 60 * 60  # 24h, per Telegram recommendation
HISTORY_MAX_DAYS = 90              # how far back the day spinner is allowed
PRELOAD_DAYS = 30                  # aggregates sent with the initial render

_SECURITY_HEADERS = [
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store, no-cache, must-revalidate, private, max-age=0"),
    ("CDN-Cache-Control", "no-store"),
    ("Vercel-CDN-Cache-Control", "no-store"),
    ("Surrogate-Control", "no-store"),
    ("Pragma", "no-cache"),
    ("Expires", "0"),
    (
        "Content-Security-Policy",
        "default-src 'none'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://telegram.org; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors https://web.telegram.org https://t.me",
    ),
]


def _verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData. Returns parsed user dict on success."""
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

    data_check_string = "\n".join(f"{k}={params[k]}" for k in sorted(params.keys()))

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
    except Exception:
        return None

    user_id = user.get("id")
    if not isinstance(user_id, int):
        return None

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return None

    return user


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _json_for_script(value) -> str:
    """Serialize for embedding inside a <script> tag.

    Script content is 'script data' — HTML entities are NOT decoded there,
    so we can't html-escape the JSON. Instead, unicode-escape the three chars
    that could break out of or confuse the tag: <, >, &. JSON parsers handle
    \\u00XX fine. This is the same trick Django's `json_script` filter uses.
    """
    s = json.dumps(value, ensure_ascii=False)
    return (
        s.replace("<", "\\u003c")
         .replace(">", "\\u003e")
         .replace("&", "\\u0026")
    )


def _today_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def _is_valid_date_in_range(date_str: str) -> bool:
    try:
        d = _date.fromisoformat(date_str)
    except Exception:
        return False
    today = _date.fromisoformat(_today_str())
    if d > today:
        return False
    if (today - d).days > HISTORY_MAX_DAYS:
        return False
    return True


def _normalize_log(log_dict: dict) -> dict:
    """Coerce the log dict from get_log_for_date() into JSON-safe numbers."""
    return {
        "date": str(log_dict.get("date") or ""),
        "calories": round(log_dict.get("calories") or 0),
        "protein": round(log_dict.get("protein") or 0),
        "carbs": round(log_dict.get("carbs") or 0),
        "fat": round(log_dict.get("fat") or 0),
        "meal_count": int(log_dict.get("meal_count") or 0),
    }


def _meal_to_json(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "meal_type": m.get("meal_type") or "",
        "description": m.get("description") or "",
        "calories": round(m.get("calories") or 0),
        "protein_g": round(m.get("protein_g") or 0),
        "carbs_g": round(m.get("carbs_g") or 0),
        "fat_g": round(m.get("fat_g") or 0),
    }


def _load_day_blob(conn, user_id: int, date_str: str) -> dict:
    log = get_log_for_date(conn, user_id, date_str)
    meals = [_meal_to_json(m) for m in get_meals_for_day(conn, user_id, date_str)]
    water_ml = int(get_water_for_date(conn, user_id, date_str) or 0)
    return {
        "date": date_str,
        "log": _normalize_log({**log, "date": date_str}),
        "meals": meals,
        "water_ml": water_ml,
    }


def _dispatch_action(conn, user_id: int, action: str) -> None:
    if action == "water_add:250":
        add_water(conn, user_id, 250)
    elif action == "water_undo":
        remove_last_water_today(conn, user_id)


class handler(BaseHTTPRequestHandler):
    def _apply_security_headers(self):
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)

    def _send_html(self, code: int, body: str):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._apply_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, code: int, data: dict):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._apply_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send_html(200, _BOOTSTRAP_HTML)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 16 * 1024:
            self._send_html(400, "<h1>Bad request</h1>")
            return

        try:
            raw = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(raw)
            init_data = (form.get("initData") or [""])[0]
            action = (form.get("action") or [""])[0]
            date_param = (form.get("date") or [""])[0]
        except Exception:
            self._send_html(400, "<h1>Bad request</h1>")
            return

        user = _verify_init_data(init_data)
        if user is None:
            if action == "day_data":
                self._send_json(401, {"error": "unauthorized"})
            else:
                self._send_html(401, _unauthorized_html())
            return

        user_id = user["id"]

        # JSON endpoint for historical day fetches
        if action == "day_data":
            if not _is_valid_date_in_range(date_param):
                self._send_json(400, {"error": "invalid_date"})
                return
            conn = get_conn()
            try:
                init_db(conn)
                blob = _load_day_blob(conn, user_id, date_param)
            except Exception:
                print("day_data error:", traceback.format_exc(), flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                self._send_json(500, {"error": "internal"})
                return
            try:
                conn.close()
            except Exception:
                pass
            self._send_json(200, blob)
            return

        # Mutating actions: dispatch then re-render the full dashboard
        if action in ("water_add:250", "water_undo"):
            conn = get_conn()
            try:
                _dispatch_action(conn, user_id, action)
            except Exception:
                print("dashboard action error:", traceback.format_exc(), flush=True)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        try:
            body = _render_dashboard(user)
        except Exception:
            print("dashboard render error:", traceback.format_exc(), flush=True)
            body = "<pre>Dashboard error (see logs)</pre>"
        self._send_html(200, body)


# ---------------------------------------------------------------- Bootstrap --

_BOOTSTRAP_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KusWise Bot</title>
<link rel="icon" type="image/png" href="/logo.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 40px 20px;
         font-family: -apple-system, system-ui, sans-serif;
         background: var(--tg-theme-bg-color, #101014);
         color: var(--tg-theme-text-color, #e6e6ea); text-align: center; }
  .spinner { width: 32px; height: 32px; margin: 20px auto;
             border: 3px solid rgba(127,127,127,0.25);
             border-top-color: var(--tg-theme-button-color, #3ea6ff);
             border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .diag { margin-top: 20px; padding: 12px;
          background: var(--tg-theme-secondary-bg-color, #1b1b22);
          border-radius: 8px;
          font-family: ui-monospace, Menlo, monospace; font-size: 0.75em;
          color: var(--tg-theme-hint-color, #9a9aa6); text-align: left;
          word-break: break-all; }
  .err { color: var(--tg-theme-destructive-text-color, #ef5b5b); }
  button { background: var(--tg-theme-button-color, #3ea6ff);
           color: var(--tg-theme-button-text-color, #fff); border: 0;
           padding: 10px 18px; border-radius: 10px; font-size: 1em;
           margin-top: 14px; cursor: pointer; font-family: inherit; }
</style>
</head>
<body>
<div id="loading">
  <div class="spinner"></div>
  <p>Завантаження…</p>
</div>
<div id="error" style="display:none"></div>
<script>
(function(){
  function applyTheme() {
    var tg = window.Telegram && window.Telegram.WebApp;
    var tp = (tg && tg.themeParams) || {};
    for (var k in tp) {
      if (tp.hasOwnProperty(k)) {
        document.documentElement.style.setProperty('--tg-theme-' + k.replace(/_/g,'-'), tp[k]);
      }
    }
    document.documentElement.dataset.theme = (tg && tg.colorScheme) || 'dark';
  }
  applyTheme();

  function findInitData() {
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.initData) return {source:'sdk', value:tg.initData};
    if (window.location.hash && window.location.hash.indexOf('tgWebAppData') !== -1) {
      var hash = window.location.hash.charAt(0) === '#'
        ? window.location.hash.substring(1) : window.location.hash;
      try {
        var params = new URLSearchParams(hash);
        var raw = params.get('tgWebAppData');
        if (raw) return {source:'hash', value:raw};
      } catch(e) {}
    }
    return null;
  }

  function showError() {
    document.getElementById('loading').style.display = 'none';
    var err = document.getElementById('error');
    var tg = window.Telegram && window.Telegram.WebApp;
    var hasSDK = !!tg, hasInitData = !!(tg && tg.initData);
    err.innerHTML =
      '<h2 class="err">Не вдалося відкрити Dashboard</h2>' +
      '<p>Схоже, сторінку відкрили не через кнопку Telegram Mini App.</p>' +
      '<p>Переконайся, що натискаєш саме кнопку <b>📱 Dashboard</b> на клавіатурі бота.</p>' +
      '<button onclick="location.reload()">🔄 Спробувати ще раз</button>' +
      '<div class="diag">' +
      'has Telegram SDK: ' + hasSDK + '<br>' +
      'has initData: ' + hasInitData + '<br>' +
      'SDK version: ' + ((tg && tg.version) || '(no SDK)') + '<br>' +
      'platform: ' + ((tg && tg.platform) || '(unknown)') + '<br>' +
      'user-agent: ' + navigator.userAgent.substring(0, 200) +
      '</div>';
    err.style.display = 'block';
  }

  function proceed(initDataStr) {
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg) {
      try { tg.ready(); } catch(e) {}
      try { tg.expand && tg.expand(); } catch(e) {}
    }
    var body = 'initData=' + encodeURIComponent(initDataStr);
    fetch(window.location.pathname, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: body,
      credentials: 'same-origin'
    }).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function(html) {
      document.open(); document.write(html); document.close();
    }).catch(function(e) {
      document.getElementById('loading').style.display = 'none';
      var err = document.getElementById('error');
      err.innerHTML = '<h2 class="err">Помилка завантаження</h2><p>' + e.message + '</p>' +
        '<button onclick="location.reload()">🔄 Спробувати ще раз</button>';
      err.style.display = 'block';
    });
  }

  var attempts = 0;
  function tick() {
    var data = findInitData();
    if (data) { proceed(data.value); return; }
    attempts++;
    if (attempts > 20) { showError(); return; }
    setTimeout(tick, 100);
  }
  tick();
})();
</script>
</body>
</html>"""


def _unauthorized_html() -> str:
    return """<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KusWise Bot</title>
<link rel="icon" type="image/png" href="/logo.png">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 40px 20px;
         font-family: -apple-system, system-ui, sans-serif;
         background: var(--tg-theme-bg-color, #101014);
         color: var(--tg-theme-text-color, #e6e6ea); text-align: center; }
  h1 { color: var(--tg-theme-destructive-text-color, #ef5b5b); }
</style></head>
<body>
<h1>🔒 Доступ заборонено</h1>
<p>Не вдалося підтвердити ідентичність у Telegram. Спробуй закрити і відкрити знову з бота.</p>
</body></html>"""


# ------------------------------------------------------------ Dashboard SSR --

_MEAL_TYPE_ORDER = ["breakfast", "lunch", "dinner", "snack"]
_MEAL_TYPE_UA = {
    "breakfast": "🍳 Сніданок",
    "lunch":     "🥗 Обід",
    "dinner":    "🍽️ Вечеря",
    "snack":     "🍎 Перекус",
}
_GOAL_UA = {
    "lose":     "🔥 Схуднути",
    "maintain": "⚖️ Підтримувати",
    "gain":     "💪 Набрати м'язи",
}
_SEX_UA = {"male": "Чоловік", "female": "Жінка"}


def _render_dashboard(user: dict) -> str:
    user_id = user["id"]
    first_name = user.get("first_name") or "друже"
    username = user.get("username") or ""

    today = _today_str()

    conn = get_conn()
    try:
        init_db(conn)
        profile = get_profile(conn, user_id) or {}
        today_blob = _load_day_blob(conn, user_id, today)
        history_30_rows = get_history(conn, user_id, days=PRELOAD_DAYS)
        water_target = int(get_water_target(conn, user_id) or 2000)
        adherence = get_adherence_stats(conn, user_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    cal_target = int(profile.get("daily_calorie_target") or 2000)
    weight_kg = profile.get("weight_kg")
    goal = profile.get("goal")
    if weight_kg and goal:
        macros = macro_gram_targets_from_profile(float(weight_kg), goal)
    else:
        macros = macro_gram_targets(cal_target)

    # Aggregates blob: map date -> {calories, protein, carbs, fat, has_meals}
    history_map = {}
    for r in history_30_rows:
        d = str(r.get("date") or "")
        if not d:
            continue
        history_map[d] = {
            "calories": round(r.get("calories") or 0),
            "protein":  round(r.get("protein") or 0),
            "carbs":    round(r.get("carbs") or 0),
            "fat":      round(r.get("fat") or 0),
            # No meal_count in get_history(); infer "has meals" from any non-zero column
            "has_meals": bool(
                (r.get("calories") or 0) or (r.get("protein") or 0)
                or (r.get("carbs") or 0) or (r.get("fat") or 0)
            ),
        }

    target_weight_kg = profile.get("target_weight_kg")
    profile_blob = {
        "age":              profile.get("age"),
        "sex":              profile.get("sex") or "",
        "weight_kg":        float(weight_kg) if weight_kg else None,
        "height_cm":        profile.get("height_cm"),
        "gym_per_week":     profile.get("gym_per_week"),
        "goal":             goal or "",
        "target_weight_kg": float(target_weight_kg) if target_weight_kg else None,
    }
    targets_blob = {
        "calories":  cal_target,
        "protein":   int(macros.get("protein") or 0),
        "carbs":     int(macros.get("carbs") or 0),
        "fat":       int(macros.get("fat") or 0),
        "water_ml":  water_target,
    }

    data = {
        "user":              {"first_name": first_name, "username": username},
        "today":             today,
        "selected_date":     today,
        "history_max_days":  HISTORY_MAX_DAYS,
        "preload_days":      PRELOAD_DAYS,
        "profile":           profile_blob,
        "targets":           targets_blob,
        "today_blob":        today_blob,
        "history":           history_map,
        "adherence":         adherence,
        "goal_ua":           _GOAL_UA.get(goal or "", ""),
        "sex_ua":            _SEX_UA.get(profile.get("sex") or "", ""),
        "bot_url":           f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else "",
    }
    return _DASHBOARD_HTML.replace("__DATA_JSON__", _json_for_script(data))


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>KusWise Bot</title>
<link rel="icon" type="image/png" href="/logo.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    color-scheme: light dark;
    --bg: var(--tg-theme-bg-color, #101014);
    --bg-secondary: var(--tg-theme-secondary-bg-color, #1b1b22);
    --bg-section: var(--tg-theme-section-bg-color, var(--tg-theme-secondary-bg-color, #1b1b22));
    --text: var(--tg-theme-text-color, #e6e6ea);
    --hint: var(--tg-theme-hint-color, #8b8b95);
    --link: var(--tg-theme-link-color, #3ea6ff);
    --button: var(--tg-theme-button-color, #3ea6ff);
    --button-text: var(--tg-theme-button-text-color, #ffffff);
    --accent: var(--tg-theme-accent-text-color, var(--tg-theme-link-color, #3ea6ff));
    --destructive: var(--tg-theme-destructive-text-color, #ef5b5b);
    --separator: rgba(127,127,127,0.18);
    --track: rgba(127,127,127,0.18);
    --ring-bg: rgba(127,127,127,0.18);
    --ok: #34c759;
    --warn: #ffbb33;
    --over: #ef5b5b;
    --nav-height: 68px;
  }
  [data-theme="light"] {
    --separator: rgba(0,0,0,0.08);
    --track: rgba(0,0,0,0.06);
    --ring-bg: rgba(0,0,0,0.08);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, system-ui, 'SF Pro Text', 'Segoe UI', sans-serif;
    font-size: 15px; line-height: 1.45;
  }
  body {
    min-height: 100vh;
    padding-top: env(safe-area-inset-top, 0);
    padding-bottom: calc(var(--nav-height) + env(safe-area-inset-bottom, 0));
  }

  /* ---------- Day spinner ---------- */
  .spinner-wrap {
    position: sticky; top: 0; z-index: 5;
    background: var(--bg);
    padding: 10px 10px 12px;
    border-bottom: 1px solid var(--separator);
    touch-action: pan-y;
    user-select: none;
  }
  .spinner-row {
    display: grid; grid-template-columns: repeat(7, 1fr);
    gap: 4px;
  }
  .day-cell {
    display: flex; flex-direction: column; align-items: center; gap: 5px;
    padding: 4px 0; border-radius: 12px;
    background: transparent; color: var(--hint);
    border: none; cursor: pointer; font-family: inherit;
  }
  .day-cell:disabled { opacity: 0.3; cursor: default; }
  .day-cell .dow {
    font-size: 0.72em; font-weight: 500;
  }
  .day-cell.today .dow {
    color: var(--text); font-weight: 600;
  }
  .day-cell .num {
    width: 34px; height: 34px; display: grid; place-items: center;
    border-radius: 50%; font-weight: 600; font-size: 0.95em;
    background: var(--bg-secondary); color: var(--text);
  }
  .day-cell.today .num {
    background: transparent;
    outline: 2px solid var(--text); outline-offset: -2px;
  }
  .day-cell.selected:not(.today) .num {
    background: var(--button); color: var(--button-text);
  }
  .day-cell:disabled .num { background: transparent; }

  /* ---------- Content shell ---------- */
  main { padding: 12px 14px 20px; }
  section[hidden] { display: none !important; }

  h1.title { margin: 2px 0 2px; font-size: 1.25em; }
  .sub { color: var(--hint); font-size: 0.9em; margin: 0 0 14px; }

  .card {
    background: var(--bg-section);
    border-radius: 14px; padding: 14px 16px;
    margin-bottom: 12px;
  }
  .card h2 {
    margin: 0 0 10px; font-size: 0.9em; font-weight: 600;
    color: var(--hint); text-transform: uppercase; letter-spacing: 0.04em;
  }

  /* ---------- Overview hero ---------- */
  .hero {
    display: grid; grid-template-columns: 1fr auto 1fr;
    gap: 10px; align-items: center;
    background: var(--bg-section); border-radius: 16px;
    padding: 18px 14px; margin-bottom: 12px;
  }
  .hero-side {
    text-align: center;
  }
  .hero-side .label {
    font-size: 0.78em; color: var(--hint);
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .hero-side .value {
    font-size: 1.4em; font-weight: 700; margin-top: 4px;
  }
  .hero-side .unit {
    font-size: 0.8em; color: var(--hint); margin-top: 2px;
  }
  .hero-side .mini-bar {
    height: 6px; border-radius: 3px; margin: 8px auto 0;
    background: var(--track); overflow: hidden; width: 90%;
  }
  .hero-side .mini-fill {
    height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa);
    border-radius: 3px; width: 0%;
  }

  .ring-wrap {
    position: relative; width: 140px; height: 140px;
    display: grid; place-items: center;
  }
  .ring-svg { transform: rotate(-90deg); }
  .ring-center {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center;
  }
  .ring-center .big {
    font-size: 1.5em; font-weight: 700; line-height: 1;
  }
  .ring-center .sub {
    margin: 4px 0 0; font-size: 0.75em; color: var(--hint);
  }
  .ring-center .unit {
    margin-top: 2px; font-size: 0.72em; color: var(--hint);
  }

  /* ---------- Macros row ---------- */
  .macro {
    display: flex; align-items: center; gap: 10px; margin: 10px 0;
  }
  .macro-name {
    flex: 0 0 auto; width: 100px; color: var(--text); font-size: 0.9em;
  }
  .macro-bar {
    flex: 1 1 auto; height: 8px; background: var(--track);
    border-radius: 4px; overflow: hidden; min-width: 40px;
  }
  .macro-fill { height: 100%; background: var(--button); border-radius: 4px; }
  .macro-fill.ok   { background: var(--ok); }
  .macro-fill.warn { background: var(--warn); }
  .macro-fill.over { background: var(--over); }
  .macro-val {
    flex: 0 0 auto; color: var(--text); font-size: 0.82em;
    font-weight: 500; white-space: nowrap; font-variant-numeric: tabular-nums;
  }

  .summary-text {
    margin: 0; font-size: 0.92em; line-height: 1.5; color: var(--text);
  }
  .summary-text .muted { color: var(--hint); }

  /* Meals tab header summary */
  .meals-summary {
    padding: 2px 0 12px;
    margin-bottom: 10px;
    border-bottom: 1px solid var(--separator);
  }
  .meals-summary-line { font-size: 1em; color: var(--text); }
  .meals-summary-line b { font-size: 1.3em; font-weight: 700; }
  .meals-summary-line .muted { color: var(--hint); font-size: 0.9em; }
  .meals-summary-macros { color: var(--hint); font-size: 0.88em; margin-top: 4px; }
  .meals-summary-bar {
    height: 6px; border-radius: 3px; margin-top: 8px;
    background: var(--track); overflow: hidden;
  }
  .meals-summary-fill { height: 100%; background: var(--accent); border-radius: 3px; }

  /* ---------- Meals list ---------- */
  .meal-group { margin-top: 4px; }
  .meal-group h3 {
    margin: 12px 0 6px; font-size: 0.85em; font-weight: 600;
    color: var(--hint); text-transform: uppercase; letter-spacing: 0.04em;
  }
  .meal-empty { color: var(--hint); font-size: 0.88em; padding: 6px 0; }
  .meal-row {
    display: grid; grid-template-columns: 1fr auto auto;
    gap: 10px; align-items: baseline;
    padding: 10px 0; border-bottom: 1px solid var(--separator);
  }
  .meal-row:last-child { border-bottom: none; }
  .meal-desc { overflow: hidden; text-overflow: ellipsis; }
  .meal-kcal { font-variant-numeric: tabular-nums; font-weight: 600; }
  .meal-pct { color: var(--hint); font-size: 0.82em; font-variant-numeric: tabular-nums; }

  /* ---------- Profile tab ---------- */
  .id-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px;
    font-size: 0.92em;
  }
  .id-grid .k { color: var(--hint); }
  .id-grid .v { font-weight: 600; text-align: right; }

  .streak-pill {
    display: inline-block; padding: 6px 12px;
    background: var(--bg-secondary); border-radius: 999px;
    font-weight: 600; font-size: 0.92em;
  }

  /* ---------- Bottom nav ---------- */
  .bottom-nav {
    position: fixed; left: 0; right: 0; bottom: 0;
    display: grid; grid-template-columns: repeat(3, 1fr);
    background: var(--bg-section);
    border-top: 1px solid var(--separator);
    padding: 6px 6px calc(6px + env(safe-area-inset-bottom, 0));
    z-index: 10;
  }
  .bottom-nav button {
    background: transparent; border: none; color: var(--hint);
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: 6px 4px; cursor: pointer; font-family: inherit; font-size: 0.72em;
    border-radius: 10px;
  }
  .bottom-nav button.active {
    color: var(--accent);
  }
  .bottom-nav .nav-icon { font-size: 1.35em; line-height: 1; }

  .muted-note {
    color: var(--hint); font-size: 0.8em; text-align: center; margin-top: 10px;
  }

  .hint-line { color: var(--hint); font-size: 0.88em; margin: 8px 0 0; }
</style>
</head>
<body>

<div class="spinner-wrap">
  <div class="spinner-row" id="spinnerRow"></div>
</div>

<main>
  <section id="tab-overview">
    <div class="hero">
      <div class="hero-side">
        <div class="label">💧 Вода</div>
        <div class="value" id="waterValue">0 / 0 л</div>
        <div class="unit" id="waterPct">0 %</div>
        <div class="mini-bar"><div class="mini-fill" id="waterBar"></div></div>
      </div>
      <div class="ring-wrap">
        <svg class="ring-svg" width="140" height="140" viewBox="0 0 140 140">
          <circle cx="70" cy="70" r="60" fill="none"
                  stroke="var(--ring-bg)" stroke-width="12"></circle>
          <circle id="calRing" cx="70" cy="70" r="60" fill="none"
                  stroke="var(--accent)" stroke-width="12"
                  stroke-linecap="round"
                  stroke-dasharray="377" stroke-dashoffset="377"></circle>
        </svg>
        <div class="ring-center">
          <div class="big" id="calEaten">0</div>
          <div class="sub">з <span id="calGoal">0</span> ккал</div>
          <div class="unit" id="calPct">0 %</div>
        </div>
      </div>
      <div class="hero-side">
        <div class="label">🔥 Ще можна</div>
        <div class="value" id="calRemaining">0</div>
        <div class="unit">ккал</div>
      </div>
    </div>

    <div class="card">
      <h2>Макро</h2>
      <div class="macro">
        <span class="macro-name">🥩 Білок</span>
        <div class="macro-bar"><div id="pFill" class="macro-fill"></div></div>
        <b class="macro-val" id="pVal">0 / 0 г</b>
      </div>
      <div class="macro">
        <span class="macro-name">🍞 Вуглеводи</span>
        <div class="macro-bar"><div id="cFill" class="macro-fill"></div></div>
        <b class="macro-val" id="cVal">0 / 0 г</b>
      </div>
      <div class="macro">
        <span class="macro-name">🥑 Жири</span>
        <div class="macro-bar"><div id="fFill" class="macro-fill"></div></div>
        <b class="macro-val" id="fVal">0 / 0 г</b>
      </div>
    </div>

    <div class="card summary-card">
      <h2>Підсумок</h2>
      <p class="summary-text" id="daySummary">—</p>
    </div>
  </section>

  <section id="tab-meals" hidden>
    <div class="card">
      <h2 id="mealsDateHeader">Страви</h2>
      <div class="meals-summary" id="mealsSummary"></div>
      <div id="mealsList"><p class="meal-empty">Завантаження…</p></div>
      <p class="hint-line">Змінити або видалити можна в боті: <b>/meals</b></p>
    </div>
  </section>

  <section id="tab-profile" hidden>
    <div class="card">
      <h2>Профіль</h2>
      <div class="id-grid" id="profileGrid"></div>
    </div>

    <div class="card">
      <h2>Цілі на день</h2>
      <div class="id-grid" id="targetsGrid"></div>
    </div>

    <div class="card">
      <h2>Середнє за весь час</h2>
      <div id="averagesBody"></div>
    </div>

    <div class="card">
      <h2>Влучність (±15 % від цілі)</h2>
      <div id="adherenceBody"></div>
    </div>

    <div class="card" id="streakCard" hidden>
      <h2>Серія</h2>
      <span class="streak-pill" id="streakValue">🔥 0 днів поспіль</span>
    </div>
  </section>
</main>

<nav class="bottom-nav" id="bottomNav">
  <button type="button" class="active" data-tab="overview">
    <span class="nav-icon">🏠</span><span>Огляд</span>
  </button>
  <button type="button" data-tab="meals">
    <span class="nav-icon">🍽️</span><span>Страви</span>
  </button>
  <button type="button" data-tab="profile">
    <span class="nav-icon">👤</span><span>Профіль</span>
  </button>
</nav>

<script type="application/json" id="__data__">__DATA_JSON__</script>
<script>
  var TG = (window.Telegram && window.Telegram.WebApp) || null;
  var DATA = JSON.parse(document.getElementById('__data__').textContent);
  var BOT_URL = DATA.bot_url || '';
  var HISTORY_MAX_DAYS = DATA.history_max_days || 90;

  // Per-day cache: date -> day_blob ({date, log:{calories,protein,carbs,fat,meal_count}, meals:[], water_ml})
  var dayCache = {};
  dayCache[DATA.today] = DATA.today_blob;

  // Aggregate cache: date -> {calories, protein, carbs, fat, has_meals}
  var aggregates = DATA.history || {};

  // Current selections
  var selectedDate = DATA.selected_date || DATA.today;
  var weekAnchor;  // date of Monday of the currently visible week
  var activeTab = 'overview';

  // --- Theme application ---
  function applyTheme() {
    var tp = (TG && TG.themeParams) || {};
    for (var k in tp) {
      if (tp.hasOwnProperty(k)) {
        document.documentElement.style.setProperty(
          '--tg-theme-' + k.replace(/_/g,'-'), tp[k]);
      }
    }
    document.documentElement.dataset.theme = (TG && TG.colorScheme) || 'dark';
  }
  applyTheme();
  if (TG) {
    try { TG.ready(); } catch(e) {}
    try { TG.expand(); } catch(e) {}
    try { TG.onEvent('themeChanged', applyTheme); } catch(e) {}
  }

  // --- Date helpers ---
  function toISO(d) {
    var y = d.getFullYear();
    var m = ('0' + (d.getMonth() + 1)).slice(-2);
    var day = ('0' + d.getDate()).slice(-2);
    return y + '-' + m + '-' + day;
  }
  function fromISO(s) {
    var parts = s.split('-');
    return new Date(+parts[0], +parts[1] - 1, +parts[2]);
  }
  function startOfWeek(d) {
    // Monday as the first day
    var nd = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var day = nd.getDay(); // 0 Sun .. 6 Sat
    var diff = (day + 6) % 7;
    nd.setDate(nd.getDate() - diff);
    return nd;
  }
  function addDays(d, n) {
    var nd = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    nd.setDate(nd.getDate() + n);
    return nd;
  }

  var todayDate = fromISO(DATA.today);
  var minDate = addDays(todayDate, -HISTORY_MAX_DAYS);
  weekAnchor = startOfWeek(fromISO(selectedDate));

  // --- Spinner rendering ---
  var DOW_UA = ['Пн','Вт','Ср','Чт','Пт','Сб','Нд'];
  var MONTH_UA = ['січ','лют','бер','кві','тра','чер','лип','сер','вер','жов','лис','гру'];

  function renderSpinner() {
    var row = document.getElementById('spinnerRow');
    row.innerHTML = '';
    for (var i = 0; i < 7; i++) {
      var d = addDays(weekAnchor, i);
      var iso = toISO(d);
      var isToday = iso === DATA.today;
      var isSelected = iso === selectedDate;
      var isFuture = d > todayDate;
      var isTooOld = d < minDate;

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'day-cell';
      if (isToday) btn.classList.add('today');
      if (isSelected) btn.classList.add('selected');
      btn.disabled = isFuture || isTooOld;
      var label = isToday ? 'Сьогодні' : DOW_UA[i];
      btn.innerHTML = '<span class="dow">' + label + '</span>' +
                      '<span class="num">' + d.getDate() + '</span>';
      (function(iso2){
        btn.addEventListener('click', function(){ selectDate(iso2); });
      })(iso);
      row.appendChild(btn);
    }
  }

  // Swipe navigation on the spinner row (horizontal drag → ±1 week).
  (function(){
    var wrap = document.querySelector('.spinner-wrap');
    if (!wrap) return;
    var startX = null, startY = null;
    wrap.addEventListener('touchstart', function(e){
      if (e.touches.length !== 1) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
    }, {passive: true});
    wrap.addEventListener('touchend', function(e){
      if (startX == null) return;
      var t = e.changedTouches[0];
      var dx = t.clientX - startX;
      var dy = t.clientY - startY;
      startX = startY = null;
      if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy)) return;
      if (dx < 0) {
        var next = addDays(weekAnchor, 7);
        if (next > todayDate) return;
        weekAnchor = next;
      } else {
        var prev = addDays(weekAnchor, -7);
        weekAnchor = (prev < minDate) ? startOfWeek(minDate) : prev;
      }
      renderSpinner();
    }, {passive: true});
  })();

  // --- Tab switching ---
  function setTab(tab) {
    activeTab = tab;
    var tabs = ['overview', 'meals', 'profile'];
    tabs.forEach(function(t){
      var el = document.getElementById('tab-' + t);
      if (el) el.hidden = (t !== tab);
    });
    document.querySelectorAll('#bottomNav button').forEach(function(b){
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    // Profile tab doesn't need the spinner; hide to save space.
    document.querySelector('.spinner-wrap').style.display =
      (tab === 'profile') ? 'none' : '';
    if (tab === 'profile') renderProfile();
    try { history.replaceState(null, '', '#' + tab); } catch(e) {}
  }
  document.querySelectorAll('#bottomNav button').forEach(function(b){
    b.addEventListener('click', function(){ setTab(b.dataset.tab); });
  });

  // --- Day selection + render ---
  function selectDate(iso) {
    selectedDate = iso;
    renderSpinner();
    var blob = dayCache[iso];
    if (blob) {
      renderOverview(blob);
      renderMeals(blob);
    } else {
      fetchDay(iso);
    }
  }

  function fetchDay(iso) {
    // Show a light "loading" state — keep previous numbers, add hint
    var initData = (TG && TG.initData) || '';
    var body = 'action=day_data&date=' + encodeURIComponent(iso) +
               '&initData=' + encodeURIComponent(initData);
    fetch(window.location.pathname, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: body,
      credentials: 'same-origin'
    }).then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function(blob){
      if (!blob || blob.error) throw new Error(blob && blob.error || 'bad response');
      dayCache[blob.date] = blob;
      aggregates[blob.date] = {
        calories: blob.log.calories, protein: blob.log.protein,
        carbs: blob.log.carbs, fat: blob.log.fat,
        has_meals: (blob.meals && blob.meals.length > 0)
      };
      if (blob.date === selectedDate) {
        renderOverview(blob);
        renderMeals(blob);
        renderSpinner();
      }
    }).catch(function(e){ console.error('day_data fetch', e); });
  }

  // --- Overview rendering ---
  function macroClass(pct) {
    if (pct > 115) return 'over';
    if (pct >= 85) return 'ok';
    if (pct < 50)  return 'warn';
    return '';
  }

  function renderOverview(blob) {
    var t = DATA.targets;
    var log = blob.log;
    var cal = log.calories || 0;
    var p = log.protein || 0, c = log.carbs || 0, f = log.fat || 0;
    var calTarget = t.calories || 1;
    var waterTarget = t.water_ml || 2000;
    var waterMl = blob.water_ml || 0;

    // Calorie ring
    var pct = Math.max(0, Math.min(1, cal / calTarget));
    var circumference = 2 * Math.PI * 60;
    document.getElementById('calRing').setAttribute(
      'stroke-dasharray', circumference.toFixed(1));
    document.getElementById('calRing').setAttribute(
      'stroke-dashoffset', (circumference * (1 - pct)).toFixed(1));
    document.getElementById('calEaten').textContent = Math.round(cal).toLocaleString('uk-UA');
    document.getElementById('calGoal').textContent = calTarget.toLocaleString('uk-UA');
    document.getElementById('calPct').textContent = Math.round(pct * 100) + ' %';

    // Remaining calories
    var remaining = Math.max(0, calTarget - cal);
    document.getElementById('calRemaining').textContent = Math.round(remaining).toLocaleString('uk-UA');

    // Water
    var waterL = (waterMl / 1000).toFixed(2).replace('.', ',');
    var waterTargetL = (waterTarget / 1000).toFixed(1).replace('.', ',');
    document.getElementById('waterValue').textContent = waterL + ' / ' + waterTargetL + ' л';
    var waterPctRaw = waterTarget > 0 ? Math.min(1, waterMl / waterTarget) : 0;
    document.getElementById('waterPct').textContent = Math.round(waterPctRaw * 100) + ' %';
    document.getElementById('waterBar').style.width = (waterPctRaw * 100).toFixed(1) + '%';

    // Macros
    function setMacro(fillId, valId, value, target) {
      var tgt = target || 1;
      var ratio = Math.max(0, Math.min(1.2, value / tgt));
      var pct = ratio * 100;
      var fill = document.getElementById(fillId);
      fill.style.width = Math.min(100, pct).toFixed(1) + '%';
      fill.className = 'macro-fill ' + macroClass((value / tgt) * 100);
      document.getElementById(valId).textContent =
        Math.round(value) + ' / ' + Math.round(tgt) + ' г';
    }
    setMacro('pFill', 'pVal', p, t.protein);
    setMacro('cFill', 'cVal', c, t.carbs);
    setMacro('fFill', 'fVal', f, t.fat);

    renderSummary(blob);
  }

  function renderSummary(blob) {
    var t = DATA.targets;
    var log = blob.log;
    var cal = log.calories || 0;
    var p = log.protein || 0, c = log.carbs || 0, f = log.fat || 0;
    var mc = log.meal_count || 0;
    var calTarget = t.calories || 1;
    var waterMl = blob.water_ml || 0;
    var isToday = (blob.date === DATA.today);

    var calPct = Math.round((cal / calTarget) * 100);
    var diff = Math.round(cal - calTarget);
    var parts = [];

    if (mc === 0) {
      parts.push(isToday
        ? 'Сьогодні ще нічого не записано.'
        : 'На цей день нічого не записано.');
    } else {
      if (calPct > 115) {
        parts.push('Калорії перевищено: <b>' + calPct + '%</b> цілі (+' + Math.abs(diff).toLocaleString('uk-UA') + ' ккал).');
      } else if (calPct >= 85) {
        parts.push('Калорії в нормі: <b>' + calPct + '%</b> цілі.');
      } else if (calPct >= 50) {
        parts.push('Калорій з\'їдено <b>' + calPct + '%</b> цілі' + (isToday ? ' — ще є простір.' : '.'));
      } else {
        parts.push('Калорій дуже мало: <b>' + calPct + '%</b> цілі.');
      }

      // Find the most-off macro (largest |pct-100|) and comment on it.
      var macros = [
        {name: 'білків',    val: p, target: t.protein},
        {name: 'вуглеводів',val: c, target: t.carbs},
        {name: 'жирів',     val: f, target: t.fat},
      ];
      var worst = null, worstOff = 0;
      macros.forEach(function(m){
        if (!m.target) return;
        var pct = (m.val / m.target) * 100;
        var off = Math.abs(pct - 100);
        if (off > worstOff) { worstOff = off; worst = {m: m, pct: pct}; }
      });
      if (worst && worstOff >= 25) {
        var pctR = Math.round(worst.pct);
        if (worst.pct < 100) {
          parts.push('Мало ' + worst.m.name + ' — <b>' + pctR + '%</b> цілі.');
        } else {
          parts.push('Багато ' + worst.m.name + ' — <b>' + pctR + '%</b> цілі.');
        }
      }

      parts.push('<span class="muted">Страв: ' + mc + ' · вода ' +
                 (waterMl / 1000).toFixed(2).replace('.', ',') + ' л.</span>');
    }

    document.getElementById('daySummary').innerHTML = parts.join(' ');
  }

  // --- Meals rendering ---
  var MEAL_TYPE_ORDER = ['breakfast', 'lunch', 'dinner', 'snack'];
  var MEAL_TYPE_UA = {
    breakfast: '🍳 Сніданок',
    lunch:     '🥗 Обід',
    dinner:    '🍽️ Вечеря',
    snack:     '🍎 Перекус'
  };

  function renderMeals(blob) {
    var header = document.getElementById('mealsDateHeader');
    header.textContent = formatLongDate(blob.date);
    var t = DATA.targets;
    var calTarget = t.calories || 1;
    var log = blob.log || {};
    var list = document.getElementById('mealsList');
    var meals = blob.meals || [];

    var cal = log.calories || 0;
    var pct = Math.round((cal / calTarget) * 100);
    var fillPct = Math.min(100, Math.max(0, pct));
    document.getElementById('mealsSummary').innerHTML =
      '<div class="meals-summary-line">' +
        '<b>' + Math.round(cal).toLocaleString('uk-UA') + '</b>' +
        ' <span class="muted">/ ' + calTarget.toLocaleString('uk-UA') + ' ккал · ' + pct + '%</span>' +
      '</div>' +
      '<div class="meals-summary-macros">' +
        '🥩 ' + Math.round(log.protein || 0) + ' г · ' +
        '🍞 ' + Math.round(log.carbs || 0) + ' г · ' +
        '🥑 ' + Math.round(log.fat || 0) + ' г · ' +
        'страв: ' + meals.length +
      '</div>' +
      '<div class="meals-summary-bar"><div class="meals-summary-fill" style="width:' + fillPct + '%"></div></div>';

    if (meals.length === 0) {
      list.innerHTML = '<p class="meal-empty">На цей день нічого не записано.</p>';
      return;
    }
    var grouped = {};
    MEAL_TYPE_ORDER.forEach(function(t){ grouped[t] = []; });
    var unknown = [];
    meals.forEach(function(m){
      if (grouped[m.meal_type]) grouped[m.meal_type].push(m);
      else unknown.push(m);
    });

    var html = '';
    MEAL_TYPE_ORDER.forEach(function(t){
      var arr = grouped[t];
      if (arr.length === 0) return;
      html += '<div class="meal-group"><h3>' + MEAL_TYPE_UA[t] + '</h3>';
      arr.forEach(function(m){
        var pct = calTarget > 0 ? Math.round((m.calories / calTarget) * 100) : 0;
        html += '<div class="meal-row">' +
                '<div class="meal-desc">' + esc(m.description) + '</div>' +
                '<div class="meal-kcal">' + Math.round(m.calories) + ' ккал</div>' +
                '<div class="meal-pct">' + pct + ' %</div>' +
                '</div>';
      });
      html += '</div>';
    });
    if (unknown.length) {
      html += '<div class="meal-group"><h3>Інше</h3>';
      unknown.forEach(function(m){
        var pct = calTarget > 0 ? Math.round((m.calories / calTarget) * 100) : 0;
        html += '<div class="meal-row">' +
                '<div class="meal-desc">' + esc(m.description) + '</div>' +
                '<div class="meal-kcal">' + Math.round(m.calories) + ' ккал</div>' +
                '<div class="meal-pct">' + pct + ' %</div>' +
                '</div>';
      });
      html += '</div>';
    }
    list.innerHTML = html;
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }

  function formatLongDate(iso) {
    var d = fromISO(iso);
    return d.getDate() + ' ' + MONTH_UA[d.getMonth()] + ' ' + d.getFullYear();
  }

  // --- Profile rendering ---
  var GOAL_UA = { lose: '🔥 Схуднути', maintain: '⚖️ Підтримувати', gain: '💪 Набрати мʼязи' };
  var SEX_UA  = { male: 'Чоловік', female: 'Жінка' };

  function idRow(k, v) {
    return '<div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + '</div>';
  }

  function renderProfile() {
    var p = DATA.profile || {}, t = DATA.targets || {}, a = DATA.adherence || {};
    var grid = document.getElementById('profileGrid');
    var rows =
      idRow('Імʼя', DATA.user.first_name || '—') +
      idRow('Вік', p.age != null ? p.age + ' р.' : '—') +
      idRow('Стать', SEX_UA[p.sex] || '—') +
      idRow('Вага', p.weight_kg != null ? p.weight_kg + ' кг' : '—') +
      idRow('Зріст', p.height_cm != null ? p.height_cm + ' см' : '—') +
      idRow('Зал/тиждень', p.gym_per_week != null ? p.gym_per_week + '×' : '—') +
      idRow('Ціль', GOAL_UA[p.goal] || '—');

    if (p.target_weight_kg != null && (p.goal === 'lose' || p.goal === 'gain')) {
      var togoTxt;
      if (p.weight_kg != null) {
        var delta = Number(p.weight_kg) - Number(p.target_weight_kg);
        var rem = p.goal === 'lose' ? Math.max(0, delta) : Math.max(0, -delta);
        if (rem <= 0.05) {
          togoTxt = p.target_weight_kg + ' кг (🎉)';
        } else {
          var sign = p.goal === 'lose' ? '−' : '+';
          togoTxt = p.target_weight_kg + ' кг (' + sign + rem.toFixed(1) + ' кг)';
        }
      } else {
        togoTxt = p.target_weight_kg + ' кг';
      }
      rows += idRow('Цільова вага', togoTxt);
    }
    grid.innerHTML = rows;

    var tg = document.getElementById('targetsGrid');
    tg.innerHTML =
      idRow('Калорії', (t.calories || 0).toLocaleString('uk-UA') + ' ккал') +
      idRow('Білок', (t.protein || 0) + ' г') +
      idRow('Вуглеводи', (t.carbs || 0) + ' г') +
      idRow('Жири', (t.fat || 0) + ' г') +
      idRow('Вода', ((t.water_ml || 0) / 1000).toFixed(1).replace('.', ',') + ' л');

    var avg = document.getElementById('averagesBody');
    if (!a.logged_days) {
      avg.innerHTML = '<p class="meal-empty">Недостатньо даних — почни логувати страви, і статистика зʼявиться.</p>';
    } else {
      avg.innerHTML =
        '<div class="macro"><span class="macro-name">🔥 Ккал</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_calories / (t.calories || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_calories || 0) + ' / ' + (t.calories || 0) + '</b></div>' +

        '<div class="macro"><span class="macro-name">🥩 Білок</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_protein_g / (t.protein || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_protein_g || 0) + ' / ' + (t.protein || 0) + ' г</b></div>' +

        '<div class="macro"><span class="macro-name">🍞 Вуглеводи</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_carbs_g / (t.carbs || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_carbs_g || 0) + ' / ' + (t.carbs || 0) + ' г</b></div>' +

        '<div class="macro"><span class="macro-name">🥑 Жири</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_fat_g / (t.fat || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_fat_g || 0) + ' / ' + (t.fat || 0) + ' г</b></div>' +

        '<p class="muted-note">Середнє за ' + a.logged_days + ' днів з даними.</p>';
    }

    var adh = document.getElementById('adherenceBody');
    if (!a.logged_days || a.logged_days < 3) {
      adh.innerHTML = '<p class="meal-empty">Недостатньо даних (потрібно ≥ 3 днів).</p>';
    } else {
      adh.innerHTML =
        adherenceRow('🔥 Калорії', a.calories_hit_pct) +
        adherenceRow('🥩 Білок',  a.protein_hit_pct) +
        adherenceRow('🍞 Вуглеводи', a.carbs_hit_pct) +
        adherenceRow('🥑 Жири',   a.fat_hit_pct);
    }

    var streakCard = document.getElementById('streakCard');
    if (a.current_streak && a.current_streak >= 2) {
      streakCard.hidden = false;
      document.getElementById('streakValue').textContent =
        '🔥 ' + a.current_streak + ' днів поспіль';
    } else {
      streakCard.hidden = true;
    }
  }

  function adherenceRow(label, pct) {
    var val = pct == null ? 0 : pct;
    var cls = val >= 60 ? 'ok' : (val >= 30 ? 'warn' : 'over');
    return '<div class="macro"><span class="macro-name">' + esc(label) + '</span>' +
           '<div class="macro-bar"><div class="macro-fill ' + cls +
             '" style="width:' + val + '%"></div></div>' +
           '<b class="macro-val">' + val + ' %</b></div>';
  }

  // --- Refresh today's data when returning to the tab ---
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden) {
      // Invalidate today's cached blob and refetch if we're viewing today.
      delete dayCache[DATA.today];
      if (selectedDate === DATA.today) fetchDay(DATA.today);
    }
  });

  // --- Boot ---
  if (location.hash === '#meals')   setTab('meals');
  else if (location.hash === '#profile') setTab('profile');
  else setTab('overview');
  renderSpinner();
  renderOverview(DATA.today_blob);
  renderMeals(DATA.today_blob);
</script>
</body></html>"""
