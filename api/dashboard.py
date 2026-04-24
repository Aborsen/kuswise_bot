"""Telegram Mini App: read-only per-user dashboard.

Served at /api/dashboard. Opened from the bot's reply-keyboard "Dashboard" button
(which sets `web_app.url` to this endpoint). Telegram passes signed `initData`
which this handler verifies via HMAC-SHA256 using the bot token as the secret
seed — the canonical Telegram Web App auth flow.

Shows: today's progress bars, today's meals, last 7 days history. Read-only.
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
    get_today_log,
    get_log_for_date,
    get_meals_for_day,
    get_history,
    get_profile,
    profile_is_complete,
    get_water_today,
    get_water_target,
    get_water_for_date,
    add_water,
    remove_last_water_today,
)


# 24-hour max age for initData, per Telegram's recommendation
INIT_DATA_MAX_AGE = 24 * 60 * 60

_SECURITY_HEADERS = [
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    # Telegram's in-app webview aggressively caches Mini App HTML, which causes
    # stale dashboards when reopening. Force a fresh fetch at every layer
    # (browser, Vercel CDN, any intermediate proxy).
    ("Cache-Control", "no-store, no-cache, must-revalidate, private, max-age=0"),
    ("CDN-Cache-Control", "no-store"),
    ("Vercel-CDN-Cache-Control", "no-store"),
    ("Surrogate-Control", "no-store"),
    ("Pragma", "no-cache"),
    ("Expires", "0"),
    # Miniapps are loaded in Telegram's webview, so frame-ancestors must allow it.
    # Note: we intentionally DO NOT set X-Frame-Options here — Telegram must iframe us.
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
    """Validate Telegram WebApp initData. Returns parsed user dict on success.

    Implements https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Uses constant-time comparison for the HMAC check.
    """
    if not init_data or not TELEGRAM_BOT_TOKEN:
        return None

    # Parse as query string (keys are URL-encoded)
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    params = dict(pairs)

    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    # Check auth_date freshness — reject stale tokens
    try:
        auth_date = int(params.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > INIT_DATA_MAX_AGE:
        return None

    # Build data_check_string: sorted lines of "key=value", \n-joined
    data_check_string = "\n".join(
        f"{k}={params[k]}" for k in sorted(params.keys())
    )

    # secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    secret_key = hmac.new(
        b"WebAppData",
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    # Parse the user JSON
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

    # Additional whitelist check (defense-in-depth; normally enforced by the bot anyway)
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return None

    return user


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

    def do_GET(self):
        # GET always serves the bootstrap page. The bootstrap JS reads the
        # Telegram initData from the SDK (or URL hash) and POSTs it back to
        # this endpoint — keeps initData out of URL/logs/history.
        self._send_html(200, _BOOTSTRAP_HTML)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        # initData is small — cap at 16 KB
        if length <= 0 or length > 16 * 1024:
            self._send_html(400, "<h1>Bad request</h1>")
            return

        try:
            raw = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(raw)
            init_data = (form.get("initData") or [""])[0]
            action = (form.get("action") or [""])[0]
        except Exception:
            self._send_html(400, "<h1>Bad request</h1>")
            return

        user = _verify_init_data(init_data)
        if user is None:
            self._send_html(401, _unauthorized_html())
            return

        if action:
            conn = get_conn()
            try:
                _dispatch_action(conn, user["id"], action)
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


# ------------------------------------------------------------------ HTML --

_BOOTSTRAP_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Food Tracker</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body { margin: 0; padding: 40px 20px; font-family: -apple-system, system-ui, sans-serif;
         background: #0f0f1a; color: #e0e0e0; text-align: center; }
  .spinner { width: 32px; height: 32px; margin: 20px auto; border: 3px solid #16213e;
             border-top-color: #e94560; border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .diag { margin-top: 20px; padding: 12px; background: #16213e; border-radius: 8px;
          font-family: ui-monospace, Menlo, monospace; font-size: 0.75em;
          color: #888; text-align: left; word-break: break-all; }
  .err { color: #e94560; }
  button { background: #e94560; color: white; border: 0; padding: 10px 18px;
           border-radius: 8px; font-size: 1em; margin-top: 14px; cursor: pointer; }
</style>
</head>
<body>
<div id="loading">
  <div class="spinner"></div>
  <p>Завантаження dashboard…</p>
</div>
<div id="error" style="display:none"></div>
<script>
(function(){
  // Try to get initData from Telegram SDK or from URL hash (Telegram sometimes
  // drops tgWebAppData in the fragment on Mini App launch).
  function findInitData() {
    var tg = window.Telegram && window.Telegram.WebApp;
    if (tg && tg.initData) return {source: 'sdk', value: tg.initData};
    if (window.location.hash && window.location.hash.indexOf('tgWebAppData') !== -1) {
      var hash = window.location.hash.charAt(0) === '#'
        ? window.location.hash.substring(1)
        : window.location.hash;
      try {
        var params = new URLSearchParams(hash);
        var raw = params.get('tgWebAppData');
        if (raw) return {source: 'hash', value: raw};
      } catch(e) {}
    }
    return null;
  }

  function showError() {
    document.getElementById('loading').style.display = 'none';
    var err = document.getElementById('error');
    var tg = window.Telegram && window.Telegram.WebApp;
    var hasSDK = !!tg;
    var hasInitData = !!(tg && tg.initData);
    var ver = (tg && tg.version) || '(no SDK)';
    var platform = (tg && tg.platform) || '(unknown)';
    err.innerHTML =
      '<h2 class="err">Не вдалося відкрити Dashboard</h2>' +
      '<p>Схоже, сторінку відкрили не через кнопку Telegram Mini App.</p>' +
      '<p>Переконайся, що натискаєш саме кнопку <b>📱 Dashboard</b> на клавіатурі бота, а не посилання в тексті.</p>' +
      '<button onclick="location.reload()">🔄 Спробувати ще раз</button>' +
      '<div class="diag">' +
      'has Telegram SDK: ' + hasSDK + '<br>' +
      'has initData: ' + hasInitData + '<br>' +
      'SDK version: ' + ver + '<br>' +
      'platform: ' + platform + '<br>' +
      'hash present: ' + !!window.location.hash + '<br>' +
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
    // POST initData to avoid putting it in the URL (cleaner, no log/history leak).
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
      document.open();
      document.write(html);
      document.close();
    }).catch(function(e) {
      document.getElementById('loading').style.display = 'none';
      var err = document.getElementById('error');
      err.innerHTML = '<h2 class="err">Помилка завантаження</h2><p>' + e.message + '</p>' +
        '<button onclick="location.reload()">🔄 Спробувати ще раз</button>';
      err.style.display = 'block';
    });
  }

  // Try immediately, then retry up to 2s waiting for the SDK to init (slow clients).
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
<title>Food Tracker</title>
<style>
  body { margin: 0; padding: 40px 20px; font-family: -apple-system, system-ui, sans-serif;
         background: #0f0f1a; color: #e0e0e0; text-align: center; }
  h1 { color: #e94560; }
</style></head>
<body>
<h1>🔒 Доступ заборонено</h1>
<p>Не вдалося підтвердити ідентичність у Telegram. Спробуй закрити і відкрити знову з бота.</p>
</body></html>"""


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _bar(value: float, target: float, width: int = 20) -> str:
    if target <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, value / target))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


