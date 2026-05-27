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
import secrets
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, date as _date
from http.server import BaseHTTPRequestHandler
from typing import Optional

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
    get_streak,
    get_weight_history,
    get_latest_recommendation,
)
from lib.log import setup_sentry, http_handler, error
from lib.i18n import t as _i18n_t
from lib.i18n.plurals import pluralize


# F-2b Chunk 7: keys whose values get serialized into the dashboard JS so the
# inline script can render dynamic strings (banners, calories summary, plurals)
# in the user's locale. Static labels (h1 / h2 / button text) interpolate
# into HTML server-side via __LABEL_FOO__ placeholders.
_DASHBOARD_JS_KEYS = (
    "dash.water_units", "dash.cal_subtitle", "dash.cal_remaining_unit",
    "dash.macro_protein", "dash.macro_carbs", "dash.macro_fat",
    "dash.share_status_preparing", "dash.share_status_sent",
    "dash.share_status_failed", "dash.share_status_network",
    "dash.dow_mon", "dash.dow_tue", "dash.dow_wed", "dash.dow_thu",
    "dash.dow_fri", "dash.dow_sat", "dash.dow_sun",
    "dash.month_jan", "dash.month_feb", "dash.month_mar", "dash.month_apr",
    "dash.month_may", "dash.month_jun", "dash.month_jul", "dash.month_aug",
    "dash.month_sep", "dash.month_oct", "dash.month_nov", "dash.month_dec",
    "dash.today_label",
    "dash.summary_empty_today", "dash.summary_empty_other",
    "dash.summary_cal_over", "dash.summary_cal_ok", "dash.summary_cal_under",
    "dash.summary_under_today_tail", "dash.summary_cal_low",
    "dash.summary_macro_low", "dash.summary_macro_high",
    "dash.macro_proteins", "dash.macro_carbs_g", "dash.macro_fats",
    "dash.summary_meals_water",
    "dash.meal_breakfast", "dash.meal_lunch", "dash.meal_dinner",
    "dash.meal_snack", "dash.meal_other",
    "dash.meals_pct_of_goal", "dash.meals_summary_kcal_of",
    "dash.meal_empty_other",
    "dash.profile_field_name", "dash.profile_field_age",
    "dash.profile_field_age_unit", "dash.profile_field_sex",
    "dash.profile_field_weight", "dash.profile_field_height",
    "dash.profile_field_gym", "dash.profile_field_goal",
    "dash.profile_target_weight", "dash.profile_target_done",
    "dash.profile_target_togo", "dash.profile_target_plain",
    "dash.profile_weekly_delta", "dash.profile_weekly_delta_v",
    "dash.profile_projection", "dash.profile_projection_weeks",
    "dash.profile_status_ahead", "dash.profile_status_on_track",
    "dash.profile_status_behind", "dash.profile_pace",
    "dash.profile_pace_actual",
    "dash.targets_calories", "dash.targets_protein", "dash.targets_carbs",
    "dash.targets_fat", "dash.targets_water",
    "dash.avg_empty", "dash.adherence_empty", "dash.avg_subtitle",
    "dash.streak_singular", "dash.streak_few", "dash.streak_many",
    "dash.unit_kcal", "dash.unit_g", "dash.unit_kg", "dash.unit_l",
    "dash.unit_cm",
    "dash.meal_macro_p", "dash.meal_macro_c", "dash.meal_macro_f",
    "dash.meal_macro_fi", "dash.meal_macro_su",
    "dash.coach_title_with_date", "dash.warn_chip", "dash.warn_detail_label",
    "dash.cal_bars_axis", "dash.weight_chart_empty",
)


def _build_js_labels(locale: str) -> str:
    """Serialize the JS-side labels dict as JSON ready to inject into the page."""
    labels = {}
    for key in _DASHBOARD_JS_KEYS:
        short = key.split(".", 1)[1]
        labels[short] = _i18n_t(key, locale=locale)
    return json.dumps(labels, ensure_ascii=False)


def _locale_from_request(path: str, form: dict | None = None) -> str:
    """Pick a supported locale from ?lang= query first, then form data, then en."""
    try:
        qs = urllib.parse.urlsplit(path).query
        params = urllib.parse.parse_qs(qs)
        candidate = (params.get("lang") or [""])[0].lower()
        if candidate in ("uk", "en"):
            return candidate
    except Exception:
        pass
    if form:
        try:
            candidate = (form.get("lang") or [""])[0].lower()
            if candidate in ("uk", "en"):
                return candidate
        except Exception:
            pass
    return "en"

setup_sentry("dashboard")


INIT_DATA_MAX_AGE = 24 * 60 * 60  # 24h, per Telegram recommendation
HISTORY_MAX_DAYS = 90              # how far back the day spinner is allowed
PRELOAD_DAYS = 30                  # aggregates sent with the initial render

# Static headers (CSP is built per-response so we can use a per-request nonce
# in place of 'unsafe-inline' for scripts).
_STATIC_SECURITY_HEADERS = [
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    # L6: explicit Permissions-Policy denial — defense in depth alongside the
    # restrictive CSP. Telegram Mini Apps don't need any of these capabilities.
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"),
    ("Cache-Control", "no-store, no-cache, must-revalidate, private, max-age=0"),
    ("CDN-Cache-Control", "no-store"),
    ("Vercel-CDN-Cache-Control", "no-store"),
    ("Surrogate-Control", "no-store"),
    ("Pragma", "no-cache"),
    ("Expires", "0"),
]


def _csp_with_nonce(nonce: str) -> str:
    """Per-request CSP. Inline scripts must carry the matching nonce attribute.
    External script-src is restricted to telegram.org for the WebApp SDK.
    Inline styles still rely on 'unsafe-inline' — Mini App theming requires
    setting CSS custom properties dynamically, which 'unsafe-inline' enables."""
    return (
        "default-src 'none'; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{nonce}' https://telegram.org; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors https://web.telegram.org https://t.me"
    )


def _new_nonce() -> str:
    return secrets.token_urlsafe(16)


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
    """Coerce the log dict from get_log_for_date() into JSON-safe numbers.

    REV #9: get_log_for_date renames the DB columns total_fiber_g / total_sugar_g
    to fiber / sugar on the dict, so we read those keys here. Adding total_*
    column names to .get() would silently coalesce to 0 for everyone.
    """
    return {
        "date": str(log_dict.get("date") or ""),
        "calories": round(log_dict.get("calories") or 0),
        "protein": round(log_dict.get("protein") or 0),
        "carbs": round(log_dict.get("carbs") or 0),
        "fat": round(log_dict.get("fat") or 0),
        "fiber": round(log_dict.get("fiber") or 0),
        "sugar": round(log_dict.get("sugar") or 0),
        "meal_count": int(log_dict.get("meal_count") or 0),
    }


