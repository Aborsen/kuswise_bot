"""Admin stats dashboard — view in browser with HTTP Basic Auth."""
import base64
import hmac
import html
import json
import os
import secrets
import sys
import traceback
from http.server import BaseHTTPRequestHandler

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import ADMIN_PASSWORD, ADMIN_USERNAME
from lib.database import get_conn, init_db, delete_meal, delete_meal_admin, recalc_daily_log, delete_user_all_data


# Static headers applied to every response (CSP is built per-request from
# _csp_with_nonce so we can use a nonce instead of 'unsafe-inline' for scripts).
_STATIC_SECURITY_HEADERS = [
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"),
]


def _csp_with_nonce(nonce: str) -> str:
    """Per-request CSP. Inline scripts must carry the matching nonce attribute;
    inline styles still rely on 'unsafe-inline' (Tailwind-style inline styles
    in the admin template are pervasive and lower-risk than inline scripts)."""
    return (
        "default-src 'none'; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


def _new_nonce() -> str:
    """Cryptographically-random per-response nonce, base64url, ~22 chars."""
    return secrets.token_urlsafe(16)


def _authorized(headers) -> bool:
    """Authenticate via HTTP Basic Auth (browser) or Bearer token (curl), where
    the Bearer token is `ADMIN_PASSWORD` (NOT `CRON_SECRET`).

    Previously this also accepted `Bearer <CRON_SECRET>`, which meant a single
    leaked secret unlocked both crons and the admin panel. The two are now
    fully separated — `CRON_SECRET` is for cron endpoints only.

    Fails closed when ADMIN_USERNAME / ADMIN_PASSWORD are unset.
    Uses constant-time comparison to resist timing attacks.
    """
    if not (ADMIN_USERNAME and ADMIN_PASSWORD):
        return False

    auth = headers.get("Authorization", "")
    if not auth:
        return False

    # Bearer = ADMIN_PASSWORD, for curl/scripted access. Username irrelevant.
    expected_bearer = f"Bearer {ADMIN_PASSWORD}"
    if hmac.compare_digest(auth.encode("utf-8"), expected_bearer.encode("utf-8")):
        return True

    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8", errors="replace")
        except Exception:
            return False
        username, _, password = decoded.partition(":")
        if (
            username
            and password
            and hmac.compare_digest(username, ADMIN_USERNAME)
            and hmac.compare_digest(password, ADMIN_PASSWORD)
        ):
            return True

    return False


def _same_origin(headers) -> bool:
    """CSRF check: require Origin header to match the request host.

    Browsers always send Origin on cross-origin POSTs and on same-origin
    POSTs with fetch/XHR, so an absent Origin on state-changing requests
    is suspicious and rejected.
    """
    origin = headers.get("Origin", "")
    host = headers.get("Host", "")
    if not origin or not host:
        return False
    # Expected origin is https://<host> (Vercel enforces HTTPS).
    return origin == f"https://{host}"


class handler(BaseHTTPRequestHandler):
    def _apply_security_headers(self, nonce: str | None = None):
        for name, value in _STATIC_SECURITY_HEADERS:
            self.send_header(name, value)
        if nonce is not None:
            self.send_header("Content-Security-Policy", _csp_with_nonce(nonce))

    def _send_unauthorized(self, body: bytes = b"Unauthorized"):
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("WWW-Authenticate", 'Basic realm="Food Admin", charset="UTF-8"')
        # No nonce needed for plain-text body; emit a strict default CSP.
        self.send_header("Content-Security-Policy", _csp_with_nonce("none"))
        self._apply_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not _authorized(self.headers):
            self._send_unauthorized(
                b"Unauthorized. Authenticate via HTTP Basic Auth "
                b"(ADMIN_USERNAME / ADMIN_PASSWORD) or Authorization: "
                b"Bearer <ADMIN_PASSWORD>."
            )
            return

        nonce = _new_nonce()
        try:
            body = build_html(nonce)
        except Exception:
            print("admin_stats error:", traceback.format_exc(), flush=True)
            body = "<pre>Error rendering dashboard (see logs)</pre>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._apply_security_headers(nonce=nonce)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_POST(self):
        """Handle admin actions (delete meal) via AJAX."""
        if not _authorized(self.headers):
            self._send_unauthorized(b'{"ok": false, "error": "unauthorized"}')
            return

        # CSRF: require same-origin Origin header on state-changing POSTs.
        if not _same_origin(self.headers):
            self._json_response(403, {"ok": False, "error": "origin mismatch"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        # Admin actions send tiny JSON payloads — cap at 8 KB.
        if length > 8 * 1024:
            self._json_response(413, {"ok": False, "error": "payload too large"})
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._json_response(400, {"ok": False, "error": "bad json"})
            return

        action = body.get("action")
        if action == "delete_meal":
            meal_id = body.get("meal_id")
            user_id = body.get("user_id")
            if not meal_id or not user_id:
                self._json_response(400, {"ok": False, "error": "meal_id and user_id required"})
                return
            conn = get_conn()
            try:
                init_db(conn)
                deleted = delete_meal(conn, int(meal_id), int(user_id))
                if not deleted:
                    self._json_response(404, {"ok": False, "error": "meal not found"})
                    return
                recalc_daily_log(conn, int(user_id), deleted["date"])
                self._json_response(200, {"ok": True, "deleted": deleted["description"][:60]})
            except Exception as e:
                print("admin delete error:", traceback.format_exc(), flush=True)
                self._json_response(500, {"ok": False, "error": str(e)})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        elif action == "delete_user":
            user_id = body.get("user_id")
            if not user_id:
                self._json_response(400, {"ok": False, "error": "user_id required"})
                return
            conn = get_conn()
            try:
                init_db(conn)
                existed = delete_user_all_data(conn, int(user_id))
                if not existed:
                    self._json_response(404, {"ok": False, "error": "user not found"})
                    return
                self._json_response(200, {"ok": True})
            except Exception as e:
                print("admin delete_user error:", traceback.format_exc(), flush=True)
                self._json_response(500, {"ok": False, "error": str(e)})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        elif action == "delete_meals_bulk":
            meal_ids = body.get("meal_ids") or []
            if not meal_ids or not isinstance(meal_ids, list):
                self._json_response(400, {"ok": False, "error": "meal_ids list required"})
                return
            # Hard cap on a single bulk-delete request. Genuine admin work
            # rarely exceeds a handful at once; the lower cap limits damage
            # from a future XSS that automates the click.
            if len(meal_ids) > 50:
                self._json_response(400, {"ok": False, "error": "max 50 per bulk delete"})
                return
            conn = get_conn()
            try:
                init_db(conn)
                deleted_count = 0
                affected = {}  # user_id -> set of dates
                for mid in meal_ids:
                    try:
                        d = delete_meal_admin(conn, int(mid))
                        if d:
                            deleted_count += 1
                            affected.setdefault(d["user_id"], set()).add(d["date"])
                    except Exception:
                        pass
                for uid, dates in affected.items():
                    for dt in dates:
                        recalc_daily_log(conn, uid, dt)
                self._json_response(200, {"ok": True, "deleted": deleted_count})
            except Exception as e:
                print("admin bulk delete error:", traceback.format_exc(), flush=True)
                self._json_response(500, {"ok": False, "error": str(e)})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            self._json_response(400, {"ok": False, "error": f"unknown action: {action}"})

    def _json_response(self, code: int, data: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # JSON responses don't render scripts, so no nonce is needed; still
        # emit the strict CSP for defense in depth.
        self.send_header("Content-Security-Policy", _csp_with_nonce("none"))
        self._apply_security_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def _esc(s) -> str:
    """HTML-escape including quotes — safe for attribute interpolation."""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


# Hard caps on rows rendered into the admin HTML. The auto-refresh fires every
# 60s, so unbounded fetches grow linearly with usage and eventually OOM the
# function. These limits keep the response under a few hundred KB at scale.
ADMIN_USERS_LIMIT = 200
ADMIN_MEALS_LIMIT = 500


def build_html(nonce: str = "") -> str:
    conn = get_conn()
    try:
        init_db(conn)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        n_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM meals")
        n_meals = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM daily_logs")
        n_days = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM daily_recommendations")
        n_recs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT user_id) FROM meals WHERE date::date = CURRENT_DATE")
        active_today = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT user_id) FROM meals WHERE date::date >= CURRENT_DATE - INTERVAL '7 days'")
        active_week = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users WHERE user_id IN (SELECT user_id FROM user_profiles)")
        n_with_profile = cur.fetchone()[0]
        n_without_profile = n_users - n_with_profile

        cur.execute("SELECT COALESCE(SUM(amount_ml), 0) FROM water_logs")
        total_water_ml = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM user_profiles WHERE target_weight_kg IS NOT NULL")
        n_with_target = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM daily_logs WHERE summary_sent = 1"
        )
        n_summaries_sent = cur.fetchone()[0]

        # Adherence (±15% of target) AND average daily calories, per user.
        cur.execute("""
            SELECT dl.user_id,
                   COUNT(*) FILTER (WHERE p.daily_calorie_target IS NOT NULL
                     AND dl.total_calories BETWEEN p.daily_calorie_target * 0.85
                     AND p.daily_calorie_target * 1.15) AS adherent_days,
                   COUNT(*) AS total_days,
                   AVG(dl.total_calories) FILTER (WHERE dl.total_calories > 0) AS avg_cal
            FROM daily_logs dl
            LEFT JOIN user_profiles p ON p.user_id = dl.user_id
            GROUP BY dl.user_id
        """)
        adherence_map = {}
        avg_cal_map = {}
        for uid, adh, tot, avg_cal in cur.fetchall():
            if tot:
                adherence_map[uid] = round(adh / tot * 100)
            if avg_cal is not None:
                avg_cal_map[uid] = round(float(avg_cal))

        cur.execute(
            """
            SELECT u.user_id, COALESCE(u.username, ''), u.created_at,
                   COUNT(m.id) AS meals, MAX(m.created_at) AS last_meal,
                   p.age, p.sex, p.weight_kg, p.height_cm,
                   p.gym_per_week, p.goal, p.daily_calorie_target,
                   p.target_weight_kg
            FROM users u
            LEFT JOIN meals m ON m.user_id = u.user_id
            LEFT JOIN user_profiles p ON p.user_id = u.user_id
            GROUP BY u.user_id, u.username, u.created_at,
                     p.age, p.sex, p.weight_kg, p.height_cm,
                     p.gym_per_week, p.goal, p.daily_calorie_target,
                     p.target_weight_kg
            ORDER BY meals DESC, u.created_at DESC
            LIMIT %s
            """,
            (ADMIN_USERS_LIMIT,),
        )
        user_rows = cur.fetchall()

        # Newest meals first, capped at ADMIN_MEALS_LIMIT for response size.
        cur.execute(
            """
            SELECT m.id, m.user_id, COALESCE(u.username, ''), m.date, m.meal_type,
                   m.description, m.calories, m.protein_g, m.carbs_g, m.fat_g,
                   m.fiber_g, m.sugar_g, m.created_at
            FROM meals m LEFT JOIN users u ON u.user_id = m.user_id
            ORDER BY m.id DESC
            LIMIT %s
            """,
            (ADMIN_MEALS_LIMIT,),
        )
        meal_rows = cur.fetchall()

        # Top foods by frequency
        cur.execute("""
            SELECT COALESCE(description, '—'), COUNT(*) AS cnt,
                   ROUND(AVG(calories)::numeric, 0) AS avg_cal
            FROM meals
            WHERE description IS NOT NULL AND description != ''
            GROUP BY description
            ORDER BY cnt DESC
            LIMIT 20
        """)
        top_foods = cur.fetchall()

        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _SEX_UA = {"male": "♂ Чол", "female": "♀ Жін"}
    _GOAL_UA = {"lose": "🔥 Схуднути", "maintain": "⚖️ Підтримка", "gain": "💪 Набір"}
    _GYM_UA = {"0": "0×", "1-2": "1–2×", "3-4": "3–4×", "5-6": "5–6×", "7": "7×"}

    # Users table
    user_tbody = ""
    for r in user_rows:
        (uid, uname, joined, meals, last, age, sex, weight, height,
         gym, goal, cal_target, target_weight) = r
        adh = adherence_map.get(uid)
        adh_cell = f"{adh}%" if adh is not None else "—"
        adh_color = ""
        if adh is not None:
            adh_color = "color:#4caf50" if adh >= 70 else ("color:#ff9800" if adh >= 40 else "color:#e94560")
        if target_weight and weight and goal in ("lose", "gain"):
            delta = float(weight) - float(target_weight)
            rem = max(0.0, delta) if goal == "lose" else max(0.0, -delta)
            sign = "−" if goal == "lose" else "+"
            tw_cell = f"{target_weight} ({sign}{rem:.1f})"
        elif target_weight:
            tw_cell = f"{target_weight}"
        else:
            tw_cell = "—"
        user_tbody += (
            f'<tr data-uid="{_esc(uid)}">'
            f'<td class="clickable">{_esc(uid)}</td>'
            f'<td class="clickable">{_esc(uname)}</td>'
            f"<td class='num clickable'>{_esc(age) if age else '—'}</td>"
            f"<td class='clickable'>{_SEX_UA.get(sex or '', '—')}</td>"
            f"<td class='num clickable'>{_esc(weight) if weight else '—'}</td>"
            f"<td class='num clickable'>{_esc(height) if height else '—'}</td>"
            f"<td class='clickable'>{_GYM_UA.get(gym or '', gym or '—')}</td>"
            f"<td class='clickable'>{_GOAL_UA.get(goal or '', goal or '—')}</td>"
            f"<td class='num clickable'>{tw_cell}</td>"
            f"<td class='num clickable'>{_esc(cal_target) if cal_target else '—'}</td>"
            f"<td class='num clickable'>{avg_cal_map.get(uid, '—')}</td>"
            f"<td class='num clickable'>{_esc(meals)}</td>"
            f"<td class='num clickable' style='{adh_color}'>{adh_cell}</td>"
            f"<td class='clickable'>{_esc((last or '—')[:10])}</td>"
            f"<td class='clickable'>{_esc((joined or '')[:10])}</td>"
            f'<td><button type="button" class="btn-del btn-del-user" data-uid="{_esc(uid)}" title="Видалити користувача">🗑</button></td>'
            f"</tr>\n"
        )

    # Meals table — all history
    meals_tbody = ""
    for r in meal_rows:
        mid, uid, uname, date, mt, desc, cal, p, c, f, fib, sug, ts = r
        meals_tbody += (
            f'<tr data-mid="{_esc(mid)}" data-uid="{_esc(uid)}">'
            f'<td><input type="checkbox" class="meal-check" value="{_esc(mid)}"></td>'
            f"<td>{_esc((ts or '')[:16])}</td>"
            f"<td>{_esc(uname)} <span class='uid'>({_esc(uid)})</span></td>"
            f"<td>{_esc(date)}</td>"
            f"<td>{_esc(mt)}</td>"
            f"<td>{_esc((desc or '')[:80])}</td>"
            f"<td class='num'>{round(cal or 0)}</td>"
            f"<td class='num'>{round(p or 0)}</td>"
            f"<td class='num'>{round(c or 0)}</td>"
            f"<td class='num'>{round(f or 0)}</td>"
            f"<td class='num'>{round(fib or 0)}</td>"
            f"<td class='num'>{round(sug or 0)}</td>"
            f'<td><button type="button" class="btn-del btn-del-meal" data-mid="{_esc(mid)}" data-uid="{_esc(uid)}" title="Видалити">🗑</button></td>'
            f"</tr>\n"
        )

    # Top foods list
    top_foods_html = ""
    for i, (food_name, cnt, avg_cal) in enumerate(top_foods, 1):
        bar_w = round(cnt / max(top_foods[0][1], 1) * 100) if top_foods else 0
        top_foods_html += (
            f"<div class='tf-row'>"
            f"<span class='tf-rank'>{i}</span>"
            f"<div class='tf-bar-wrap'>"
            f"<div class='tf-bar' style='width:{bar_w}%'></div>"
            f"<span class='tf-name'>{_esc(food_name[:60])}</span>"
            f"</div>"
            f"<span class='tf-cnt'>{cnt}×</span>"
            f"<span class='tf-cal'>{int(avg_cal or 0)} ккал</span>"
            f"</div>\n"
        )

    # Build modal user data as JSON for JS lookup
    user_modal_data = {}
    for r in user_rows:
        (uid, uname, joined, meals, last, age, sex, weight, height,
         gym, goal, cal_target, target_weight) = r
        user_modal_data[str(uid)] = {
            "uid": uid, "uname": uname or "—",
            "joined": str(joined or "")[:10],
            "meals": meals,
            "last": str(last or "")[:10],
            "age": age, "sex": _SEX_UA.get(sex or "", "—"),
            "weight": weight, "height": height,
            "gym": _GYM_UA.get(gym or "", gym or "—"),
            "goal": _GOAL_UA.get(goal or "", goal or "—"),
            "cal_target": cal_target,
            "target_weight": target_weight,
            "avg_cal": avg_cal_map.get(uid),
            "adherence": adherence_map.get(uid),
        }
    user_modal_json = json.dumps(user_modal_data, ensure_ascii=False, default=str)

    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KusWise Bot — Admin</title>
<link rel="icon" type="image/png" href="/logo.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, 'Segoe UI', sans-serif; background: #0f0f1a; color: #e0e0e0; margin: 0; padding: 20px; }}
  h1 {{ color: #e94560; margin-bottom: 4px; display:flex; align-items:center; gap:14px; }}
  h1 img.logo {{ width: 48px; height: 48px; border-radius: 50%; object-fit: cover; background:#000; }}
  h2 {{ background: #16213e; padding: 10px 16px; border-radius: 8px; color: #e0e0e0; margin-top: 30px; display:flex; align-items:center; gap:10px; }}
  h2 .h2-actions {{ margin-left: auto; display:flex; gap:8px; }}
  .subtitle {{ color: #888; margin-top: 0; }}
  .refresh-info {{ color: #555; font-size: 0.8em; float: right; }}

  /* Top-level admin tabs */
  .admin-tabs {{ display:flex; gap:8px; margin: 16px 0 8px; flex-wrap:wrap; }}
  .admin-tabs button {{
    background: #16213e; color: #ccc; border: 1px solid #2a2a4a;
    padding: 9px 18px; border-radius: 10px; font-size: 0.95em;
    cursor: pointer; font-family: inherit; font-weight: 500;
  }}
  .admin-tabs button:hover {{ border-color: #e94560; color: #e94560; }}
  .admin-tabs button.active {{
    background: #e94560; border-color: #e94560; color: #fff; font-weight: 600;
  }}
  section[hidden] {{ display: none !important; }}

  /* Stat cards */
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
  .card {{ background: #16213e; border-radius: 12px; padding: 20px 28px; min-width: 130px; text-align: center; }}
  .card .num {{ font-size: 2.2em; font-weight: bold; color: #e94560; }}
  .card .num.green {{ color: #4caf50; }}
  .card .label {{ color: #a0a0a0; margin-top: 4px; font-size: 0.9em; }}

  /* Onboarding funnel */
  .funnel {{ display:flex; gap:16px; flex-wrap:wrap; margin: 8px 0 20px; }}
  .funnel-item {{ background:#16213e; border-radius:10px; padding:14px 20px; flex:1; min-width:160px; }}
  .funnel-item .f-num {{ font-size:1.8em; font-weight:bold; color:#e94560; }}
  .funnel-item .f-num.green {{ color:#4caf50; }}
  .funnel-item .f-label {{ color:#a0a0a0; font-size:0.85em; margin-top:2px; }}
  .funnel-bar {{ height:6px; background:#2a2a4a; border-radius:3px; margin-top:8px; overflow:hidden; }}
  .funnel-fill {{ height:100%; background:#e94560; border-radius:3px; transition:width 0.5s; }}
  .funnel-fill.green {{ background:#4caf50; }}

  /* Top foods */
  .tf-row {{ display:flex; align-items:center; gap:10px; padding:5px 0; border-bottom:1px solid #1e1e3a; }}
  .tf-rank {{ color:#666; font-size:0.8em; min-width:22px; text-align:right; }}
  .tf-bar-wrap {{ flex:1; position:relative; height:24px; background:#1a1a2e; border-radius:4px; overflow:hidden; }}
  .tf-bar {{ position:absolute; left:0; top:0; height:100%; background:#0f3460; border-radius:4px; }}
  .tf-name {{ position:relative; z-index:1; padding:0 8px; font-size:0.88em; line-height:24px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:320px; }}
  .tf-cnt {{ min-width:40px; text-align:right; color:#e94560; font-size:0.9em; font-weight:bold; }}
  .tf-cal {{ min-width:70px; text-align:right; color:#888; font-size:0.85em; }}

  /* Filter bar */
  .filter-bar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 12px 0 8px; }}
  .filter-bar input, .filter-bar select {{
    background: #16213e; color: #e0e0e0; border: 1px solid #2a2a4a;
    padding: 7px 11px; border-radius: 6px; font-size: 0.9em;
  }}
  .filter-bar input::placeholder {{ color: #666; }}
  .filter-bar input:focus, .filter-bar select:focus {{ outline: none; border-color: #e94560; }}
  .filter-bar .count {{ color: #888; font-size: 0.85em; margin-left: auto; }}

  /* Tables */
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 30px; }}
  th, td {{ padding: 7px 10px; text-align: left; border-bottom: 1px solid #1e1e3a; white-space: nowrap; }}
  td {{ font-size: 0.9em; }}
  th {{ background: #0f3460; color: #fff; cursor: pointer; user-select: none; position: sticky; top: 0; }}
  th:hover {{ background: #1a4a80; }}
  th.no-sort {{ cursor: default; }}
  th.no-sort:hover {{ background: #0f3460; }}
  th .arrow {{ font-size: 0.7em; margin-left: 4px; opacity: 0.5; }}
  th.sorted .arrow {{ opacity: 1; color: #e94560; }}
  tr:hover {{ background: #1a1a30; }}
  td.clickable {{ cursor: pointer; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .uid {{ color: #666; font-size: 0.8em; }}

  .table-wrap {{ max-height: 70vh; overflow: auto; border: 1px solid #1e1e3a; border-radius: 8px; }}
  .table-wrap table {{ margin: 0; }}

  /* Buttons */
  .btn-del {{
    background: transparent; border: 1px solid #e94560; color: #e94560;
    border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 1em;
    transition: all 0.15s;
  }}
  .btn-del:hover {{ background: #e94560; color: #fff; }}
  .btn-del:disabled {{ opacity: 0.3; cursor: default; }}
  .btn-del.done {{ border-color: #4caf50; color: #4caf50; }}

  .btn-action {{
    background: #16213e; border: 1px solid #2a2a4a; color: #ccc;
    border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 0.88em;
    transition: all 0.15s; white-space: nowrap;
  }}
  .btn-action:hover {{ border-color: #e94560; color: #e94560; }}
  .btn-action.danger {{ border-color: #e94560; color: #e94560; }}
  .btn-action.danger:hover {{ background: #e94560; color: #fff; }}

  /* User search bar */
  .user-filter-bar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:12px 0 8px; }}
  .user-filter-bar input, .user-filter-bar select {{
    background: #16213e; color: #e0e0e0; border: 1px solid #2a2a4a;
    padding: 7px 11px; border-radius: 6px; font-size: 0.9em;
  }}
  .user-filter-bar input::placeholder {{ color: #666; }}
  .user-filter-bar input:focus, .user-filter-bar select:focus {{ outline: none; border-color: #e94560; }}
  .user-filter-bar .count {{ color: #888; font-size: 0.85em; margin-left: auto; }}

  /* Modal */
  .modal-overlay {{
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7);
    z-index: 1000; align-items: center; justify-content: center;
  }}
  .modal-overlay.open {{ display: flex; }}
  .modal {{
    background: #16213e; border-radius: 14px; padding: 28px 32px;
    max-width: 520px; width: 95%; max-height: 80vh; overflow-y: auto;
    position: relative;
  }}
  .modal h3 {{ color: #e94560; margin-top:0; }}
  .modal .close-btn {{
    position: absolute; top: 14px; right: 16px;
    background: none; border: none; color: #888; font-size: 1.4em; cursor: pointer;
  }}
  .modal .close-btn:hover {{ color: #e94560; }}
  .modal-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; margin-top: 12px; }}
  .modal-field {{ padding: 8px 12px; background: #0f0f1a; border-radius: 8px; }}
  .modal-field .mf-label {{ color:#888; font-size:0.78em; text-transform:uppercase; letter-spacing:.04em; }}
  .modal-field .mf-val {{ font-size:1em; margin-top:2px; }}

  @media (max-width: 700px) {{
    .cards {{ flex-direction: column; }}
    th, td {{ padding: 5px 6px; font-size: 0.8em; }}
    .modal {{ padding: 16px; }}
    .modal-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<!-- User detail modal -->
<div class="modal-overlay" id="userModal" onclick="closeModal(event)">
  <div class="modal">
    <button class="close-btn" onclick="document.getElementById('userModal').classList.remove('open')">✕</button>
    <h3 id="modalTitle">Профіль користувача</h3>
    <div class="modal-grid" id="modalGrid"></div>
  </div>
</div>

<h1><img class="logo" src="/logo.png" alt=""> KusWise Bot — Admin</h1>
<p class="subtitle">Повна статистика та керування ботом <span class="refresh-info">↻ авто-оновлення кожні 60 с</span></p>

<nav class="admin-tabs" id="adminTabs">
  <button type="button" class="active" data-tab="overview">📊 Огляд</button>
  <button type="button" data-tab="meals">🍽️ Страви</button>
</nav>

<section id="tab-overview">
<div class="cards">
  <div class="card"><div class="num">{n_users}</div><div class="label">Всього юзерів</div></div>
  <div class="card"><div class="num green">{active_today}</div><div class="label">Активні сьогодні</div></div>
  <div class="card"><div class="num">{active_week}</div><div class="label">Активні за тиждень</div></div>
  <div class="card"><div class="num">{n_meals}</div><div class="label">Страв записано</div></div>
  <div class="card"><div class="num">{n_days}</div><div class="label">Днів з даними</div></div>
  <div class="card"><div class="num">{n_recs}</div><div class="label">Рекомендацій</div></div>
  <div class="card"><div class="num">{n_summaries_sent}</div><div class="label">Нічних підсумків надіслано</div></div>
  <div class="card"><div class="num">{round(total_water_ml / 1000)}</div><div class="label">Літрів води записано</div></div>
  <div class="card"><div class="num">{n_with_target}</div><div class="label">З цільовою вагою</div></div>
</div>

<h2>🚀 Онбординг</h2>
<div class="funnel">
  <div class="funnel-item">
    <div class="f-num">{n_users}</div>
    <div class="f-label">Запустили бот</div>
    <div class="funnel-bar"><div class="funnel-fill" style="width:100%"></div></div>
  </div>
  <div class="funnel-item">
    <div class="f-num green">{n_with_profile}</div>
    <div class="f-label">Заповнили профіль</div>
    <div class="funnel-bar"><div class="funnel-fill green" style="width:{round(n_with_profile / max(n_users, 1) * 100)}%"></div></div>
  </div>
  <div class="funnel-item">
    <div class="f-num" style="color:#ff9800">{n_without_profile}</div>
    <div class="f-label">Без профілю</div>
    <div class="funnel-bar"><div class="funnel-fill" style="width:{round(n_without_profile / max(n_users, 1) * 100)}%; background:#ff9800"></div></div>
  </div>
  <div class="funnel-item">
    <div class="f-num">{round(n_with_profile / max(n_users, 1) * 100)}%</div>
    <div class="f-label">Конверсія онбордингу</div>
    <div class="funnel-bar"><div class="funnel-fill" style="width:{round(n_with_profile / max(n_users, 1) * 100)}%; background:#9c27b0"></div></div>
  </div>
</div>

<h2>👥 Користувачі
  <div class="h2-actions">
    <button class="btn-action" onclick="exportCSV('tblUsers', 'users.csv')">⬇ CSV</button>
  </div>
</h2>

<div class="user-filter-bar">
  <input type="text" id="searchUsers" placeholder="🔍 Пошук (id, username…)" style="min-width:220px;" oninput="applyUserFilters()">
  <select id="filterGoal" onchange="applyUserFilters()">
    <option value="">Всі цілі</option>
    <option value="Схуднути">Схуднути</option>
    <option value="Підтримка">Підтримка</option>
    <option value="Набір">Набір</option>
  </select>
  <select id="filterSex" onchange="applyUserFilters()">
    <option value="">Обидві статі</option>
    <option value="Чол">Чоловіки</option>
    <option value="Жін">Жінки</option>
  </select>
  <span class="count" id="usersCount"></span>
</div>

<div class="table-wrap">
<table id="tblUsers">
<thead><tr>
  <th data-col="0" data-type="num">user_id <span class="arrow">▲</span></th>
  <th data-col="1" data-type="str">Username <span class="arrow">▲</span></th>
  <th data-col="2" data-type="num">Вік <span class="arrow">▲</span></th>
  <th data-col="3" data-type="str">Стать <span class="arrow">▲</span></th>
  <th data-col="4" data-type="num">Вага <span class="arrow">▲</span></th>
  <th data-col="5" data-type="num">Зріст <span class="arrow">▲</span></th>
  <th data-col="6" data-type="str">Зал/тиж <span class="arrow">▲</span></th>
  <th data-col="7" data-type="str">Мета <span class="arrow">▲</span></th>
  <th data-col="8" data-type="num">Ціль ваги <span class="arrow">▲</span></th>
  <th data-col="9" data-type="num">ккал/день <span class="arrow">▲</span></th>
  <th data-col="10" data-type="num">Сер. ккал <span class="arrow">▲</span></th>
  <th data-col="11" data-type="num">Страв <span class="arrow">▲</span></th>
  <th data-col="12" data-type="num">Адгер% <span class="arrow">▲</span></th>
  <th data-col="13" data-type="str">Остання страва <span class="arrow">▲</span></th>
  <th data-col="14" data-type="str">Приєднався <span class="arrow">▲</span></th>
  <th class="no-sort">Дія</th>
</tr></thead>
<tbody>{user_tbody}</tbody>
</table>
</div>
</section>

<section id="tab-meals" hidden>

<h2>🏆 Топ-20 страв
  <div class="h2-actions"></div>
</h2>
<div style="background:#16213e; border-radius:10px; padding:16px 20px; margin-bottom:20px;">
{top_foods_html or '<p style="color:#666">Немає даних</p>'}
</div>

<h2>🍽️ Вся історія страв
  <div class="h2-actions">
    <button class="btn-action danger" id="bulkDeleteBtn" onclick="bulkDeleteMeals()" style="display:none">🗑 Видалити обрані</button>
    <button class="btn-action" onclick="exportCSV('tblMeals', 'meals.csv')">⬇ CSV</button>
  </div>
</h2>

<div class="filter-bar">
  <input type="text" id="searchMeals" placeholder="🔍 Пошук (назва, тип, користувач…)" style="min-width:240px;">
  <select id="filterUser"><option value="">Всі користувачі</option></select>
  <select id="filterType">
    <option value="">Всі типи</option>
    <option value="breakfast">Сніданок</option>
    <option value="lunch">Обід</option>
    <option value="dinner">Вечеря</option>
    <option value="snack">Перекус</option>
  </select>
  <input type="date" id="filterDateFrom" title="Від дати">
  <input type="date" id="filterDateTo" title="До дати">
  <span class="count" id="mealsCount"></span>
</div>

<div class="table-wrap">
<table id="tblMeals">
<thead><tr>
  <th class="no-sort"><input type="checkbox" id="checkAll" title="Вибрати всі видимі"></th>
  <th data-col="1" data-type="str">Час <span class="arrow">▲</span></th>
  <th data-col="2" data-type="str">Користувач <span class="arrow">▲</span></th>
  <th data-col="3" data-type="str">Дата <span class="arrow">▲</span></th>
  <th data-col="4" data-type="str">Тип <span class="arrow">▲</span></th>
  <th data-col="5" data-type="str">Опис <span class="arrow">▲</span></th>
  <th data-col="6" data-type="num">ккал <span class="arrow">▲</span></th>
  <th data-col="7" data-type="num">Б <span class="arrow">▲</span></th>
  <th data-col="8" data-type="num">В <span class="arrow">▲</span></th>
  <th data-col="9" data-type="num">Ж <span class="arrow">▲</span></th>
  <th data-col="10" data-type="num">Кліт <span class="arrow">▲</span></th>
  <th data-col="11" data-type="num">Цук <span class="arrow">▲</span></th>
  <th class="no-sort">Дія</th>
</tr></thead>
<tbody>{meals_tbody}</tbody>
</table>
</div>
</section>

<script nonce="{_esc(nonce)}">
const USER_DATA = {user_modal_json};

/* --- Top-level tab switcher --- */
(function(){{
  const tabs = document.querySelectorAll('#adminTabs button');
  const sections = ['overview', 'meals'];
  function setTab(name) {{
    tabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    sections.forEach(s => {{
      const el = document.getElementById('tab-' + s);
      if (el) el.hidden = (s !== name);
    }});
    try {{ history.replaceState(null, '', '#' + name); }} catch(e) {{}}
  }}
  tabs.forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  if (location.hash === '#meals') setTab('meals');
}})();

/* --- Auto-refresh every 60s --- */
setTimeout(() => location.reload(), 60000);

/* --- Sortable tables --- */
document.querySelectorAll('table').forEach(table => {{
  const headers = table.querySelectorAll('th[data-col]');
  let curCol = -1, asc = true;
  headers.forEach(th => {{
    th.addEventListener('click', () => {{
      const col = +th.dataset.col;
      const type = th.dataset.type;
      if (curCol === col) asc = !asc; else {{ curCol = col; asc = true; }}
      headers.forEach(h => h.classList.remove('sorted'));
      th.classList.add('sorted');
      th.querySelector('.arrow').textContent = asc ? '▲' : '▼';
      const tbody = table.querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => {{
        let va = a.cells[col]?.textContent.trim() || '';
        let vb = b.cells[col]?.textContent.trim() || '';
        if (type === 'num') {{
          va = parseFloat(va.replace(/[^\\d.-]/g, '')) || 0;
          vb = parseFloat(vb.replace(/[^\\d.-]/g, '')) || 0;
          return asc ? va - vb : vb - va;
        }}
        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
      }});
      rows.forEach(r => tbody.appendChild(r));
    }});
  }});
}});

/* --- Users filter --- */
const usersTable = document.getElementById('tblUsers');
const userRows = () => Array.from(usersTable.querySelectorAll('tbody tr'));
function applyUserFilters() {{
  const q = document.getElementById('searchUsers').value.toLowerCase();
  const goal = document.getElementById('filterGoal').value;
  const sex = document.getElementById('filterSex').value;
  let visible = 0;
  userRows().forEach(row => {{
    const text = row.textContent.toLowerCase();
    const rowGoal = row.cells[7]?.textContent || '';
    const rowSex = row.cells[3]?.textContent || '';
    let show = true;
    if (q && !text.includes(q)) show = false;
    if (goal && !rowGoal.includes(goal)) show = false;
    if (sex && !rowSex.includes(sex)) show = false;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  document.getElementById('usersCount').textContent = `Показано: ${{visible}} / ${{userRows().length}}`;
}}
applyUserFilters();

/* --- Meals filtering --- */
const mealsTable = document.getElementById('tblMeals');
const mealsRows = Array.from(mealsTable.querySelectorAll('tbody tr'));
const searchInput = document.getElementById('searchMeals');
const filterUser = document.getElementById('filterUser');
const filterType = document.getElementById('filterType');
const filterDateFrom = document.getElementById('filterDateFrom');
const filterDateTo = document.getElementById('filterDateTo');
const mealsCount = document.getElementById('mealsCount');

const usersMap = new Map();
mealsRows.forEach(r => {{
  const cell = r.cells[2]?.textContent.trim() || '';
  if (cell && !usersMap.has(cell)) usersMap.set(cell, cell);
}});
Array.from(usersMap.keys()).sort().forEach(u => {{
  const opt = document.createElement('option');
  opt.value = u; opt.textContent = u;
  filterUser.appendChild(opt);
}});

function applyFilters() {{
  const q = searchInput.value.toLowerCase();
  const user = filterUser.value.toLowerCase();
  const type = filterType.value.toLowerCase();
  const dateFrom = filterDateFrom.value;
  const dateTo = filterDateTo.value;
  let visible = 0;
  mealsRows.forEach(row => {{
    const text = row.textContent.toLowerCase();
    const rowUser = (row.cells[2]?.textContent || '').toLowerCase();
    const rowType = (row.cells[4]?.textContent || '').toLowerCase();
    const rowDate = (row.cells[3]?.textContent || '').trim();
    let show = true;
    if (q && !text.includes(q)) show = false;
    if (user && !rowUser.includes(user)) show = false;
    if (type && !rowType.includes(type)) show = false;
    if (dateFrom && rowDate < dateFrom) show = false;
    if (dateTo && rowDate > dateTo) show = false;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  mealsCount.textContent = `Показано: ${{visible}} / ${{mealsRows.length}}`;
  updateBulkBtn();
}}

searchInput.addEventListener('input', applyFilters);
filterUser.addEventListener('change', applyFilters);
filterType.addEventListener('change', applyFilters);
filterDateFrom.addEventListener('change', applyFilters);
filterDateTo.addEventListener('change', applyFilters);
applyFilters();

/* --- Check-all + bulk delete toggle --- */
function updateBulkBtn() {{
  const checked = document.querySelectorAll('.meal-check:checked').length;
  const btn = document.getElementById('bulkDeleteBtn');
  btn.style.display = checked > 0 ? '' : 'none';
  btn.textContent = `🗑 Видалити обрані (${{checked}})`;
}}

document.getElementById('checkAll').addEventListener('change', function() {{
  mealsRows.filter(r => r.style.display !== 'none').forEach(r => {{
    const cb = r.querySelector('.meal-check');
    if (cb) cb.checked = this.checked;
  }});
  updateBulkBtn();
}});

document.querySelectorAll('.meal-check').forEach(cb => cb.addEventListener('change', updateBulkBtn));

/* --- Delegated clicks: user-row modal + delete buttons (bulletproof) --- */
document.getElementById('tblUsers').addEventListener('click', function(e) {{
  const delBtn = e.target.closest('.btn-del-user');
  if (delBtn) {{
    e.preventDefault(); e.stopPropagation();
    deleteUser(delBtn);
    return;
  }}
  const cell = e.target.closest('td.clickable');
  if (cell) {{
    const row = cell.closest('tr');
    if (row) showUserModal(row);
  }}
}});
document.getElementById('tblMeals').addEventListener('click', function(e) {{
  const delBtn = e.target.closest('.btn-del-meal');
  if (delBtn) {{
    e.preventDefault(); e.stopPropagation();
    deleteMeal(delBtn);
  }}
}});

/* --- User modal --- */
function showUserModal(row) {{
  const uid = row.dataset.uid;
  const d = USER_DATA[uid];
  if (!d) return;
  document.getElementById('modalTitle').textContent = `@${{d.uname}} (${{d.uid}})`;
  const adh = d.adherence != null ? d.adherence + '%' : '—';
  const fields = [
    ['Telegram ID', d.uid],
    ['Username', '@' + d.uname],
    ['Приєднався', d.joined],
    ['Остання страва', d.last],
    ['Страв всього', d.meals],
    ['Адгерентність', adh],
    ['Вік', d.age ? d.age + ' р.' : '—'],
    ['Стать', d.sex],
    ['Вага', d.weight ? d.weight + ' кг' : '—'],
    ['Зріст', d.height ? d.height + ' см' : '—'],
    ['Зал/тиж', d.gym],
    ['Мета', d.goal],
    ['Цільова вага', d.target_weight ? d.target_weight + ' кг' : '—'],
    ['ккал/день (ціль)', d.cal_target ? d.cal_target + ' ккал' : '—'],
    ['Сер. ккал/день', d.avg_cal != null ? d.avg_cal + ' ккал' : '—'],
  ];
  document.getElementById('modalGrid').innerHTML = fields.map(([label, val]) =>
    `<div class="modal-field"><div class="mf-label">${{label}}</div><div class="mf-val">${{val ?? '—'}}</div></div>`
  ).join('');
  document.getElementById('userModal').classList.add('open');
}}

function closeModal(e) {{
  if (e.target === document.getElementById('userModal')) {{
    document.getElementById('userModal').classList.remove('open');
  }}
}}

document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') document.getElementById('userModal').classList.remove('open');
}});

/* --- CSV export --- */
function exportCSV(tableId, filename) {{
  const table = document.getElementById(tableId);
  const rows = [];
  table.querySelectorAll('tr').forEach(tr => {{
    const cells = [];
    tr.querySelectorAll('th, td').forEach(td => {{
      // Skip checkbox column
      if (td.querySelector('input[type=checkbox]')) return;
      let text = td.textContent.trim().replace(/\\s+/g, ' ');
      if (text.includes(',') || text.includes('"') || text.includes('\\n')) {{
        text = '"' + text.replace(/"/g, '""') + '"';
      }}
      cells.push(text);
    }});
    if (cells.length) rows.push(cells.join(','));
  }});
  const blob = new Blob([rows.join('\\n')], {{ type: 'text/csv;charset=utf-8;' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}}

/* --- Delete single meal --- */
async function deleteMeal(btn) {{
  const row = btn.closest('tr');
  const mid = row.dataset.mid;
  const uid = row.dataset.uid;
  const desc = row.cells[5]?.textContent.trim() || '';
  if (!confirm(`Видалити страву "${{desc}}"?`)) return;
  btn.disabled = true; btn.textContent = '...';
  try {{
    const resp = await fetch(window.location.pathname, {{
      method: 'POST', credentials: 'same-origin',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ action: 'delete_meal', meal_id: +mid, user_id: +uid }})
    }});
    const data = await resp.json();
    if (data.ok) {{
      row.style.transition = 'opacity 0.3s'; row.style.opacity = '0';
      setTimeout(() => {{ row.remove(); applyFilters(); }}, 300);
      const totalCard = document.querySelector('.card:nth-child(4) .num');
      if (totalCard) totalCard.textContent = Math.max(0, parseInt(totalCard.textContent) - 1);
    }} else {{
      alert('Помилка: ' + (data.error || 'невідома'));
      btn.disabled = false; btn.textContent = '🗑';
    }}
  }} catch(e) {{
    alert('Мережева помилка: ' + e.message);
    btn.disabled = false; btn.textContent = '🗑';
  }}
}}

/* --- Bulk delete meals --- */
async function bulkDeleteMeals() {{
  const checked = Array.from(document.querySelectorAll('.meal-check:checked'));
  if (!checked.length) return;
  if (!confirm(`Видалити ${{checked.length}} страв(и)? Це незворотньо.`)) return;
  const mealIds = checked.map(cb => +cb.value);
  const btn = document.getElementById('bulkDeleteBtn');
  btn.disabled = true; btn.textContent = 'Видалення…';
  try {{
    const resp = await fetch(window.location.pathname, {{
      method: 'POST', credentials: 'same-origin',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ action: 'delete_meals_bulk', meal_ids: mealIds }})
    }});
    const data = await resp.json();
    if (data.ok) {{
      checked.forEach(cb => {{
        const row = cb.closest('tr');
        if (row) row.remove();
      }});
      applyFilters();
      document.getElementById('checkAll').checked = false;
      updateBulkBtn();
      const totalCard = document.querySelector('.card:nth-child(4) .num');
      if (totalCard) totalCard.textContent = Math.max(0, parseInt(totalCard.textContent) - data.deleted);
    }} else {{
      alert('Помилка: ' + (data.error || 'невідома'));
      btn.disabled = false;
      updateBulkBtn();
    }}
  }} catch(e) {{
    alert('Мережева помилка: ' + e.message);
    btn.disabled = false;
    updateBulkBtn();
  }}
}}

/* --- Delete user --- */
async function deleteUser(btn) {{
  const row = btn.closest('tr');
  const uid = row.dataset.uid;
  const uname = row.cells[1]?.textContent.trim() || uid;
  // Typed-confirm: require the operator to type the user_id back. Defends
  // against accidental misclicks and slows down any future XSS-driven mass-delete.
  const typed = prompt(
    `Видалити користувача "${{uname}}" (${{uid}}) та ВСІ його дані?\\n` +
    `Це незворотньо. Введи user_id (${{uid}}) щоб підтвердити:`
  );
  if (typed === null) return;
  if (typed.trim() !== String(uid)) {{
    alert('Підтвердження не співпало. Видалення скасовано.');
    return;
  }}
  btn.disabled = true; btn.textContent = '...';
  try {{
    const resp = await fetch(window.location.pathname, {{
      method: 'POST', credentials: 'same-origin',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ action: 'delete_user', user_id: +uid }})
    }});
    const data = await resp.json();
    if (data.ok) {{
      row.style.transition = 'opacity 0.3s'; row.style.opacity = '0';
      setTimeout(() => row.remove(), 300);
      document.querySelectorAll(`#tblMeals tbody tr[data-uid="${{uid}}"]`).forEach(r => r.remove());
      applyFilters();
      const usersCard = document.querySelector('.card:nth-child(1) .num');
      if (usersCard) usersCard.textContent = Math.max(0, parseInt(usersCard.textContent) - 1);
    }} else {{
      alert('Помилка: ' + (data.error || 'невідома'));
      btn.disabled = false; btn.textContent = '🗑';
    }}
  }} catch(e) {{
    alert('Мережева помилка: ' + e.message);
    btn.disabled = false; btn.textContent = '🗑';
  }}
}}
</script>

</body>
</html>"""