_MEAL_TYPE_UA = {
    "breakfast": "🍳 Сніданок",
    "lunch": "🥗 Обід",
    "dinner": "🍽️ Вечеря",
    "snack": "🍎 Перекус",
}


def _aggregate(rows: list[dict]) -> dict:
    """Sum + average totals over history rows (for week/month views)."""
    if not rows:
        return {"days": 0, "avg_cal": 0, "avg_p": 0, "avg_c": 0, "avg_f": 0,
                "total_cal": 0, "total_p": 0, "total_c": 0, "total_f": 0}
    n = len(rows)
    tc = sum(r.get("calories", 0) for r in rows)
    tp = sum(r.get("protein", 0) for r in rows)
    tca = sum(r.get("carbs", 0) for r in rows)
    tf = sum(r.get("fat", 0) for r in rows)
    return {
        "days": n,
        "avg_cal": round(tc / n), "avg_p": round(tp / n),
        "avg_c": round(tca / n), "avg_f": round(tf / n),
        "total_cal": round(tc), "total_p": round(tp),
        "total_c": round(tca), "total_f": round(tf),
    }


def _render_history_table(rows: list[dict]) -> str:
    if not rows:
        return "<tr><td colspan='5' class='empty'>Історії ще немає.</td></tr>"
    out = ""
    for h in rows:
        out += (
            f"<tr>"
            f"<td>{_esc(h.get('date',''))}</td>"
            f"<td class='num'>{round(h.get('calories',0))}</td>"
            f"<td class='num'>{round(h.get('protein',0))}</td>"
            f"<td class='num'>{round(h.get('carbs',0))}</td>"
            f"<td class='num'>{round(h.get('fat',0))}</td>"
            f"</tr>"
        )
    return out


_GOAL_UA = {
    "lose": "🔥 Схуднути",
    "maintain": "⚖️ Підтримувати",
    "gain": "💪 Набрати м'язи",
}


def _water_card(total_ml: int, target_ml: int) -> str:
    """Compact single-row water bar (fits inside any card)."""
    total_ml = max(0, int(total_ml or 0))
    target_ml = max(1, int(target_ml or 2000))
    total_l = total_ml / 1000
    target_l = target_ml / 1000
    pct = (total_ml / target_ml) if target_ml else 0
    pct_int = round(pct * 100)
    width = max(0, min(100, pct_int))
    return (
        f'<div class="water-row">'
        f'<div class="water-label"><span>💧 Вода</span>'
        f'<b>{total_l:.2f} / {target_l:.1f} л ({pct_int}%)</b></div>'
        f'<div class="water-bar"><div class="water-fill" style="width:{width}%"></div></div>'
        f'</div>'
    )