def _meal_to_json(m: dict) -> dict:
    # allergen_warnings / crohn_warnings are stored as TEXT containing JSON
    # arrays; get_meals_for_day already json.loads them so by the time they
    # reach this helper they are Python lists.
    return {
        "id": m.get("id"),
        "meal_type": m.get("meal_type") or "",
        "description": m.get("description") or "",
        "calories": round(m.get("calories") or 0),
        "protein_g": round(m.get("protein_g") or 0),
        "carbs_g": round(m.get("carbs_g") or 0),
        "fat_g": round(m.get("fat_g") or 0),
        "fiber_g": round(m.get("fiber_g") or 0),
        "sugar_g": round(m.get("sugar_g") or 0),
        "allergen_warnings": list(m.get("allergen_warnings") or []),
        "crohn_warnings": list(m.get("crohn_warnings") or []),
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


def _fiber_sugar_targets(profile: dict, cal_target: int) -> tuple[int, int]:
    """Per-user daily fiber goal and added-sugar cap.

    Fiber: 14 g per 1000 kcal (USDA Dietary Guidelines for Americans), clamped
    to 20–45 g. Sex-agnostic on purpose — scales naturally with the user's
    calorie target so men (typically higher target) end up around 28–38 g and
    women around 22–28 g.

    Sugar: AHA added-sugar caps — 25 g for females, 36 g for males. Defaults
    to 36 g (the more lenient bound) when sex is unset, so users don't get
    falsely flagged as "over" before completing their profile.
    """
    fiber = max(20, min(45, round(cal_target * 14 / 1000)))
    sex = (profile.get("sex") or "").lower()
    sugar = 25 if sex.startswith("f") else 36
    return int(fiber), int(sugar)


def _build_streak_line(streak: Optional[dict], locale: str) -> Optional[str]:
    """Pre-render the streak line server-side. Returns None to hide the row.

    Plurals are rendered server-side (Slavic 1/few/many) — JS has no plural
    helper, so we ship the finished string in `data.streak_line`.
    """
    if not streak:
        return None
    cur_n = int(streak.get("current_streak") or 0)
    best_n = int(streak.get("longest_streak") or 0)
    fr_n = int(streak.get("freeze_days_remaining") or 0)
    if cur_n < 1 and best_n < 1:
        return None

    day_s = _i18n_t("dash.day_w_singular", locale=locale)
    day_f = _i18n_t("dash.day_w_few", locale=locale)
    day_m = _i18n_t("dash.day_w_many", locale=locale)
    fr_s = _i18n_t("dash.freeze_w_singular", locale=locale)
    fr_f = _i18n_t("dash.freeze_w_few", locale=locale)
    fr_m = _i18n_t("dash.freeze_w_many", locale=locale)
    best_label = _i18n_t("dash.streak_best_label", locale=locale)

    cur_w = pluralize(cur_n, locale, day_s, day_f, day_m)
    best_w = pluralize(best_n, locale, day_s, day_f, day_m)
    fr_w = pluralize(fr_n, locale, fr_s, fr_f, fr_m)

    parts = [f"🔥 {cur_n} {cur_w}"]
    if best_n > cur_n:
        parts.append(f"🏆 {best_label} {best_n} {best_w}")
    if fr_n > 0:
        parts.append(f"❄️ {fr_n} {fr_w}")
    return " · ".join(parts)


def _dispatch_action(conn, user_id: int, action: str) -> None:
    # No mutating actions from the dashboard right now — water logging
    # lives in the bot only. Kept as a stub so the POST handler can call
    # it harmlessly and we have a single place to wire future actions.
    return


class handler(BaseHTTPRequestHandler):
    def _apply_security_headers(self, nonce: str | None = None):
        for name, value in _STATIC_SECURITY_HEADERS:
            self.send_header(name, value)
        # When a nonce is provided, the response embeds inline <script nonce=…>
        # blocks; otherwise emit a CSP that allows no inline scripts at all.
        self.send_header(
            "Content-Security-Policy",
            _csp_with_nonce(nonce if nonce is not None else "none"),
        )

    def _send_html(self, code: int, body: str, nonce: str | None = None):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._apply_security_headers(nonce=nonce)
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

    @http_handler("dashboard")
    def do_GET(self):
        nonce = _new_nonce()
        locale = _locale_from_request(self.path)
        self._send_html(200, _render_bootstrap(nonce=nonce, locale=locale), nonce=nonce)

    @http_handler("dashboard")
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

        # F-2b Chunk 7: URL-derived locale is just a hint for the unauth /
        # bootstrap render. Once we've verified initData and have a user_id,
        # the AUTHORITATIVE locale is profile.lang from the DB — which
        # survives /language toggles even though Telegram caches the chat
        # menu button URL with whatever locale was current at /start time.
        url_locale = _locale_from_request(self.path, form)

        user = _verify_init_data(init_data)
        if user is None:
            if action == "day_data":
                self._send_json(401, {"error": "unauthorized"})
            else:
                self._send_html(401, _unauthorized_html(locale=url_locale))
            return

        user_id = user["id"]
        first_name = user.get("first_name") or None

        # Read the authoritative locale from the user's profile in DB.
        # Falls back to the URL-derived locale only when no profile row exists
        # yet (i.e. user has not started onboarding).
        try:
            _conn_for_locale = get_conn()
            try:
                init_db(_conn_for_locale)
                _profile_for_locale = get_profile(_conn_for_locale, user_id)
            finally:
                try:
                    _conn_for_locale.close()
                except Exception:
                    pass
            from lib.i18n import locale_of as _locale_of
            locale = _locale_of(_profile_for_locale) if _profile_for_locale else url_locale
        except Exception:
            locale = url_locale

        # F-12.5 (dashboard share): generate the weekly recap PNG and send it
        # to the user's chat via the bot. Mini App stays on screen so the JS
        # can show a "card sent" toast → tg.close() shortly after.
        if action == "request_recap":
            from lib.recap import build_user_recap
            from lib.database import get_profile
            from lib.telegram_helpers import send_photo
            conn = get_conn()
            try:
                init_db(conn)
                profile = get_profile(conn, user_id)
                png, caption = build_user_recap(conn, user_id, profile, first_name)
                resp = send_photo(user_id, png, caption=caption)
            except Exception:
                print("dashboard recap error:", traceback.format_exc(), flush=True)
                self._send_json(500, {"ok": False, "error": "internal"})
                return
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            self._send_json(200, {"ok": bool(resp.get("ok"))})
            return

        # 2026-05: JSON endpoint that returns the same data blob the
        # full HTML render inlines via __DATA_JSON__. Phase 2 of the
        # dashboard refactor will switch the page to a fast shell HTML
        # that fires this XHR on load — eliminating the 5–7s spinner
        # wait between Telegram tapping "Dashboard" and seeing anything.
        if action == "initial_data":
            try:
                data = _load_initial_data(user, locale=locale)
            except Exception:
                print("initial_data error:", traceback.format_exc(), flush=True)
                self._send_json(500, {"error": "internal"})
                return
            self._send_json(200, data)
            return

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

        # No mutating actions from the dashboard right now (water logging
        # is bot-only). Empty tuple keeps the dispatch path future-proof.
        if action in ():
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

        nonce = _new_nonce()
        try:
            body = _render_dashboard(user, nonce=nonce, locale=locale)
        except Exception:
            print("dashboard render error:", traceback.format_exc(), flush=True)
            body = "<pre>Dashboard error (see logs)</pre>"
        self._send_html(200, body, nonce=nonce)


# ---------------------------------------------------------------- Bootstrap --

_BOOTSTRAP_HTML = """<!DOCTYPE html>
<html lang="__LANG__">
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
  <p>__LABEL_LOADING__</p>
</div>
<div id="error" style="display:none"></div>
<script nonce="__NONCE__">
(function(){
  var L = __JS_LABELS__;
  var LANG = '__LANG__';
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
      '<h2 class="err">' + L.err_title + '</h2>' +
      '<p>' + L.err_p1 + '</p>' +
      '<p>' + L.err_p2 + '</p>' +
      '<button onclick="location.reload()">' + L.retry + '</button>' +
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
    // 2026-05 dashboard Phase 2 hotfix #3: carry initData across the
    // form-submit navigation via sessionStorage. The chat-list /
    // direct-link entry path delivers tgWebAppData in the URL hash
    // but form-submit nav clears the hash — so the POST-rendered
    // shell can't recover initData via the legacy sources. Same-tab
    // sessionStorage survives the nav and is byte-identical to the
    // value found here (no Python encoding round-trip risk). The
    // Phase 2 bootstrap on the POST-rendered shell reads from
    // sessionStorage first.
    try {
      sessionStorage.setItem('__kuswise_initData__', initDataStr);
    } catch(e) {}
    // Submit a real form POST instead of fetch + document.write. With the
    // strict CSP nonce in place, document.write keeps the original page's
    // CSP active, which has the bootstrap's nonce — not the rendered
    // dashboard's nonce — so the rendered page's inline scripts get blocked.
    // A full form navigation lets the browser apply the new response's CSP
    // (and matching nonce) to the new document.
    var form = document.createElement('form');
    form.method = 'POST';
    form.action = window.location.pathname;
    form.style.display = 'none';
    var inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'initData';
    inp.value = initDataStr;
    form.appendChild(inp);
    // F-2b Chunk 7: forward locale to the POST handler so the dashboard
    // SSR uses the same language as the bootstrap.
    var langInp = document.createElement('input');
    langInp.type = 'hidden';
    langInp.name = 'lang';
    langInp.value = LANG;
    form.appendChild(langInp);
    document.body.appendChild(form);
    form.submit();
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


def _render_bootstrap(nonce: str, locale: str = "en") -> str:
    """Build the bootstrap page HTML for a given locale + CSP nonce."""
    bootstrap_js_keys = (
        "dash.bootstrap_err_title", "dash.bootstrap_err_p1",
        "dash.bootstrap_err_p2", "dash.bootstrap_retry",
    )
    js_labels = json.dumps(
        {k.split(".", 1)[1].replace("bootstrap_", ""): _i18n_t(k, locale=locale) for k in bootstrap_js_keys},
        ensure_ascii=False,
    )
    # Normalize key names so the JS object reads `L.err_title`, `L.retry`, etc.
    js_labels = (
        js_labels
        .replace('"err_title"', '"err_title"')  # already short
    )
    return (
        _BOOTSTRAP_HTML
        .replace("__NONCE__", _esc(nonce))
        .replace("__LANG__", locale)
        .replace("__LABEL_LOADING__", _esc(_i18n_t("dash.bootstrap_loading", locale=locale)))
        .replace("__JS_LABELS__", js_labels)
    )


def _unauthorized_html(locale: str = "en") -> str:
    h1 = _esc(_i18n_t("dash.unauth_h1", locale=locale))
    # _i18n_t returns the body raw (no <b> escaping needed; static text).
    p = _i18n_t("dash.unauth_p", locale=locale)
    return f"""<!DOCTYPE html>
<html lang="{locale}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KusWise Bot</title>
<link rel="icon" type="image/png" href="/logo.png">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; padding: 40px 20px;
         font-family: -apple-system, system-ui, sans-serif;
         background: var(--tg-theme-bg-color, #101014);
         color: var(--tg-theme-text-color, #e6e6ea); text-align: center; }}
  h1 {{ color: var(--tg-theme-destructive-text-color, #ef5b5b); }}
</style></head>
<body>
<h1>{h1}</h1>
<p>{_esc(p)}</p>
</body></html>"""


# ------------------------------------------------------------ Dashboard SSR --

_MEAL_TYPE_ORDER = ["breakfast", "lunch", "dinner", "snack"]


def _goal_label(goal: str | None, locale: str = "en") -> str:
    if not goal:
        return ""
    return _i18n_t(f"dash.goal_{goal}", locale=locale)


def _sex_label(sex: str | None, locale: str = "en") -> str:
    if not sex:
        return ""
    return _i18n_t(f"dash.sex_{sex}", locale=locale)


def _load_initial_data(user: dict, locale: str = "en") -> dict:
    """Build the full dashboard data blob that the page renders from.

    Returns the JSON-serializable dict that was previously inlined into
    the HTML via ``__DATA_JSON__``. Used by:
      * ``_render_dashboard`` (the legacy SSR-everything path) — until
        Phase 2 of the 2026-05 refactor splits the render into a fast
        shell + an XHR for this dict.
      * The ``action=initial_data`` POST endpoint — Phase 2's JSON
        endpoint that the JS shell will call on load.

    All DB queries for the initial render live here, in one place, so
    the two consumers stay in lockstep.
    """
    user_id = user["id"]
    first_name = user.get("first_name") or ("friend" if locale == "en" else "друже")  # noqa: i18n
    username = user.get("username") or ""
    today = _today_str()

    conn = get_conn()
    try:
        init_db(conn)
        profile = get_profile(conn, user_id) or {}
        today_blob = _load_day_blob(conn, user_id, today)
        history_30_rows = get_history(conn, user_id, days=PRELOAD_DAYS)
        water_target = int(get_water_target(conn, user_id) or 2000)
        # 2026-05 quick win B: pass profile in so get_adherence_stats
        # doesn't re-fetch it. Also caps the SELECT to 90 days (quick
        # win C) so the slowest query on this page stays bounded.
        adherence = get_adherence_stats(conn, user_id, profile=profile)
        weight_history_rows = get_weight_history(conn, user_id, limit=90)
        streak_row = get_streak(conn, user_id)
        latest_rec = get_latest_recommendation(conn, user_id)
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
    # F-5: pull goals projection + actual progress so the dashboard can render
    # weeks-to-goal, projected date, and on-track classification. REV #10: the
    # try/except still wraps the math (NaN, division-by-zero) — just no longer
    # also masking a closed-conn fetch error.
    try:
        from lib import goals as _goals
        _proj = _goals.projection_for_profile(profile)
        _hist = weight_history_rows[:20]  # what goals.py needs
        _actual = _goals.actual_weekly_delta(_hist, window_weeks=4)
        _status = (
            _goals.classify_actual_vs_target(_actual, _proj.weekly_delta_kg)
            if _actual is not None else None
        )
        goals_blob = {
            "weekly_delta_kg":     _proj.weekly_delta_kg,
            "weeks_to_goal":       _proj.weeks_to_goal,
            "projected_date":      _proj.projected_date.isoformat() if _proj.projected_date else None,
            "reason":              _proj.reason,
            "actual_weekly_delta": _actual,
            "status":              _status,
        }
    except Exception:
        goals_blob = {
            "weekly_delta_kg":     0.0,
            "weeks_to_goal":       None,
            "projected_date":      None,
            "reason":              "no_current",
            "actual_weekly_delta": None,
            "status":              None,
        }

    # Top-level weight_history for the chart on the Profile tab. Reuses the
    # same fetch as goals_blob — one DB hit, two consumers.
    weight_history = []
    for r in weight_history_rows:
        try:
            recorded = r.get("recorded_at")
            if hasattr(recorded, "isoformat"):
                recorded = recorded.isoformat()
            weight_history.append({
                "recorded_at": recorded,
                "weight_kg": float(r.get("weight_kg") or 0),
            })
        except Exception:
            continue
    streak_line = _build_streak_line(streak_row, locale)
    profile_blob = {
        "age":              profile.get("age"),
        "sex":              profile.get("sex") or "",
        "weight_kg":        float(weight_kg) if weight_kg else None,
        "height_cm":        profile.get("height_cm"),
        "gym_per_week":     profile.get("gym_per_week"),
        "goal":             goal or "",
        "target_weight_kg": float(target_weight_kg) if target_weight_kg else None,
        "weekly_delta_kg":  profile.get("weekly_delta_kg"),  # raw user setting (None if unset)
        "goals":            goals_blob,
    }
    fiber_target, sugar_target = _fiber_sugar_targets(profile, cal_target)
    targets_blob = {
        "calories":  cal_target,
        "protein":   int(macros.get("protein") or 0),
        "carbs":     int(macros.get("carbs") or 0),
        "fat":       int(macros.get("fat") or 0),
        "fiber_g":   fiber_target,
        "sugar_g":   sugar_target,
        "water_ml":  water_target,
    }

    return {
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
        "weight_history":    weight_history,
        "streak_line":       streak_line,
        "latest_recommendation": latest_rec,
        "goal_ua":           _goal_label(goal, locale=locale),
        "sex_ua":            _sex_label(profile.get("sex"), locale=locale),
        "bot_url":           f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else "",
    }


def _render_dashboard(user: dict, nonce: str = "", locale: str = "en") -> str:
    """Render the dashboard SHELL — layout + JS, no inlined data.

    2026-05 Phase 2: the shell renders fast (~50–150ms, no DB queries
    inside this function). Data arrives via a follow-up XHR to
    ``action=initial_data`` which calls ``_load_initial_data`` and
    returns JSON. The JS bootstrap (added below the static labels)
    stuffs that JSON into ``#__data__`` and calls
    ``window.__bootDashboard()`` — the rest of the existing inline
    script body — once data is available.

    User perceives the dashboard ~10× faster: the spinner sits on
    the shell HTML for ~500ms (cold) / ~150ms (warm) instead of
    waiting for the full data render before any HTML lands.
    """
    body = (
        _DASHBOARD_HTML
        # 2026-05 Phase 2: empty data placeholder. JS fills it via XHR
        # before calling window.__bootDashboard().
        .replace("__DATA_JSON__", "null")
        .replace("__NONCE__", _esc(nonce))
        .replace("__LANG__", locale)
        .replace("__JS_LABELS__", _build_js_labels(locale))
    )
    # Static HTML labels — interpolated server-side.
    static_labels = {
        "WATER":              _i18n_t("dash.water",              locale=locale),
        "WATER_UNITS":        _i18n_t("dash.water_units",        locale=locale),
        "CAL_REMAINING":      _i18n_t("dash.cal_remaining",      locale=locale),
        "CAL_REMAINING_UNIT": _i18n_t("dash.cal_remaining_unit", locale=locale),
        "MACRO_H2":           _i18n_t("dash.macro_h2",           locale=locale),
        "MACRO_PROTEIN":      _i18n_t("dash.macro_protein",      locale=locale),
        "MACRO_CARBS":        _i18n_t("dash.macro_carbs",        locale=locale),
        "MACRO_FAT":          _i18n_t("dash.macro_fat",          locale=locale),
        "MACRO_FIBER":        _i18n_t("dash.macro_fiber",        locale=locale),
        "MACRO_SUGAR":        _i18n_t("dash.macro_sugar",        locale=locale),
        "COACH_TITLE":        _i18n_t("dash.coach_title",        locale=locale),
        "CAL_BARS_TITLE":     _i18n_t("dash.cal_bars_title",     locale=locale),
        "WEIGHT_CHART_TITLE": _i18n_t("dash.weight_chart_title", locale=locale),
        "SUMMARY":            _i18n_t("dash.summary",            locale=locale),
        "SHARE_H2":           _i18n_t("dash.share_h2",           locale=locale),
        "SHARE_BLURB":        _i18n_t("dash.share_blurb",        locale=locale),
        "SHARE_BTN":          _i18n_t("dash.share_btn",          locale=locale),
        "MEALS_H2":           _i18n_t("dash.meals_h2",           locale=locale),
        "MEALS_LOADING":      _i18n_t("dash.meals_loading",      locale=locale),
        "MEALS_HINT":         _i18n_t("dash.meals_hint",         locale=locale),
        "PROFILE_H2":         _i18n_t("dash.profile_h2",         locale=locale),
        "DAILY_TARGETS":      _i18n_t("dash.daily_targets",      locale=locale),
        "ALLTIME_AVG":        _i18n_t("dash.alltime_avg",        locale=locale),
        "ADHERENCE":          _i18n_t("dash.adherence",          locale=locale),
        "STREAK_H2":          _i18n_t("dash.streak_h2",          locale=locale),
        "STREAK_PILL_ZERO":   _i18n_t("dash.streak_pill_zero",   locale=locale),
        "NAV_OVERVIEW":       _i18n_t("dash.nav_overview",       locale=locale),
        "NAV_MEALS":          _i18n_t("dash.nav_meals",          locale=locale),
        "NAV_PROFILE":        _i18n_t("dash.nav_profile",        locale=locale),
        # 2026-05 Phase 2 — full-page loading + error overlay.
        "LOADING_FAILED":     _i18n_t("dash.loading_failed",     locale=locale),
        "RETRY":              _i18n_t("dash.retry",              locale=locale),
    }
    for k, v in static_labels.items():
        body = body.replace(f"__LABEL_{k}__", v)
    return body


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="__LANG__">
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
  .coach-card summary {
    cursor: pointer; font-weight: 600; font-size: 0.95em;
    color: var(--text); list-style: none;
  }
  .coach-card summary::-webkit-details-marker { display: none; }
  .coach-card summary::after {
    content: '▾'; float: right; color: var(--hint); font-size: 0.85em;
  }
  .coach-card[open] summary::after { content: '▴'; }
  .coach-card .coach-body {
    margin: 8px 0 0; color: var(--text); font-size: 0.9em; line-height: 1.4;
    white-space: pre-wrap;
  }
  .warn-chip {
    display: inline-block; margin-left: 6px; padding: 1px 6px;
    border-radius: 4px; font-size: 0.75em; font-weight: 600;
    background: rgba(255, 187, 51, 0.15); color: var(--warn);
    cursor: pointer; user-select: none;
  }
  .warn-detail {
    margin-top: 4px; padding: 6px 8px; border-radius: 6px;
    background: var(--bg-secondary); font-size: 0.8em; color: var(--hint);
    line-height: 1.4;
  }
  .cal-bars { width: 100%; height: 80px; display: block; }
  .cal-bars rect { rx: 1.5; }
  .cal-bars rect.ok    { fill: var(--ok); }
  .cal-bars rect.warn  { fill: var(--warn); }
  .cal-bars rect.over  { fill: var(--over); }
  .cal-bars rect.empty { fill: var(--track); opacity: 0.5; }
  .weight-chart { width: 100%; height: 120px; display: block; }
  .weight-chart .raw { fill: none; stroke: var(--accent); stroke-width: 1; opacity: 0.4; }
  .weight-chart .avg { fill: none; stroke: var(--accent); stroke-width: 2; }
  .weight-chart .dot { fill: var(--accent); }
  .weight-empty { color: var(--hint); font-size: 0.9em; text-align: center; padding: 18px 8px; }

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

  /* Meals tab: hero summary card */
  .meals-summary-card { text-align: center; padding: 16px 14px; }
  /* F-12.5 share-card on overview tab */
  .share-card { padding: 16px 14px 14px; }
  .share-card h2 { margin: 0 0 8px; }
  .share-blurb { margin: 0 0 12px; font-size: 0.95em; line-height: 1.4;
                 color: var(--tg-theme-hint-color, #9a9aa6); }
  .share-card button { width: 100%; padding: 13px 18px; font-size: 1.05em;
                       font-weight: 600; margin-top: 0; }
  .share-card button[disabled] { opacity: 0.55; cursor: default; }
  .share-status { margin-top: 10px; font-size: 0.9em; min-height: 1.1em;
                  text-align: center;
                  color: var(--tg-theme-hint-color, #9a9aa6); }
  .share-status.ok  { color: #4caf50; }
  .share-status.err { color: var(--tg-theme-destructive-text-color, #ef5b5b); }
  .meals-summary-head {
    font-size: 0.85em; color: var(--hint);
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .meals-summary-big {
    margin-top: 6px;
    display: flex; align-items: baseline; justify-content: center; gap: 8px;
  }
  .meals-summary-kcal {
    font-size: 2.4em; font-weight: 700; line-height: 1; color: var(--text);
  }
  .meals-summary-kcal-of { color: var(--hint); font-size: 0.95em; }
  .meals-summary-pct {
    margin-top: 6px; color: var(--hint); font-size: 0.9em;
  }
  .meals-summary-bar {
    height: 8px; border-radius: 4px; margin-top: 12px;
    background: var(--track); overflow: hidden;
  }
  .meals-summary-fill {
    height: 100%; background: var(--accent); border-radius: 4px;
    transition: width 0.3s ease;
  }
  .meals-summary-macros { color: var(--hint); font-size: 0.9em; margin-top: 10px; }

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
  .meal-macros {
    color: var(--hint); font-size: 0.82em;
    font-variant-numeric: tabular-nums;
    margin-top: 3px;
  }

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

  /* 2026-05 Phase 2 — full-page loading + error overlay spinner. */
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<!-- 2026-05 Phase 2: full-page loading overlay shown until the
     initial_data XHR resolves and window.__bootDashboard() runs.
     Removed from the DOM when data arrives. Shows shellError on
     failure so the user knows to retry. -->
<div id="shellLoading" style="position:fixed;inset:0;display:flex;
     align-items:center;justify-content:center;
     background:var(--bg, #101014);z-index:9999;">
  <div style="width:32px;height:32px;
       border:3px solid rgba(127,127,127,0.25);
       border-top-color: var(--tg-theme-button-color, #3ea6ff);
       border-radius:50%;animation:spin 0.8s linear infinite;"></div>
</div>
<div id="shellError" style="position:fixed;inset:0;display:none;
     align-items:center;justify-content:center;flex-direction:column;
     background:var(--bg, #101014);z-index:9998;
     color:var(--tg-theme-text-color, #e6e6ea);
     font-family:-apple-system, system-ui, sans-serif;padding:20px;">
  <p style="text-align:center;margin:0 0 16px 0;">⚠️ __LABEL_LOADING_FAILED__</p>
  <button onclick="location.reload()"
          style="background:var(--tg-theme-button-color, #3ea6ff);
                 color:var(--tg-theme-button-text-color, #fff);
                 border:0;padding:10px 18px;border-radius:10px;
                 font-size:1em;font-family:inherit;cursor:pointer;">
    __LABEL_RETRY__
  </button>
</div>

<div class="spinner-wrap">
  <div class="spinner-row" id="spinnerRow"></div>
</div>

<main>
  <section id="tab-overview">
    <div class="hero">
      <div class="hero-side">
        <div class="label">__LABEL_WATER__</div>
        <div class="value" id="waterValue">__LABEL_WATER_UNITS__</div>
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
          <div class="sub" id="calSub">—</div>
          <div class="unit" id="calPct">0 %</div>
        </div>
      </div>
      <div class="hero-side">
        <div class="label">__LABEL_CAL_REMAINING__</div>
        <div class="value" id="calRemaining">0</div>
        <div class="unit">__LABEL_CAL_REMAINING_UNIT__</div>
      </div>
    </div>

    <details class="card coach-card" id="coachCard" hidden>
      <summary id="coachTitle">__LABEL_COACH_TITLE__</summary>
      <p class="coach-body" id="coachBody">—</p>
    </details>

    <div class="card">
      <h2>__LABEL_MACRO_H2__</h2>
      <div class="macro">
        <span class="macro-name">__LABEL_MACRO_PROTEIN__</span>
        <div class="macro-bar"><div id="pFill" class="macro-fill"></div></div>
        <b class="macro-val" id="pVal">—</b>
      </div>
      <div class="macro">
        <span class="macro-name">__LABEL_MACRO_CARBS__</span>
        <div class="macro-bar"><div id="cFill" class="macro-fill"></div></div>
        <b class="macro-val" id="cVal">—</b>
      </div>
      <div class="macro">
        <span class="macro-name">__LABEL_MACRO_FAT__</span>
        <div class="macro-bar"><div id="fFill" class="macro-fill"></div></div>
        <b class="macro-val" id="fVal">—</b>
      </div>
      <div class="macro">
        <span class="macro-name">__LABEL_MACRO_FIBER__</span>
        <div class="macro-bar"><div id="fbFill" class="macro-fill"></div></div>
        <b class="macro-val" id="fbVal">—</b>
      </div>
      <div class="macro">
        <span class="macro-name">__LABEL_MACRO_SUGAR__</span>
        <div class="macro-bar"><div id="sgFill" class="macro-fill"></div></div>
        <b class="macro-val" id="sgVal">—</b>
      </div>
    </div>

    <div class="card cal-bars-card">
      <h2>__LABEL_CAL_BARS_TITLE__</h2>
      <svg class="cal-bars" id="calBarsSvg" viewBox="0 0 300 80" preserveAspectRatio="none"></svg>
    </div>

    <div class="card summary-card">
      <h2>__LABEL_SUMMARY__</h2>
      <p class="summary-text" id="daySummary">—</p>
    </div>

    <div class="card share-card">
      <h2>__LABEL_SHARE_H2__</h2>
      <p class="share-blurb">__LABEL_SHARE_BLURB__</p>
      <button type="button" id="shareBtn">__LABEL_SHARE_BTN__</button>
      <div class="share-status" id="shareStatus"></div>
    </div>
  </section>

  <section id="tab-meals" hidden>
    <div class="card meals-summary-card">
      <div class="meals-summary-head" id="mealsDateHeader">__LABEL_MEALS_H2__</div>
      <div class="meals-summary-big">
        <span class="meals-summary-kcal" id="sumKcal">0</span>
        <span class="meals-summary-kcal-of" id="sumKcalOfLabel">—</span>
      </div>
      <div class="meals-summary-pct" id="sumPct">0%</div>
      <div class="meals-summary-bar"><div class="meals-summary-fill" id="sumFill"></div></div>
      <div class="meals-summary-macros" id="sumMacros">—</div>
    </div>
    <div class="card">
      <div id="mealsList"><p class="meal-empty">__LABEL_MEALS_LOADING__</p></div>
      <p class="hint-line">__LABEL_MEALS_HINT__</p>
    </div>
  </section>

  <section id="tab-profile" hidden>
    <div class="card">
      <h2>__LABEL_PROFILE_H2__</h2>
      <div class="id-grid" id="profileGrid"></div>
    </div>

    <div class="card">
      <h2>__LABEL_DAILY_TARGETS__</h2>
      <div class="id-grid" id="targetsGrid"></div>
    </div>

    <div class="card">
      <h2>__LABEL_ALLTIME_AVG__</h2>
      <div id="averagesBody"></div>
    </div>

    <div class="card">
      <h2>__LABEL_ADHERENCE__</h2>
      <div id="adherenceBody"></div>
    </div>

    <div class="card" id="streakCard" hidden>
      <h2>__LABEL_STREAK_H2__</h2>
      <span class="streak-pill" id="streakValue">__LABEL_STREAK_PILL_ZERO__</span>
    </div>

    <div class="card" id="weightChartCard">
      <h2>__LABEL_WEIGHT_CHART_TITLE__</h2>
      <svg class="weight-chart" id="weightChartSvg" viewBox="0 0 300 120" preserveAspectRatio="none"></svg>
      <p class="weight-empty" id="weightChartEmpty" hidden>—</p>
    </div>
  </section>
</main>

<nav class="bottom-nav" id="bottomNav">
  <button type="button" class="active" data-tab="overview">
    <span class="nav-icon">🏠</span><span>__LABEL_NAV_OVERVIEW__</span>
  </button>
  <button type="button" data-tab="meals">
    <span class="nav-icon">🍽️</span><span>__LABEL_NAV_MEALS__</span>
  </button>
  <button type="button" data-tab="profile">
    <span class="nav-icon">👤</span><span>__LABEL_NAV_PROFILE__</span>
  </button>
</nav>

<script type="application/json" id="__data__">__DATA_JSON__</script>

<!-- 2026-05 Phase 2 bootstrap: applies theme + TG.ready synchronously
     (so page chrome is correct immediately), then fetches initial_data
     via XHR and runs the existing dashboard init once data arrives.
     Previously the entire data blob was inlined and the user waited
     5–7s for the spinner to clear; now they see the dashboard layout
     in ~500ms (cold) and watch the cards fill in. -->
<script nonce="__NONCE__">
(function() {
  var TG = (window.Telegram && window.Telegram.WebApp) || null;
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

  function showShellError() {
    var el = document.getElementById('shellLoading');
    if (el) el.style.display = 'none';
    var err = document.getElementById('shellError');
    if (err) err.style.display = 'flex';
  }

  // Three sources for the Telegram auth blob, preferred in order:
  //  (1) `sessionStorage.__kuswise_initData__` — written by the GET
  //      bootstrap before the form-submit navigation. This is the
  //      most reliable source for chat-list / direct-link entries
  //      because form-submit nav LOSES the URL hash that initially
  //      carried tgWebAppData. sessionStorage survives same-tab
  //      same-origin nav and is byte-identical to the value the
  //      GET bootstrap read (no Python encoding round-trip risk).
  //  (2) `tg.initData` — SDK property. Works for in-chat entry
  //      paths (chat menu button, inline web_app button) where
  //      Telegram keeps the chat context active.
  //  (3) `location.hash#tgWebAppData=...` — URL hash. Initial entry
  //      point for chat-list opens but typically lost after the
  //      form-submit nav; kept here as defense-in-depth (e.g.
  //      direct page reload outside the form-submit flow).
  function findInitData() {
    try {
      var s = sessionStorage.getItem('__kuswise_initData__');
      if (s) return s;
    } catch(e) {}
    if (TG && TG.initData) return TG.initData;
    if (window.location.hash &&
        window.location.hash.indexOf('tgWebAppData') !== -1) {
      var hash = window.location.hash.charAt(0) === '#'
        ? window.location.hash.substring(1) : window.location.hash;
      try {
        var params = new URLSearchParams(hash);
        var raw = params.get('tgWebAppData');
        if (raw) return raw;
      } catch(e) {}
    }
    return '';
  }
  var initData = findInitData();
  if (!initData) { showShellError(); return; }

  // 2026-05 Phase 2 hotfix #4: URL-encoded body, NOT multipart.
  // A FormData body makes fetch send `multipart/form-data; boundary=...`
  // but the POST handler uses `urllib.parse.parse_qs(raw)` which only
  // understands `application/x-www-form-urlencoded`. On a multipart
  // body, parse_qs returns an empty `initData` field → server 401 →
  // "Couldn't load the dashboard." Same fix pattern as scan.py:297
  // and the `fetchDay` / `request_recap` XHRs below.
  var body = 'action=initial_data' +
             '&initData=' + encodeURIComponent(initData) +
             '&lang=' + encodeURIComponent(document.documentElement.lang || 'en');
  fetch(window.location.pathname, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body
  })
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      // Drop the carried initData now that the XHR landed —
      // prevents a stale value from being used on a future load
      // in the same tab (per plan R3).
      try { sessionStorage.removeItem('__kuswise_initData__'); } catch(e) {}
      document.getElementById('__data__').textContent = JSON.stringify(data);
      var el = document.getElementById('shellLoading');
      if (el) el.style.display = 'none';
      if (typeof window.__bootDashboard === 'function') {
        try { window.__bootDashboard(); }
        catch(e) { console.error('bootDashboard failed:', e); showShellError(); }
      }
    })
    .catch(function(err) {
      console.error('initial_data fetch failed:', err);
      showShellError();
    });
})();
</script>

<script nonce="__NONCE__">
window.__bootDashboard = function() {
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
  var L = __JS_LABELS__;
  var LANG = '__LANG__';
  function fmt(s, vars) {
    if (!s) return '';
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, function(_, k) {
      return vars[k] !== undefined ? vars[k] : ('{' + k + '}');
    });
  }
  var DOW_UA = [L.dow_mon, L.dow_tue, L.dow_wed, L.dow_thu, L.dow_fri, L.dow_sat, L.dow_sun];
  var MONTH_UA = [L.month_jan, L.month_feb, L.month_mar, L.month_apr, L.month_may, L.month_jun,
                  L.month_jul, L.month_aug, L.month_sep, L.month_oct, L.month_nov, L.month_dec];

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
      var label = isToday ? L.today_label : DOW_UA[i];
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

  // --- Share-week button (overview tab) ---
  // Coach note (latest end-of-day summary). Hidden when no row exists.
  (function(){
    var card = document.getElementById('coachCard');
    var rec  = DATA.latest_recommendation;
    if (!card) return;
    if (!rec || !rec.recommendation) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    var titleEl = document.getElementById('coachTitle');
    var bodyEl  = document.getElementById('coachBody');
    if (titleEl) {
      titleEl.textContent = fmt(L.coach_title_with_date, {
        date: formatLongDate(rec.date)
      });
    }
    if (bodyEl) bodyEl.textContent = rec.recommendation;
  })();

  // Posts action=request_recap → server generates the PNG via the same
  // build_user_recap path /recap uses, sends it to the user's chat, and
  // we just close the Mini App so they see it land.
  var shareBtn    = document.getElementById('shareBtn');
  var shareStatus = document.getElementById('shareStatus');
  if (shareBtn) {
    shareBtn.addEventListener('click', function() {
      shareBtn.disabled = true;
      shareStatus.className = 'share-status';
      shareStatus.textContent = L.share_status_preparing;
      var initData = (TG && TG.initData) || '';
      var body = 'action=request_recap&initData=' + encodeURIComponent(initData);
      fetch(window.location.pathname, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body,
        credentials: 'same-origin'
      }).then(function(r){
        return r.json().catch(function(){ return {ok: false}; });
      }).then(function(j){
        if (j && j.ok) {
          shareStatus.className = 'share-status ok';
          shareStatus.textContent = L.share_status_sent;
          setTimeout(function(){
            try { TG && TG.close(); } catch(e) {}
          }, 900);
        } else {
          shareStatus.className = 'share-status err';
          shareStatus.textContent = L.share_status_failed;
          shareBtn.disabled = false;
        }
      }).catch(function(){
        shareStatus.className = 'share-status err';
        shareStatus.textContent = L.share_status_network;
        shareBtn.disabled = false;
      });
    });
  }

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
    var localeStr = LANG === 'uk' ? 'uk-UA' : 'en-US';
    document.getElementById('calEaten').textContent = Math.round(cal).toLocaleString(localeStr);
    document.getElementById('calPct').textContent = Math.round(pct * 100) + ' %';
    // Render the calorie subtitle (e.g. "of 2400 kcal") with the goal value
    // baked in. Setting innerHTML here also (re)creates the inner #calGoal
    // span, so we must NOT touch #calGoal before this line — Phase F bug.
    var calSubEl = document.getElementById('calSub');
    if (calSubEl) {
      calSubEl.innerHTML = fmt(L.cal_subtitle, {goal: '<span id="calGoal">' + calTarget.toLocaleString(localeStr) + '</span>'});
    }

    // Remaining calories
    var remaining = Math.max(0, calTarget - cal);
    document.getElementById('calRemaining').textContent = Math.round(remaining).toLocaleString(localeStr);

    // Water
    var decSep = LANG === 'uk' ? ',' : '.';
    var waterL = (waterMl / 1000).toFixed(2).replace('.', decSep);
    var waterTargetL = (waterTarget / 1000).toFixed(1).replace('.', decSep);
    document.getElementById('waterValue').textContent = waterL + ' / ' + waterTargetL + ' ' + L.unit_l;
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
        Math.round(value) + ' / ' + Math.round(tgt) + ' ' + L.unit_g;
    }
    setMacro('pFill', 'pVal', p, t.protein);
    setMacro('cFill', 'cVal', c, t.carbs);
    setMacro('fFill', 'fVal', f, t.fat);

    // Fiber: server-computed 14 g per 1000 kcal (USDA), clamped 20-45 g.
    // Bar fills toward 100% — positive macro, classify like protein.
    var fb = log.fiber || 0;
    var fbTarget = t.fiber_g || 28;
    setMacro('fbFill', 'fbVal', fb, fbTarget);
    // Sugar: server-computed AHA cap (25 g female / 36 g male).
    // Bar fills toward the cap; turns red once exceeded.
    var sg = log.sugar || 0;
    var sgTarget = t.sugar_g || 36;
    var sgFill = document.getElementById('sgFill');
    var sgVal  = document.getElementById('sgVal');
    if (sgFill && sgVal) {
      var sgRatio = Math.max(0, Math.min(1.2, sg / sgTarget));
      sgFill.style.width = Math.min(100, sgRatio * 100).toFixed(1) + '%';
      var sgPct = (sg / sgTarget) * 100;
      sgFill.className = 'macro-fill ' + (sgPct > 100 ? 'over' : (sgPct > 75 ? 'warn' : 'ok'));
      sgVal.textContent = Math.round(sg) + ' / ' + Math.round(sgTarget) + ' ' + L.unit_g;
    }

    renderSummary(blob);
    renderCalBars();
  }

  function renderCalBars() {
    var svg = document.getElementById('calBarsSvg');
    if (!svg) return;
    var aggregates = DATA.history || {};
    var todayISO = DATA.today;
    var target = (DATA.targets && DATA.targets.calories) || 2000;
    // Enumerate the last 30 dates (sparse-dict iteration pitfall — REV #4).
    var days = [];
    var d = fromISO(todayISO);
    for (var i = 0; i < 30; i++) {
      var iso = toISO(d);
      var row = aggregates[iso] || {has_meals: false, calories: 0};
      days.push({iso: iso, has_meals: !!row.has_meals, cal: row.calories || 0});
      d = addDays(d, -1);
    }
    days.reverse();
    var w = 300, h = 80, gap = 1.5;
    var barW = (w - gap * (days.length - 1)) / days.length;
    var rects = '';
    for (var j = 0; j < days.length; j++) {
      var day = days[j];
      var cls;
      if (!day.has_meals) cls = 'empty';
      else {
        var pct = target > 0 ? (day.cal / target) * 100 : 0;
        if (pct >= 130 || pct < 70) cls = 'over';
        else if (pct >= 90 && pct <= 110) cls = 'ok';
        else cls = 'warn';
      }
      var bh = day.has_meals
        ? Math.max(2, Math.min(h, (day.cal / Math.max(target * 1.3, 1)) * h))
        : 2;
      var x = j * (barW + gap);
      var y = h - bh;
      rects += '<rect class="' + cls + '" x="' + x.toFixed(2) +
               '" y="' + y.toFixed(2) +
               '" width="' + barW.toFixed(2) +
               '" height="' + bh.toFixed(2) + '"></rect>';
    }
    svg.innerHTML = rects;
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

    var localeStr2 = LANG === 'uk' ? 'uk-UA' : 'en-US';
    var decSep2 = LANG === 'uk' ? ',' : '.';
    if (mc === 0) {
      parts.push(isToday ? L.summary_empty_today : L.summary_empty_other);
    } else {
      if (calPct > 115) {
        parts.push(fmt(L.summary_cal_over, {pct: calPct, diff: Math.abs(diff).toLocaleString(localeStr2)}));
      } else if (calPct >= 85) {
        parts.push(fmt(L.summary_cal_ok, {pct: calPct}));
      } else if (calPct >= 50) {
        parts.push(fmt(L.summary_cal_under, {pct: calPct, tail: (isToday ? L.summary_under_today_tail : '.')}));
      } else {
        parts.push(fmt(L.summary_cal_low, {pct: calPct}));
      }

      // Find the most-off macro (largest |pct-100|) and comment on it.
      var macros = [
        {name: L.macro_proteins, val: p, target: t.protein},
        {name: L.macro_carbs_g,  val: c, target: t.carbs},
        {name: L.macro_fats,     val: f, target: t.fat},
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
        var key = worst.pct < 100 ? L.summary_macro_low : L.summary_macro_high;
        parts.push(fmt(key, {macro: worst.m.name, pct: pctR}));
      }

      parts.push('<span class="muted">' + fmt(L.summary_meals_water, {meals: mc, water: (waterMl / 1000).toFixed(2).replace('.', decSep2)}) + '</span>');
    }

    document.getElementById('daySummary').innerHTML = parts.join(' ');
  }

  // --- Meals rendering ---
  var MEAL_TYPE_ORDER = ['breakfast', 'lunch', 'dinner', 'snack'];
  var MEAL_TYPE_LABELS = {
    breakfast: L.meal_breakfast,
    lunch:     L.meal_lunch,
    dinner:    L.meal_dinner,
    snack:     L.meal_snack
  };

  function renderMeals(blob) {
    var header = document.getElementById('mealsDateHeader');
    header.textContent = formatLongDate(blob.date);
    var t = DATA.targets;
    var calTarget = t.calories || 1;
    var log = blob.log || {};
    var list = document.getElementById('mealsList');
    var meals = blob.meals || [];
    var localeStr = LANG === 'uk' ? 'uk-UA' : 'en-US';

    var cal = log.calories || 0;
    var pct = Math.round((cal / calTarget) * 100);
    var fillPct = Math.min(100, Math.max(0, pct));
    document.getElementById('sumKcal').textContent =
      Math.round(cal).toLocaleString(localeStr);
    var ofLabel = document.getElementById('sumKcalOfLabel');
    if (ofLabel) ofLabel.textContent = fmt(L.meals_summary_kcal_of, {target: calTarget.toLocaleString(localeStr)});
    document.getElementById('sumPct').textContent = fmt(L.meals_pct_of_goal, {pct: pct});
    document.getElementById('sumFill').style.width = fillPct + '%';
    document.getElementById('sumMacros').innerHTML =
      '🥩 ' + Math.round(log.protein || 0) + ' ' + L.unit_g + ' · ' +
      '🍞 ' + Math.round(log.carbs || 0) + ' ' + L.unit_g + ' · ' +
      '🥑 ' + Math.round(log.fat || 0) + ' ' + L.unit_g + ' · ' +
      meals.length + ' ' + (LANG === 'uk' ? 'страв' : 'meals');  // noqa: i18n

    if (meals.length === 0) {
      list.innerHTML = '<p class="meal-empty">' + L.meal_empty_other + '</p>';
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
    function mealRowHTML(m) {
      var pct = calTarget > 0 ? Math.round((m.calories / calTarget) * 100) : 0;
      var warns = (m.allergen_warnings || []).concat(m.crohn_warnings || []);
      var chip = '';
      var detail = '';
      if (warns.length) {
        chip = '<span class="warn-chip" data-mealid="' + esc(m.id) + '">' +
               fmt(L.warn_chip, {n: warns.length}) + '</span>';
        detail = '<div class="warn-detail" id="warn-' + esc(m.id) + '" hidden>' +
                 '<b>' + esc(L.warn_detail_label) + ':</b> ' +
                 warns.map(esc).join(', ') + '</div>';
      }
      var macrosLine =
        L.meal_macro_p + ' ' + Math.round(m.protein_g || 0) + L.unit_g + ' · ' +
        L.meal_macro_c + ' ' + Math.round(m.carbs_g   || 0) + L.unit_g + ' · ' +
        L.meal_macro_f + ' ' + Math.round(m.fat_g     || 0) + L.unit_g + ' · ' +
        L.meal_macro_fi + ' ' + Math.round(m.fiber_g || 0) + L.unit_g + ' · ' +
        L.meal_macro_su + ' ' + Math.round(m.sugar_g || 0) + L.unit_g;
      return '<div class="meal-row">' +
             '<div class="meal-desc">' + esc(m.description) + chip +
               '<div class="meal-macros">' + macrosLine + '</div>' +
             '</div>' +
             '<div class="meal-kcal">' + Math.round(m.calories) + ' ' + L.unit_kcal + '</div>' +
             '<div class="meal-pct">' + pct + ' %</div>' +
             '</div>' + detail;
    }
    MEAL_TYPE_ORDER.forEach(function(t){
      var arr = grouped[t];
      if (arr.length === 0) return;
      html += '<div class="meal-group"><h3>' + MEAL_TYPE_LABELS[t] + '</h3>';
      arr.forEach(function(m){ html += mealRowHTML(m); });
      html += '</div>';
    });
    if (unknown.length) {
      html += '<div class="meal-group"><h3>' + L.meal_other + '</h3>';
      unknown.forEach(function(m){ html += mealRowHTML(m); });
      html += '</div>';
    }
    list.innerHTML = html;
    // Toggle warn-detail when its chip is tapped.
    list.querySelectorAll('.warn-chip').forEach(function(chip){
      chip.addEventListener('click', function(e){
        e.stopPropagation();
        var id = chip.getAttribute('data-mealid');
        var det = document.getElementById('warn-' + id);
        if (det) det.hidden = !det.hidden;
      });
    });
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
  // Server-side label maps (DATA.goal_ua / DATA.sex_ua) carry the locale-
  // resolved label text; JS just uses the strings already produced by Python.

  function idRow(k, v) {
    return '<div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + '</div>';
  }

  function renderProfile() {
    var p = DATA.profile || {}, t = DATA.targets || {}, a = DATA.adherence || {};
    var grid = document.getElementById('profileGrid');
    var localeStr = LANG === 'uk' ? 'uk-UA' : 'en-US';
    var decSep = LANG === 'uk' ? ',' : '.';
    var rows =
      idRow(L.profile_field_name, DATA.user.first_name || '—') +
      idRow(L.profile_field_age, p.age != null ? p.age + ' ' + L.profile_field_age_unit : '—') +
      idRow(L.profile_field_sex, DATA.sex_ua || '—') +
      idRow(L.profile_field_weight, p.weight_kg != null ? p.weight_kg + ' ' + L.unit_kg : '—') +
      idRow(L.profile_field_height, p.height_cm != null ? p.height_cm + ' ' + L.unit_cm : '—') +
      idRow(L.profile_field_gym, p.gym_per_week != null ? p.gym_per_week + '×' : '—') +
      idRow(L.profile_field_goal, DATA.goal_ua || '—');

    if (p.target_weight_kg != null && (p.goal === 'lose' || p.goal === 'gain')) {
      var togoTxt;
      if (p.weight_kg != null) {
        var delta = Number(p.weight_kg) - Number(p.target_weight_kg);
        var rem = p.goal === 'lose' ? Math.max(0, delta) : Math.max(0, -delta);
        if (rem <= 0.05) {
          togoTxt = fmt(L.profile_target_done, {target: p.target_weight_kg});
        } else {
          var sign = p.goal === 'lose' ? '−' : '+';
          togoTxt = fmt(L.profile_target_togo, {target: p.target_weight_kg, sign: sign, rem: rem.toFixed(1)});
        }
      } else {
        togoTxt = fmt(L.profile_target_plain, {target: p.target_weight_kg});
      }
      rows += idRow(L.profile_target_weight, togoTxt);
    }

    // F-5: Goals projection block (weekly delta + projected date + status).
    var g = p.goals || {};
    if (g.weekly_delta_kg && (p.goal === 'lose' || p.goal === 'gain')) {
      var deltaTxt = fmt(L.profile_weekly_delta_v, {sign: (g.weekly_delta_kg > 0 ? '+' : ''), val: Number(g.weekly_delta_kg).toFixed(2)});
      rows += idRow(L.profile_weekly_delta, deltaTxt);
    }
    if (g.reason === 'ok' && g.projected_date) {
      // Format YYYY-MM-DD → DD.MM.YYYY for display.
      var pd = String(g.projected_date);
      var pdTxt = pd.length === 10
                ? pd.substring(8, 10) + '.' + pd.substring(5, 7) + '.' + pd.substring(0, 4)
                : pd;
      var weeks = g.weeks_to_goal != null ? fmt(L.profile_projection_weeks, {n: Number(g.weeks_to_goal)}) : '';
      rows += idRow(L.profile_projection, pdTxt + weeks);
    }
    if (g.status) {
      var statusTxt = g.status === 'ahead'    ? L.profile_status_ahead
                    : g.status === 'on_track' ? L.profile_status_on_track
                    : g.status === 'behind'   ? L.profile_status_behind
                    : '';
      if (statusTxt) {
        var actualTxt = g.actual_weekly_delta != null
          ? fmt(L.profile_pace_actual, {val: (g.actual_weekly_delta > 0 ? '+' : '') + Number(g.actual_weekly_delta).toFixed(2)})
          : '';
        rows += idRow(L.profile_pace, statusTxt + actualTxt);
      }
    }
    grid.innerHTML = rows;

    var tgEl = document.getElementById('targetsGrid');
    tgEl.innerHTML =
      idRow(L.targets_calories, (t.calories || 0).toLocaleString(localeStr) + ' ' + L.unit_kcal) +
      idRow(L.targets_protein, (t.protein || 0) + ' ' + L.unit_g) +
      idRow(L.targets_carbs, (t.carbs || 0) + ' ' + L.unit_g) +
      idRow(L.targets_fat, (t.fat || 0) + ' ' + L.unit_g) +
      idRow(L.targets_water, ((t.water_ml || 0) / 1000).toFixed(1).replace('.', decSep) + ' ' + L.unit_l);

    var avg = document.getElementById('averagesBody');
    if (!a.logged_days) {
      avg.innerHTML = '<p class="meal-empty">' + L.avg_empty + '</p>';
    } else {
      avg.innerHTML =
        '<div class="macro"><span class="macro-name">🔥 ' + L.unit_kcal + '</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_calories / (t.calories || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_calories || 0) + ' / ' + (t.calories || 0) + '</b></div>' +

        '<div class="macro"><span class="macro-name">' + L.macro_protein + '</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_protein_g / (t.protein || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_protein_g || 0) + ' / ' + (t.protein || 0) + ' ' + L.unit_g + '</b></div>' +

        '<div class="macro"><span class="macro-name">' + L.macro_carbs + '</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_carbs_g / (t.carbs || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_carbs_g || 0) + ' / ' + (t.carbs || 0) + ' ' + L.unit_g + '</b></div>' +

        '<div class="macro"><span class="macro-name">' + L.macro_fat + '</span>' +
        '<div class="macro-bar"><div class="macro-fill ok" style="width:' +
          Math.min(100, (a.avg_fat_g / (t.fat || 1)) * 100).toFixed(1) + '%"></div></div>' +
        '<b class="macro-val">' + Math.round(a.avg_fat_g || 0) + ' / ' + (t.fat || 0) + ' ' + L.unit_g + '</b></div>' +

        '<p class="muted-note">' + fmt(L.avg_subtitle, {n: a.logged_days}) + '</p>';
    }

    var adh = document.getElementById('adherenceBody');
    if (!a.logged_days || a.logged_days < 3) {
      adh.innerHTML = '<p class="meal-empty">' + L.adherence_empty + '</p>';
    } else {
      adh.innerHTML =
        adherenceRow('🔥 ' + L.targets_calories, a.calories_avg_pct) +
        adherenceRow(L.macro_protein, a.protein_avg_pct) +
        adherenceRow(L.macro_carbs,   a.carbs_avg_pct) +
        adherenceRow(L.macro_fat,     a.fat_avg_pct);
    }

    var streakCard = document.getElementById('streakCard');
    var line = DATA.streak_line;
    if (line) {
      streakCard.hidden = false;
      document.getElementById('streakValue').textContent = line;
    } else {
      streakCard.hidden = true;
    }

    renderWeightChart();
  }

  function renderWeightChart() {
    var svg = document.getElementById('weightChartSvg');
    var empty = document.getElementById('weightChartEmpty');
    if (!svg || !empty) return;
    var hist = (DATA.weight_history || []).slice();
    if (hist.length < 3) {
      svg.innerHTML = '';
      svg.style.display = 'none';
      empty.textContent = L.weight_chart_empty;
      empty.hidden = false;
      return;
    }
    svg.style.display = '';
    empty.hidden = true;

    // Sort oldest-first by recorded_at (ISO string compare works).
    hist.sort(function(a, b){
      return String(a.recorded_at) < String(b.recorded_at) ? -1 : 1;
    });

    var w = 300, h = 120, pad = 6;
    var weights = hist.map(function(r){ return r.weight_kg; });
    var minW = Math.min.apply(null, weights);
    var maxW = Math.max.apply(null, weights);
    if (maxW - minW < 0.5) { minW -= 0.5; maxW += 0.5; }
    var n = hist.length;
    function xAt(i)    { return pad + (i / Math.max(n - 1, 1)) * (w - 2 * pad); }
    function yAt(kg)   { return pad + (1 - (kg - minW) / (maxW - minW)) * (h - 2 * pad); }

    // Raw line
    var rawPts = hist.map(function(r, i){ return xAt(i).toFixed(1) + ',' + yAt(r.weight_kg).toFixed(1); }).join(' ');
    // 7-point trailing rolling average
    var avgPts = [];
    for (var i = 0; i < n; i++) {
      var lo = Math.max(0, i - 6);
      var sum = 0, count = 0;
      for (var j = lo; j <= i; j++) { sum += weights[j]; count++; }
      avgPts.push(xAt(i).toFixed(1) + ',' + yAt(sum / count).toFixed(1));
    }
    var dots = hist.map(function(r, i){
      return '<circle class="dot" cx="' + xAt(i).toFixed(1) +
             '" cy="' + yAt(r.weight_kg).toFixed(1) +
             '" r="1.5"></circle>';
    }).join('');
    svg.innerHTML =
      '<polyline class="raw" points="' + rawPts + '"></polyline>' +
      '<polyline class="avg" points="' + avgPts.join(' ') + '"></polyline>' +
      dots;
  }

  function adherenceRow(label, pct) {
    // pct is the user's average daily total as % of goal.
    // Color band: closer to 100 = greener. The goal IS 100%, so deviation
    // in either direction is worse — symmetric around 100, not a 0→100 ramp.
    //   green (ok):   90-110 (within ±10% of goal)
    //   amber (warn): 75-90 or 110-125
    //   red (over):   under 75 or over 125
    // The bar fill is clamped to 100 so over-target values don't overflow
    // the bar visually; the numeric label still shows the real percentage.
    var val = pct == null ? 0 : pct;
    var dist = Math.abs(val - 100);
    var cls = dist <= 10 ? 'ok' : (dist <= 25 ? 'warn' : 'over');
    var barWidth = Math.min(Math.max(val, 0), 100);
    return '<div class="macro"><span class="macro-name">' + esc(label) + '</span>' +
           '<div class="macro-bar"><div class="macro-fill ' + cls +
             '" style="width:' + barWidth + '%"></div></div>' +
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
}; // end window.__bootDashboard — invoked by the Phase 2 bootstrap once XHR data arrives.
</script>
</body></html>"""