def _summary_card(cal: float, p: float, c: float, f: float,
                  cal_target: int, macros: dict) -> str:
    """Rule-based end-of-day bullet summary. Takes per-user targets as args."""
    cal_pct = round((cal / cal_target) * 100) if cal_target else 0
    p_pct = (p / macros["protein"]) if macros.get("protein") else 0
    c_pct = (c / macros["carbs"]) if macros.get("carbs") else 0
    f_pct = (f / macros["fat"]) if macros.get("fat") else 0

    if cal == 0:
        headline = "Ще нічого не записано — додай перший прийом їжі."
    elif cal_pct < 30:
        headline = f"Поки {cal_pct}% цілі по калоріях — день тільки набирає обертів."
    elif cal_pct < 70:
        headline = f"{cal_pct}% цілі по калоріях — середина дня, тримай курс."
    elif cal_pct <= 105:
        headline = f"{cal_pct}% цілі — норма майже виконана 💪"
    else:
        headline = f"{cal_pct}% цілі — перевищено норму на {round(cal - cal_target)} ккал."

    bullets: list[str] = []
    if p_pct < 0.7:
        bullets.append("🍗 Білків мало — додай яйця, курку, рибу або сир.")
    elif p_pct >= 1.0:
        bullets.append("🍗 Білків достатньо — відмінно.")

    if c_pct < 0.5:
        bullets.append("🍞 Вуглеводів мало — додай крупу, фрукти або хліб.")
    elif c_pct > 1.2:
        bullets.append("🍞 Вуглеводів забагато — завтра менше солодкого й хліба.")

    if f_pct < 0.5:
        bullets.append("🥑 Жирів мало — додай горіхи, олію або авокадо.")
    elif f_pct > 1.3:
        bullets.append("🥑 Жирів забагато — завтра менше масла й смаженого.")

    if cal and cal < cal_target * 0.85:
        bullets.append("🍽️ Додай ще їжі — калорійності для дня замало.")
    elif cal > cal_target * 1.05:
        bullets.append("🍽️ Калорії вже з запасом — завтра легший старт.")

    bullets_html = "".join(f'<p class="sum-line">{b}</p>' for b in bullets)
    return f'<p class="sum-head">{_esc(headline)}</p>{bullets_html}'


def _hero_card(cal: float, p: float, water_ml: int, hours_left: int,
               date_str: str, cal_target: int, macros: dict) -> str:
    """Big above-the-fold: remaining kcal + status chip + context pills."""
    target = cal_target or 1
    r = cal / target if target else 0
    if cal == 0:
        chip_cls, chip_emoji, chip_text = "info", "⚪", "ще нічого"
    elif r > 1.15:
        chip_cls, chip_emoji, chip_text = "over", "🔴", "перебір"
    elif r > 1.05:
        chip_cls, chip_emoji, chip_text = "warn", "🟡", "майже впритик"
    elif r >= 0.85:
        chip_cls, chip_emoji, chip_text = "ok", "🟢", "на місці"
    elif hours_left < 4:
        chip_cls, chip_emoji, chip_text = "warn", "🟡", "дожени"
    else:
        chip_cls, chip_emoji, chip_text = "info", "🔵", "простір є"

    remaining = cal_target - cal
    if cal > cal_target:
        big_html = (
            f'<div class="big over">−{round(abs(remaining)):,}</div>'
            f'<div class="unit">ккал перебір</div>'
        )
    else:
        big_html = (
            f'<div class="big">{round(max(0, remaining)):,}</div>'
            f'<div class="unit">ккал лишилось</div>'
        )

    time_hint = f"До кінця дня ~{hours_left} год" if hours_left > 0 else "День скоро зміниться"
    p_target = macros.get("protein") or 0
    water_l = water_ml / 1000

    return (
        f'<div class="hero">'
        f'<div class="hero-head">📊 {_esc(date_str)}</div>'
        f'{big_html}'
        f'<div><span class="status-chip {chip_cls}">{chip_emoji} {chip_text}</span></div>'
        f'<div class="sub2">{time_hint}</div>'
        f'<div class="pills">Білки: {round(p)}/{p_target} г · Вода: {water_l:.1f} л</div>'
        f'</div>'
    )


def _quick_actions_html(water_today_ml: int) -> str:
    undo_attr = '' if water_today_ml > 0 else ' disabled'
    return (
        '<div class="quick-actions">'
        '<button type="button" class="qa-btn" data-action="water_add:250">💧 +250 мл</button>'
        f'<button type="button" class="qa-btn qa-btn-ghost" data-action="water_undo"{undo_attr}>↩️ Скасувати</button>'
        '<button type="button" class="qa-btn qa-btn-ghost" data-close="1">💬 До бота</button>'
        '</div>'
    )


def _goal_header_html(profile: dict | None) -> str:
    goal = (profile or {}).get("goal") or ""
    label = _GOAL_UA.get(goal, "")
    if not label:
        return ""
    return f'<p class="goal">🎯 {_esc(label)}</p>'


def _adherence_line(week_rows: list[dict], cal_target: int) -> str:
    target = cal_target or 1
    days_logged = sum(1 for r in week_rows if (r.get("calories") or 0) > 0)
    if days_logged == 0:
        return "Твій прогрес · почнемо сьогодні"
    days_in_range = sum(
        1 for r in week_rows
        if (r.get("calories") or 0) > 0
        and 0.85 <= (r["calories"] / target) <= 1.05
    )
    return f"За 7 днів: {days_in_range}/{days_logged} днів у цілі"


def _dispatch_action(conn, user_id: int, action: str) -> None:
    """Handle POST actions from the dashboard. Unknown actions ignored silently."""
    if action == "water_add:250":
        add_water(conn, user_id, 250)
    elif action == "water_undo":
        remove_last_water_today(conn, user_id)


def _macro_row(label: str, value: float, target: float, unit: str) -> str:
    """One-line flex row: label · filled bar · value/target."""
    pct = 0.0
    if target and target > 0:
        pct = max(0.0, min(100.0, (value / target) * 100))
    return (
        f'<div class="macro">'
        f'<span class="macro-name">{_esc(label)}</span>'
        f'<div class="macro-fill-wrap"><div class="macro-fill" style="width:{pct:.1f}%"></div></div>'
        f'<b class="macro-value">{round(value)} / {round(target)} {_esc(unit)}</b>'
        f'</div>'
    )


def _render_meal_list(meals: list[dict], empty_msg: str) -> str:
    """Render a filterable meal list. Each meal gets data-* attrs for client JS."""
    if not meals:
        return f"<p class='empty'>{empty_msg}</p>"
    out = ""
    for m in meals:
        mt = m.get("meal_type", "") or ""
        desc = (m.get("description") or "")
        allergen_count = len(m.get("allergen_warnings") or [])
        crohn_count = len(m.get("crohn_warnings") or [])
        badges = ""
        # No allergies for this profile — allergen_count should always be 0;
        # keep the badge render defensive in case legacy rows exist.
        if allergen_count:
            badges += f"<span class='badge badge-allergen' title='Алергени'>⚠️ {allergen_count}</span>"
        if crohn_count:
            badges += f"<span class='badge badge-crohn' title='Нотатки здоров''я'>💡 {crohn_count}</span>"
        out += (
            f"<div class='meal' data-type='{_esc(mt)}' "
            f"data-desc='{_esc(desc.lower())}' "
            f"data-has-allergen='{1 if allergen_count else 0}' "
            f"data-has-crohn='{1 if crohn_count else 0}'>"
            f"<div class='meal-head'>{_MEAL_TYPE_UA.get(mt, mt)}"
            f" · <span class='kcal'>{round(m.get('calories',0))} ккал</span> {badges}</div>"
            f"<div class='meal-desc'>{_esc(desc[:160])}</div>"
            f"<div class='meal-macros'>Б {round(m.get('protein_g',0))}г · "
            f"В {round(m.get('carbs_g',0))}г · "
            f"Ж {round(m.get('fat_g',0))}г</div>"
            f"</div>"
        )
    return out


def _render_filter_bar(prefix: str, search_placeholder: str) -> str:
    """Shared filter-bar HTML. JS in the page attaches to ids that use `prefix`."""
    return f"""
    <div class="filter-bar">
      <input type="search" id="{prefix}Search" placeholder="🔍 {search_placeholder}">
      <div class="chips" id="{prefix}TypeChips">
        <button class="chip active" data-type="">Всі</button>
        <button class="chip" data-type="breakfast">🍳 Сніданок</button>
        <button class="chip" data-type="lunch">🥗 Обід</button>
        <button class="chip" data-type="dinner">🍽️ Вечеря</button>
        <button class="chip" data-type="snack">🍎 Перекус</button>
      </div>
      <div class="chips" id="{prefix}ToggleChips" style="margin-top: 6px;">
        <button class="chip toggle-crohn" data-flag="crohn">💡 Тільки з нотатками здоров'я</button>
      </div>
      <div class="filter-count" id="{prefix}FilterCount"></div>
    </div>"""


def _render_dashboard(user: dict) -> str:
    from datetime import datetime, timedelta

    user_id = user["id"]
    first_name = user.get("first_name") or "друже"

    yday_date = (datetime.now(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        init_db(conn)
        profile = get_profile(conn, user_id)
        log = get_today_log(conn, user_id)
        today_meals = get_meals_for_day(conn, user_id, log["date"])
        yday_log = get_log_for_date(conn, user_id, yday_date)
        yday_meals = get_meals_for_day(conn, user_id, yday_date)
        week = get_history(conn, user_id, days=7)
        month = get_history(conn, user_id, days=30)
        water_total = get_water_today(conn, user_id)
        water_target = get_water_target(conn, user_id)
        water_yday = get_water_for_date(conn, user_id, yday_date)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    cal = log.get("calories") or 0
    p = log.get("protein") or 0
    c = log.get("carbs") or 0
    f = log.get("fat") or 0
    meal_count = log.get("meal_count") or 0

    y_cal = yday_log.get("calories") or 0
    y_p = yday_log.get("protein") or 0
    y_c = yday_log.get("carbs") or 0
    y_f = yday_log.get("fat") or 0
    y_meal_count = yday_log.get("meal_count") or 0

    cal_target = (profile or {}).get("daily_calorie_target") or 2000
    weight_kg = (profile or {}).get("weight_kg")
    goal = (profile or {}).get("goal")
    if weight_kg and goal:
        macros = macro_gram_targets_from_profile(weight_kg, goal)
    else:
        macros = macro_gram_targets(cal_target)

    week_agg = _aggregate(week)
    month_agg = _aggregate(month)

    today_meals_html = _render_meal_list(today_meals, "Сьогодні ще нічого не записано.")
    yday_meals_html = _render_meal_list(yday_meals, "Вчора нічого не записано.")

    week_rows = _render_history_table(week)
    month_rows = _render_history_table(month)

    day_summary_html = _summary_card(cal, p, c, f, cal_target, macros)
    yday_summary_html = _summary_card(y_cal, y_p, y_c, y_f, cal_target, macros)
    water_html = _water_card(water_total, water_target)
    yday_water_html = _water_card(water_yday, water_target)

    now_kyiv = datetime.now(LOCAL_TZ)
    hours_left = max(0, 24 - now_kyiv.hour)
    hero_html = _hero_card(cal, p, water_total, hours_left, log.get("date", ""), cal_target, macros)
    goal_html = _goal_header_html(profile)
    adherence_line = _adherence_line(week, cal_target)
    quick_actions_html = _quick_actions_html(water_total)

    day_filter_bar = _render_filter_bar("day", "Пошук (назва, тип…)")
    yday_filter_bar = _render_filter_bar("yesterday", "Пошук (назва, тип…)")

    _js_bot_url = json.dumps(
        f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else ""
    )

    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Food Tracker</title>
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0;
         padding: max(16px, calc(var(--tg-safe-area-inset-top, env(safe-area-inset-top, 0px)) + var(--tg-content-safe-area-inset-top, 0px))) 16px 16px;
         font-family: -apple-system, system-ui, 'Segoe UI', sans-serif;
         background: #0f0f1a; color: #e0e0e0; font-size: 15px; line-height: 1.45; }}
  h1 {{ margin: 0 0 4px; color: #e94560; font-size: 1.4em; }}
  .sub {{ color: #888; font-size: 0.9em; margin-bottom: 18px; }}
  .goal {{ color: #ffbb5b; font-size: 0.88em; margin: 0 0 2px; line-height: 1.35; }}

  .tabs {{ display: flex; gap: 6px; background: #16213e; padding: 4px; border-radius: 10px;
          margin-bottom: 14px; }}
  .tab {{ flex: 1; padding: 8px 10px; border: none; background: transparent; color: #bdbdd0;
         font-size: 0.95em; border-radius: 7px; cursor: pointer; font-family: inherit; }}
  .tab.active {{ background: #e94560; color: #fff; font-weight: 600; }}
  .tab:not(.active):hover {{ background: #1e2e52; }}

  .view {{ display: none; }}
  .view.active {{ display: block; }}

  .card {{ background: #16213e; border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; }}
  .card h2 {{ margin: 0 0 10px; font-size: 1.05em; color: #e0e0e0; }}

  /* One-line macro row (label · bar · value) */
  .macro {{ display: flex; align-items: center; gap: 10px; margin: 10px 0; }}
  .macro-name {{ flex: 0 0 auto; width: 90px; color: #bdbdd0; font-size: 0.88em; }}
  .macro-fill-wrap {{ flex: 1 1 auto; height: 8px; background: #0f0f1a;
                      border-radius: 4px; overflow: hidden; min-width: 40px; }}
  .macro-fill {{ height: 100%; background: #e94560; border-radius: 4px; }}
  .macro-value {{ flex: 0 0 auto; color: #e0e0e0; font-size: 0.84em;
                  font-weight: 500; white-space: nowrap; }}

  /* Hero card (above-the-fold big number + status) */
  .hero {{ background: linear-gradient(135deg, #1e2e52 0%, #16213e 100%);
           border-radius: 14px; padding: 22px 18px; margin-bottom: 12px; text-align: center; }}
  .hero-head {{ color: #888; font-size: 0.85em; margin-bottom: 10px; }}
  .hero .big {{ font-size: 2.8em; font-weight: 700; color: #e0e0e0; line-height: 1; }}
  .hero .big.over {{ color: #e94560; }}
  .hero .unit {{ color: #888; font-size: 0.9em; margin-top: 4px; margin-bottom: 10px; }}
  .status-chip {{ display: inline-block; padding: 4px 10px; border-radius: 999px;
                  font-size: 0.82em; font-weight: 600; }}
  .status-chip.ok    {{ background: #133a2b; color: #4caf82; }}
  .status-chip.warn  {{ background: #4a3a1e; color: #ffbb5b; }}
  .status-chip.over  {{ background: #4a1e2a; color: #ff6b7f; }}
  .status-chip.info  {{ background: #1e3a4a; color: #6bb5ff; }}
  .hero .sub2 {{ color: #bdbdd0; margin-top: 8px; font-size: 0.88em; }}
  .hero .pills {{ margin-top: 10px; color: #bdbdd0; font-size: 0.82em; }}

  /* Quick-action buttons under the hero */
  .quick-actions {{ display: grid; grid-template-columns: 1fr 1fr 1fr;
                    gap: 8px; margin-bottom: 14px; }}
  .qa-btn {{ padding: 12px 6px; border: none; border-radius: 10px;
             background: #e94560; color: #fff; font-size: 0.9em;
             font-weight: 600; cursor: pointer; font-family: inherit; width: 100%; }}
  .qa-btn:active {{ transform: scale(0.97); }}
  .qa-btn-ghost {{ background: #1e2e52; color: #e0e0e0; }}
  .qa-btn[disabled] {{ opacity: 0.4; cursor: not-allowed; }}

  /* Collapsible details card */
  .details-card {{ padding: 12px 16px; }}
  .details-card > summary {{ cursor: pointer; font-size: 1em; color: #e0e0e0;
                             font-weight: 600; padding: 4px 0; list-style: none;
                             display: flex; align-items: center; gap: 6px; }}
  .details-card > summary::-webkit-details-marker {{ display: none; }}
  .details-card[open] > summary {{ margin-bottom: 10px; }}
  .details-card .chev {{ display: inline-block; color: #e94560; font-weight: 700;
                         transition: transform 0.18s ease; }}
  .details-card[open] .chev {{ transform: rotate(90deg); }}

  /* Water row (compact) */
  .water-row {{ margin: 4px 0; }}
  .water-label {{ display: flex; justify-content: space-between; font-size: 0.9em;
                  color: #bdbdd0; margin-bottom: 6px; }}
  .water-label b {{ color: #e0e0e0; }}
  .water-bar {{ height: 8px; background: #0f0f1a; border-radius: 4px; overflow: hidden; }}
  .water-fill {{ height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa); border-radius: 4px; }}

  /* Daily summary bullets */
  .sum-head {{ margin: 0 0 10px; font-size: 0.95em; color: #e0e0e0; line-height: 1.5; }}
  .sum-line {{ margin: 6px 0; font-size: 0.9em; color: #bdbdd0; line-height: 1.5; }}

  .meal {{ padding: 10px 0; border-bottom: 1px solid #1e1e3a; }}
  .meal:last-child {{ border-bottom: none; }}
  .meal-head {{ font-weight: 600; margin-bottom: 2px; }}
  .meal-head .kcal {{ color: #888; font-weight: 400; font-size: 0.9em; }}
  .meal-desc {{ color: #bdbdd0; font-size: 0.9em; margin-bottom: 2px; }}
  .meal-macros {{ color: #888; font-size: 0.8em; }}

  .badge {{ display: inline-block; font-size: 0.7em; padding: 1px 6px; border-radius: 6px;
           margin-left: 4px; vertical-align: middle; font-weight: 500; }}
  .badge-allergen {{ background: #4a1e2a; color: #ff6b7f; }}
  .badge-crohn {{ background: #4a3a1e; color: #ffbb5b; }}

  .filter-bar {{ margin-bottom: 12px; }}
  .filter-bar input[type="search"] {{
    width: 100%; background: #0f0f1a; color: #e0e0e0; border: 1px solid #2a2a4a;
    padding: 8px 12px; border-radius: 8px; font-size: 0.9em; margin-bottom: 8px;
    font-family: inherit;
  }}
  .filter-bar input[type="search"]::placeholder {{ color: #666; }}
  .filter-bar input[type="search"]:focus {{ outline: none; border-color: #e94560; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .chip {{ padding: 4px 10px; border-radius: 14px; background: #0f0f1a; color: #bdbdd0;
         font-size: 0.8em; cursor: pointer; border: 1px solid #2a2a4a;
         font-family: inherit; user-select: none; }}
  .chip.active {{ background: #e94560; color: #fff; border-color: #e94560; }}
  .chip.toggle.active {{ background: #4a1e2a; color: #ff6b7f; border-color: #ff6b7f; }}
  .chip.toggle-crohn.active {{ background: #4a3a1e; color: #ffbb5b; border-color: #ffbb5b; }}

  .filter-count {{ font-size: 0.8em; color: #666; margin-top: 6px; }}

  .stats {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 10px; }}
  .stat {{ background: #0f0f1a; padding: 10px; border-radius: 8px; text-align: center; }}
  .stat .v {{ font-size: 1.3em; font-weight: 600; color: #e94560; }}
  .stat .l {{ font-size: 0.75em; color: #888; margin-top: 2px; text-transform: uppercase; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #1e1e3a; }}
  th {{ color: #888; font-weight: 500; font-size: 0.8em; text-transform: uppercase; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}

  .empty {{ color: #666; text-align: center; padding: 12px; }}
</style>
</head>
<body>

<h1>👋 Привіт, {_esc(first_name)}!</h1>
{goal_html}
<p class="sub">{_esc(adherence_line)}</p>

<div class="tabs" role="tablist">
  <button class="tab active" data-view="day" role="tab">День</button>
  <button class="tab" data-view="yesterday" role="tab">Вчора</button>
  <button class="tab" data-view="week" role="tab">7 днів</button>
  <button class="tab" data-view="month" role="tab">30 днів</button>
</div>

<!-- ============ DAY VIEW ============ -->
<div class="view active" data-view="day">
  {hero_html}

  {quick_actions_html}

  <div class="card">
    <h2>📋 Деталі дня</h2>
    {_macro_row("Калорії", cal, cal_target, "ккал")}
    {_macro_row("Білки", p, macros['protein'], "г")}
    {_macro_row("Вуглеводи", c, macros['carbs'], "г")}
    {_macro_row("Жири", f, macros['fat'], "г")}
    <p class="sub" style="margin-top:10px">Страв записано: {meal_count}</p>
  </div>

  <div class="card">
    <h2>💧 Вода</h2>
    {water_html}
  </div>

  <div class="card">
    <h2>💡 Підсумок дня</h2>
    {day_summary_html}
  </div>

  <div class="card">
    <h2>🍽️ Страви сьогодні</h2>
    {day_filter_bar}
    <div id="dayMealsList">{today_meals_html}</div>
  </div>
</div>

<!-- ============ YESTERDAY VIEW ============ -->
<div class="view" data-view="yesterday">
  <div class="card">
    <h2>📆 Вчора ({_esc(yday_date)})</h2>
    {_macro_row("Калорії", y_cal, cal_target, "ккал")}
    {_macro_row("Білки", y_p, macros['protein'], "г")}
    {_macro_row("Вуглеводи", y_c, macros['carbs'], "г")}
    {_macro_row("Жири", y_f, macros['fat'], "г")}
    <p class="sub" style="margin-top:10px">Страв записано: {y_meal_count}</p>
  </div>

  <div class="card">
    <h2>💧 Вода</h2>
    {yday_water_html}
  </div>

  <div class="card">
    <h2>💡 Підсумок дня</h2>
    {yday_summary_html}
  </div>

  <div class="card">
    <h2>🍽️ Страви вчора</h2>
    {yday_filter_bar}
    <div id="yesterdayMealsList">{yday_meals_html}</div>
  </div>
</div>

<!-- ============ WEEK VIEW ============ -->
<div class="view" data-view="week">
  <div class="card">
    <h2>📅 Останні 7 днів</h2>
    <div class="stats">
      <div class="stat"><div class="v">{week_agg['days']}</div><div class="l">днів з даними</div></div>
      <div class="stat"><div class="v">{week_agg['avg_cal']}</div><div class="l">сер. ккал/день</div></div>
      <div class="stat"><div class="v">{week_agg['avg_p']}</div><div class="l">сер. білків (г)</div></div>
      <div class="stat"><div class="v">{week_agg['avg_c']}</div><div class="l">сер. вуглеводів (г)</div></div>
    </div>
    <table>
      <thead><tr>
        <th>Дата</th>
        <th class="num">ккал</th>
        <th class="num">Б</th>
        <th class="num">В</th>
        <th class="num">Ж</th>
      </tr></thead>
      <tbody>{week_rows}</tbody>
    </table>
  </div>
</div>

<!-- ============ MONTH VIEW ============ -->
<div class="view" data-view="month">
  <div class="card">
    <h2>📅 Останні 30 днів</h2>
    <div class="stats">
      <div class="stat"><div class="v">{month_agg['days']}</div><div class="l">днів з даними</div></div>
      <div class="stat"><div class="v">{month_agg['avg_cal']}</div><div class="l">сер. ккал/день</div></div>
      <div class="stat"><div class="v">{month_agg['avg_p']}</div><div class="l">сер. білків (г)</div></div>
      <div class="stat"><div class="v">{month_agg['avg_c']}</div><div class="l">сер. вуглеводів (г)</div></div>
    </div>
    <table>
      <thead><tr>
        <th>Дата</th>
        <th class="num">ккал</th>
        <th class="num">Б</th>
        <th class="num">В</th>
        <th class="num">Ж</th>
      </tr></thead>
      <tbody>{month_rows}</tbody>
    </table>
  </div>
</div>

<script>
  var TG = (window.Telegram && window.Telegram.WebApp) || null;
  var BOT_URL = {_js_bot_url};
  if (TG) {{
    try {{ TG.ready(); }} catch(e) {{}}
    try {{ TG.expand(); }} catch(e) {{}}
  }}

  /* --- POST an action back to /api/dashboard and swap the HTML with the response --- */
  function doAction(action, btn) {{
    if (btn && btn.disabled) return;
    var initData = (TG && TG.initData) || '';
    var body = 'action=' + encodeURIComponent(action) +
               '&initData=' + encodeURIComponent(initData);
    if (btn) {{ btn.disabled = true; btn.style.opacity = 0.5; }}
    fetch(window.location.pathname, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
      body: body,
      credentials: 'same-origin'
    }}).then(function(r) {{ return r.text(); }}).then(function(html) {{
      document.open(); document.write(html); document.close();
    }}).catch(function(e) {{
      if (btn) {{ btn.disabled = false; btn.style.opacity = 1; }}
      console.error('action failed', e);
    }});
  }}
  document.querySelectorAll('[data-action]').forEach(function(el) {{
    el.addEventListener('click', function() {{ doAction(el.dataset.action, el); }});
  }});

  function closeApp() {{
    /* Always navigate to the bot chat (openTelegramLink is a no-op when
       already in that chat), then always close the Mini App overlay.
       Tried gating the navigate on launch context — it broke the menu
       button path. Do both unconditionally. */
    if (BOT_URL && TG && typeof TG.openTelegramLink === 'function') {{
      try {{ TG.openTelegramLink(BOT_URL); }} catch(e) {{}}
    }}
    if (TG && typeof TG.close === 'function') {{
      try {{ TG.close(); return; }} catch(e) {{}}
    }}
    try {{ window.close(); }} catch(e) {{}}
    try {{ history.back(); }} catch(e) {{}}
  }}
  document.querySelectorAll('[data-close]').forEach(function(el) {{
    el.addEventListener('click', closeApp);
  }});

  document.querySelectorAll('.tab').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var target = btn.dataset.view;
      document.querySelectorAll('.tab').forEach(function(b) {{ b.classList.toggle('active', b.dataset.view === target); }});
      document.querySelectorAll('.view').forEach(function(v) {{ v.classList.toggle('active', v.dataset.view === target); }});
    }});
  }});

  /* --- Reusable meal filter init (day view, yesterday view) --- */
  function initMealFilters(prefix) {{
    var activeType = '';
    var onlyAllergen = false;
    var onlyCrohn = false;
    var searchEl = document.getElementById(prefix + 'Search');
    var countEl = document.getElementById(prefix + 'FilterCount');
    var typeChipsEl = document.getElementById(prefix + 'TypeChips');
    var toggleChipsEl = document.getElementById(prefix + 'ToggleChips');
    var listEl = document.getElementById(prefix + 'MealsList');
    if (!searchEl || !listEl) return;
    var meals = Array.from(listEl.querySelectorAll('.meal'));
    if (meals.length === 0) return;

    function apply() {{
      var q = (searchEl.value || '').toLowerCase().trim();
      var visible = 0;
      meals.forEach(function(m) {{
        var show = true;
        if (activeType && m.dataset.type !== activeType) show = false;
        if (onlyAllergen && m.dataset.hasAllergen !== '1') show = false;
        if (onlyCrohn && m.dataset.hasCrohn !== '1') show = false;
        if (q && m.dataset.desc.indexOf(q) === -1) show = false;
        m.style.display = show ? '' : 'none';
        if (show) visible++;
      }});
      countEl.textContent = 'Показано ' + visible + ' / ' + meals.length;
    }}

    typeChipsEl.querySelectorAll('.chip').forEach(function(chip) {{
      chip.addEventListener('click', function() {{
        activeType = chip.dataset.type;
        typeChipsEl.querySelectorAll('.chip').forEach(function(c) {{
          c.classList.toggle('active', c === chip);
        }});
        apply();
      }});
    }});
    toggleChipsEl.querySelectorAll('.chip').forEach(function(chip) {{
      chip.addEventListener('click', function() {{
        var isActive = chip.classList.toggle('active');
        if (chip.dataset.flag === 'allergen') onlyAllergen = isActive;
        if (chip.dataset.flag === 'crohn') onlyCrohn = isActive;
        apply();
      }});
    }});
    searchEl.addEventListener('input', apply);
    apply();
  }}
  initMealFilters('day');
  initMealFilters('yesterday');
</script>
</body></html>"""
