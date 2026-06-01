# CLAUDE.md — AI-agent specification

> **Read this first.** Rules + patterns + scars for any AI agent working in
> this repo. For the structural "what is the system" reference, read
> [`ARCHITECTURE.md`](./ARCHITECTURE.md) — that file is 600+ lines of tables,
> components, and module index; *this* file is the action-oriented rulebook.

`AGENTS.md` is a symlink to this file (cross-vendor agents that look for
`AGENTS.md` find the same content).

---

## 1. Quick start

```bash
# Run the test suite (270+ tests, ~1s). MUST run from the worktree dir.
cd /Users/victorgorlenko/Desktop/kuswise_bot/.claude/worktrees/<branch>
/Users/victorgorlenko/Desktop/kuswise_bot/.venv/bin/pytest tests/test_smoke.py -q

# i18n release gate (blocks commits with un-marked Cyrillic in .py files)
bash scripts/check_i18n.sh

# Deploy: push to main. NEVER run `vercel deploy`.
git push origin <branch>:main

# Probe daily health monitor (returns full JSON + posts to admin channel)
SECRET=$(grep '^CRON_SECRET=' .env | cut -d= -f2- | tr -d '"')
curl -sS -H "Authorization: Bearer $SECRET" https://kuswise-bot.vercel.app/api/cron_health_monitor
```

---

## 2. Hard rules (must follow)

These exist because each one has been violated and cost real time. Treat
them as invariants, not suggestions.

### Deploy

- **Deploy via `git push` to `main`. Never via `vercel` CLI.** The CLI
  bypasses platform-level deploy gates and the user's team-locked
  workflow. Saved in operator memory.
- **`vercel.json` is the cron registry.** Adding a cron schedule
  elsewhere (e.g. a runtime scheduler) won't fire.

### Schema

- **Bump `SCHEMA_VERSION` in `lib/database.py` on ANY DDL change** —
  new table, column, index, anything inside `init_db`. The version
  acts as a short-circuit so cold starts skip ~50 idempotent
  `CREATE/ALTER` round-trips. **Forgetting to bump means your new
  column never gets created on warm servers** — first code path that
  needs it will fail with "column does not exist."
- **`PROFILE_COLUMNS` (the SELECT list) and `_ALLOWED_PROFILE_FIELDS`
  (the write whitelist) must always be in sync.** Both in
  `lib/database.py`. Adding a column to one but not the other =
  `update_profile(new_col=...)` silently drops writes. See scar 5.2.

### Webhook / Telegram

- **`do_POST` must respond 200 BEFORE calling `process_update`.**
  Telegram's webhook timeout is ~60s; OpenAI vision can take 30–90s on
  hard meal photos. If our handler waits, Telegram retries the same
  update and the retry double-pops `pending_photos` → user sees
  `errors.pending_expired` ("10 minutes passed"). The current order in
  `api/webhook.py::do_POST` is: auth → read body → `_respond_ok()` →
  `wfile.flush()` → `process_update`. Don't move it.
- **All OpenAI clients construct with `timeout=45.0`.** SDK default is
  600s. A hung call would otherwise cascade into the Telegram-retry
  bug above. Applies to `lib/openai_vision.py`, `lib/openai_chat.py`,
  `lib/openai_nutrition.py`, `lib/openai_voice.py`.
- **Telegram 400/403 → stamp `blocked_at` and skip.** Use the
  `_send_with_autoblock` / `_send_with_autoptout` pattern. Never raise
  on a Telegram refusal — it's an expected user signal, not an error.

### Module hygiene

- **`from __future__ import annotations` at the top of every
  cron module** (and any module that uses pep-604 union syntax in
  annotations). Without it, a typo like `callable | None` kills the
  module at import time. The regression net for this lives in
  `test_all_cron_modules_import_cleanly`.
- **Cyrillic in `.py` files needs `# noqa: i18n`** at end of line.
  `scripts/check_i18n.sh` blocks commits otherwise. **Only string
  literals can carry Cyrillic with the marker** — comments cannot.
  Localized user strings live in `lib/i18n/dict_uk.json` /
  `dict_en.json`, never inline.

### Tests

- **Every code change must keep `tests/test_smoke.py` green.** Single
  file, 270+ tests, fake-cursor pattern for DB mocks (`_AdminConn` /
  `_FakeConn`), no test isolation between subtests.
- **Source-grep tests are first-class.** When a regression can't be
  caught with a unit test (e.g. "this column must be in the SELECT
  list AND the whitelist"), grep the source. See
  `test_admin_notified_at_wired_through_profile_helpers` for the
  pattern.

---

## 3. Patterns & invariants

### Cron handler shape

Every `api/cron_*.py` follows this skeleton:

```python
from __future__ import annotations
# ... imports ...

setup_sentry("cron_<name>")

def _authorized(headers) -> bool:
    """Constant-time bearer-token check. Fails closed if CRON_SECRET
    is unset."""
    if not CRON_SECRET:
        return False
    expected = f"Bearer {CRON_SECRET}"
    return hmac.compare_digest(headers.get("Authorization", "").encode(),
                               expected.encode())

class handler(BaseHTTPRequestHandler):
    @http_handler("cron_<name>")
    def do_GET(self):
        if not _authorized(self.headers):
            self.send_response(401); self.end_headers(); return
        try:
            result = run_<name>()
        except Exception as exc:
            error("cron_<name>_failed", exc=exc)
            result = {"ok": False, "error": "internal"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

def run_<name>() -> dict:
    conn = get_conn()
    run_id: int | None = None              # ← bracket pattern
    run_id = start_cron_run(conn, "cron_<name>")  # ← Phase B+
    status = "ok"; err_repr = None; result = {"ok": True}
    try:
        init_db(conn)                       # ← schema-version gated
        # ... actual work ...
        result = {"ok": True, ...counters...}
    except Exception as exc:
        status = "error"
        err_repr = repr(exc)
        raise
    finally:
        try:
            finish_cron_run(conn, run_id, status,
                            result if status == "ok" else None, err_repr)
        except Exception:
            pass    # observability must never mask the cron outcome
        try: conn.close()
        except Exception: pass
    return result
```

Phase B+ crons use `start_cron_run` / `finish_cron_run` (the bracket
pattern that distinguishes "Vercel didn't invoke" from "function
crashed mid-flight"). Legacy crons still use the one-shot
`record_cron_run` — `cron_daily_summary`, `cron_midnight_reset`,
`cron_weekly_weight_checkin`, `cron_health_monitor` will migrate in
Phase C.

### Idempotency stamps

Once-per-user actions stamp a timestamp column so re-runs no-op:

| Column | Action gated |
|---|---|
| `last_morning_sent_at` | Morning greeting (per local day) |
| `last_nudge_sent_at` | Evening zero-log nudge (per local day) |
| `lang_confirmed_at` | Onboarding language stamp |
| `weekly_checkin_sent_at` | Monday weight prompt |
| `admin_notified_at` | "New user joined" post to admin channel |
| `nudge_mid_flow_sent_at` | Mid-onboarding "one step away" kicker |
| `blocked_at` | Telegram refused us (auto-block) |
| `activation_step` | F-17 activation funnel state |

When you add a once-per-user action, add a stamp column too. Pattern:
nullable `TEXT` column, stamped with `_now_iso()` on success, gated
in the cohort query with `<col> IS NULL` or `<col> < NOW() - INTERVAL ...`.

### Telegram auto-block

```python
def _send_with_autoblock(conn, user_id: int, text: str) -> str:
    resp = send_message(user_id, text)
    if isinstance(resp, dict) and resp.get("ok") is False:
        if resp.get("error_code") in (400, 403):
            set_blocked(conn, user_id, True)
            return "blocked"
        return "failed"
    return "sent"
```

Every cron + backfill script uses this shape. Telegram's `blocked_at`
auto-clears when the user messages the bot again (handled in
`api/webhook.py` near line 468).

### Health monitor

`cron_health_monitor` fires **daily at 06:00 UTC = 09:00 Kyiv** and
posts to `ADMIN_NOTIFY_CHAT_ID`. It always posts — healthy days
included — so the daily fire is itself the liveness signal. If a day
passes without a report in the channel, the monitor itself is down.

Sections in the report:
- **CRONS** — per-cron `started → finished_ok / errored / lost`
  breakdown (Phase B bracket data)
- **NOTIFICATIONS SENT** — counters from each cron's `result_json`
- **ACTIVATION** — F-17 funnel rung progression
- **ACTIVITY** — signups, meals logged, first-meal conversions
- **ONBOARDING FUNNEL** — total stuck mid-onboarding + per-step counts
- **ALL CHECKS PASSED ✓** OR **🚨 ALERTS:** list

### Backfill scripts

All in `scripts/backfill_*.py`. Each:
- Calls `init_db(conn)` first (schema-version gate runs migrations if needed)
- Has a `--dry-run` flag
- Hardcodes target `user_id`s or selects by a stuck-state column
- Stamps an idempotency column so re-runs are no-ops
- Uses `_send_with_autoblock` semantics on Telegram 400/403
- Loads env from `.env` via `dotenv` — specifically
  `ADMIN_NOTIFY_CHAT_ID` must be present in `.env` or admin-channel
  notifications silently no-op (scar 5.6).

---

## 4. Scars — past bugs to NOT repeat

Curated list, dated, each with the fix commit hash. **When you hit a
new bug worth remembering, append an entry here in the same commit as
the fix.** Convention: one line per scar, then a "Fix" sub-bullet.

### 5.10 — Weekly weight check-in trapped typed meals + dead `/skip` (2026-05-28)

`cron_weekly_weight_checkin` sets `awaiting_input_type='weight'`
unprompted (the only cron that pushes an input state). Two bugs
from the same dispatcher branch in `api/webhook.py`:

1. The branch routed **every** non-slash text to
   `handle_weight_input`, which rejects non-numbers with
   `weight.not_a_number` **without clearing the state** — so every
   typed meal ("2 eggs") bounced in a "that's not a number" loop
   until the user sent a bare number or `/cancel`. Photo (guard at
   ~`webhook.py:525`) and voice (`handle_voice` never checks
   `weight`) already escaped; **text had no abandon path.**
2. The branch was gated `and not text.startswith("/")`, so the
   `/skip` the prompt advertises (`weight.checkin_prompt`) never
   reached `handle_weight_input` (which handles it) — it fell to the
   command dispatcher → "unknown command."

- **Fix:** restructure the weight branch — route
  `("/skip", "skip")` and `_parse_float`-able bare numbers to
  `handle_weight_input`; for any other **non-command** text, clear
  `awaiting_input_type` and fall through to `handle_text_entry`
  (the canonical typed-meal path); let real slash commands pass
  through unchanged. Mirror the photo guard in `handle_voice` too
  (clear a lingering `_TEXT_INPUT_STATES` flag before the meal
  fallthrough). Regression tests
  `test_weight_state_routes_skip_and_abandons_meals` +
  `test_voice_meal_clears_lingering_text_input_state`.
- **Commit:** TBD (this commit)
- **Lesson:** (1) Any cron-pushed `awaiting_input_type` needs an
  abandon path for ALL three input modes (text/photo/voice), or the
  unprompted state traps the user. (2) A `not text.startswith("/")`
  dispatcher guard silently kills any `/command` the prompt tells the
  user to type — if a prompt advertises `/skip`, the state's branch
  must route `/skip`, not exclude it. The same `/skip` gap still
  exists for the user-initiated states (water_target, target_weight,
  weekly_delta, barcode_*, timezone, health_*) — noted follow-up.

### 5.1 — F-17 morning cron dead for 54 hours (2026-05-24)

Lowercase `callable` builtin used as a type annotation
(`callable | None`) crashed the module at import time. Vercel returned
500 on every invocation; no `cron_runs` rows for 54 hours = no signal.

- **Fix:** `from __future__ import annotations` at top of all cron
  modules + `test_all_cron_modules_import_cleanly` regression test.
- **Commit:** `eaecca1`
- **Lesson:** A typo in a type annotation can kill a serverless function
  silently. Eager annotation evaluation is a footgun on Python 3.13.

### 5.2 — Whitelist drop double-posted 7 admin notifications (2026-05-27)

Added `admin_notified_at` column to schema but forgot to add it to
`_ALLOWED_PROFILE_FIELDS` (the `update_profile` whitelist). Backfill
script ran 3× before the stamp persisted → admin channel got
21 posts instead of 7.

- **Fix:** wire `admin_notified_at` through BOTH `PROFILE_COLUMNS` AND
  `_ALLOWED_PROFILE_FIELDS`. Regression test asserts both.
- **Commit:** `938461a`
- **Lesson:** Two parallel lists describing the same schema is a
  drift hazard. Always update both in the same commit.

### 5.3 — Synchronous webhook + slow OpenAI = "10 minutes" error (2026-05-27)

Webhook waited for `analyze_photo` (OpenAI vision, no timeout
configured → SDK default 600s) before returning 200 to Telegram.
Telegram retried at 60s. The retry hit the same handler, popped
`pending_photos` (already empty), emitted `errors.pending_expired`
("Минуло більше 10 хвилин...").

- **Fix:** (a) Respond 200 + flush BEFORE `process_update`.
  (b) All OpenAI clients use `timeout=45.0` (under Telegram's 60s
  threshold).
- **Commit:** `6c368ff`
- **Lesson:** Any synchronous webhook handler that calls external
  APIs needs an explicit timeout under the platform's webhook
  retry threshold. SDK defaults are wrong for serverless.

### 5.4 — `reset_onboarding` wrote stale Q1 step name (2026-05-27)

After Q1 was reordered (sex first instead of age), `reset_onboarding`
still wrote `awaiting_age`. User tapped "Start over" → step set to
`awaiting_age` → keyboard sent for sex → tapping male/female hit the
"already answered" toast (because the `onb:sex:` callback gates on
`step == 'awaiting_sex'`).

- **Fix:** one-line column-value change in `reset_onboarding`. Added
  source-grep test that asserts the function writes `awaiting_sex`.
- **Commit:** `4978600`
- **Lesson:** Step-name constants drift silently across reorders. A
  source-grep regression test is cheaper than waiting for users to hit it.

### 5.5 — Onboarding confirmation screen lost 39% of users (2026-05-27)

The "EN/UK?" language-confirmation step bounced 13 of 33 unfinished
users (39%). Auto-detect from Telegram's `language_code` was already
running; the screen was asking users to confirm what we already knew.

- **Fix:** removed the screen entirely. Auto-detect + drop straight
  to first question. Language can still be changed via `/profile →
  🌐 Language` (added in same commit).
- **Commit:** `98e9075`
- **Lesson:** Confirmation steps that ask the user to re-state what
  we already know are leaks, not safety nets.

### 5.6 — Backfill script silently no-op'd admin notifications (2026-05-27)

`scripts/backfill_finish_onboarding.py` finalized 7 stuck users but
didn't call `_notify_admin_new_user`. The admin channel never got
the notifications. Operator noticed days later. Root cause #2:
`ADMIN_NOTIFY_CHAT_ID` wasn't in the local `.env`, so even if the
script had called the helper, the helper would have silently
returned (it short-circuits on empty env var).

- **Fix:** (a) Wrote `scripts/backfill_admin_notifications.py` to
  post the missing 7. (b) Added `ADMIN_NOTIFY_CHAT_ID` to local `.env`.
  (c) Added `admin_notified_at` idempotency stamp.
- **Commit:** `b7b174b` + `938461a`
- **Lesson:** Backfill scripts that replicate runtime logic must
  replicate ALL of it, including admin-channel side effects. Operator
  `.env` files drift from prod env vars; document required vars in
  this file (see § 8).

### 5.7 — Vercel cron drops ~30–40% of hourly fires (ongoing, 2026-05-26+)

`cron_good_morning` fires hourly per `vercel.json` (`30 * * * *`) but
shows 12–18 of expected 24 fires in any given 24h window. Vercel
cron is documented as best-effort. Built the start/finish bracketing
to distinguish "Vercel never invoked" (low `started` count) from
"function crashed" (high `running_unfinished` count).

- **Fix:** Phase A+B of cron-run lifecycle bracketing. Phase C
  (migrate remaining 4 crons) pending.
- **Commits:** `be2c058` (helpers), `5bb5207` (Phase B migration of
  cron_good_morning + health-monitor breakdown), `c496a3b` (health
  monitor itself).
- **Lesson:** Vercel cron SLA is "best effort, no guarantees." Build
  observability that distinguishes "platform didn't invoke" from
  "code crashed." Don't trust a missing row to mean any specific cause.

### 5.9 — Late `from X import Y` shadowed a module-level name inside `do_POST` (2026-05-27)

`api/dashboard.py::do_POST` had a `from lib.database import
get_profile` inside the `if action == "request_recap":` branch
(added on 2026-04-25 as part of the share-recap feature). The
same name was already imported at module level. Python's scoping
rule: ANY assignment to a name inside a function makes that name
local for the ENTIRE function. So when the locale block earlier
in `do_POST` called `get_profile(_conn_for_locale, user_id)`, it
hit `UnboundLocalError: cannot access local variable 'get_profile'
where it is not associated with a value` — even though the
module-level import was sitting right there.

The locale block's broad `except Exception: locale = url_locale`
swallowed the UnboundLocalError silently. `url_locale` is `'en'`
for chat-list opens (the menu button URL has no `?lang=`) and
`'uk'` for in-chat opens (the URL is built per-user with
`?lang=<locale>`). Hence the in-chat/chat-list divergence —
which looked exactly like a menu-button-URL caching bug and
absorbed 6 wrong hotfixes today before a `print(traceback...)`
in the except clause revealed the real error.

- **Fix:** delete the redundant local import. The module-level
  one (line ~40 of `api/dashboard.py`) is the only one needed.
- **Commit:** TBD (this commit)
- **Lesson:** Don't repeat a `from X import Y` inside a function
  if `Y` is already imported at module level. Python's local-scope
  rule fires on ANY assignment in the function — including
  `from … import …` — regardless of WHERE in the function the
  assignment sits. Broad `except Exception: locale = fallback`
  clauses can hide this for months: the user just sees the
  fallback. Add a `traceback.format_exc()` inside any such except
  so a regression like this surfaces in runtime logs immediately.
  Regression-tested in `test_smoke.py`
  ::`test_dashboard_do_post_no_local_get_profile_import`.

### 5.8 — Dashboard XHR multipart/parse_qs mismatch (2026-05-27)

Phase 2 dashboard refactor (`6b73de1`) split the page into a fast
shell + a follow-up XHR for the data blob. The XHR was built with
`new FormData()`, which makes fetch send `multipart/form-data;
boundary=...`. But the POST handler at `api/dashboard.py` line 421
uses `urllib.parse.parse_qs(raw)`, which only understands
`application/x-www-form-urlencoded`. On a multipart body, parse_qs
returns garbage keys and `initData=""` → `_verify_init_data("")`
returns None → server replies 401 → JS shell shows "Couldn't load
the dashboard." Three downstream hotfixes (`0902824` URL-hash
fallback, `e07c15b` server-stamp [reverted by `57b4bbd`],
`b4d0a43` sessionStorage carry-through) all addressed
`findInitData()` rather than the body encoding — none of them
fixed the actual broken layer.

- **Fix:** replace `new FormData()` with a literal
  `'action=initial_data&initData=...&lang=...'` URL-encoded body +
  explicit `Content-Type: application/x-www-form-urlencoded` header.
  Pattern-identical to `fetchDay` and `request_recap` 200 lines
  below in the same file, and to the documented fix in
  `api/scan.py:297-301`.
- **Commit:** `5ea42bc`
- **Lesson:** When a serverless handler uses `parse_qs` to read the
  body, ALL clients (form-submit POST nav, fetch XHRs, anything
  else) MUST send `application/x-www-form-urlencoded`. `new
  FormData()` quietly produces multipart, which `parse_qs` can't
  decode. Either pick one body parser convention per handler and
  hold the line, or add a multipart fallback (`api/barcode.py`
  does this for file uploads). The codebase's choice is URL-encoded
  for all dashboard XHRs — the regression test in
  `tests/test_smoke.py::test_dashboard_initial_data_xhr_uses_urlencoded_not_formdata`
  enforces it.

---

## 5. Cron + flow quick reference

(Source of truth: `vercel.json`. This table mirrors it for fast lookup.)

| Cron | Schedule (UTC) | Purpose | Idempotency stamp |
|---|---|---|---|
| `cron_daily_summary` | `0 * * * *` (hourly :00) | Per-tz-hour cohort: AI evening summary (Branch 1) + zero-log nudge (Branch 2) | `daily_logs.summary_sent`, `last_nudge_sent_at` |
| `cron_good_morning` | `30 * * * *` (hourly :30) | Per-tz-hour 08:00 cohort: morning greeting + F-17 activation funnel (demo/d4/d7) | `last_morning_sent_at`, `activation_step` |
| `cron_midnight_reset` | `0 0 * * *` | Daily cleanup: stale photos/quotas, freeze refills, tz unstuck, F-17 auto-quiet sweep | various |
| `cron_weekly_weight_checkin` | `0 6 * * 1` (Mon 06:00) | Weight prompt + weekly recap PNG | `weekly_checkin_sent_at` |
| `cron_health_monitor` | `0 6 * * *` (06:00 UTC = 09:00 Kyiv) | Daily report to `ADMIN_NOTIFY_CHAT_ID` — health of all the above | (none — always posts) |

---

## 6. Where to look for X

| Question | File / function |
|---|---|
| Where do meals get saved? | `lib/database.py::save_meal` |
| How do I add a profile column? | 1. Add `ALTER TABLE` to `init_db` <br> 2. Bump `SCHEMA_VERSION` <br> 3. Add to `PROFILE_COLUMNS` list <br> 4. Add to `_ALLOWED_PROFILE_FIELDS` set |
| How do I add a new inline keyboard? | `lib/telegram_helpers.py` — keyboard factory functions, all locale-aware |
| How do I add an i18n string? | Add the key to BOTH `lib/i18n/dict_en.json` AND `lib/i18n/dict_uk.json`. Add a localization test that asserts both render. |
| Where do user-facing strings come from? | `lib/i18n/dict_*.json` (loaded lazily by `lib/i18n/__init__.py::_load`) |
| What does the admin panel show? | `api/admin_stats.py` (HTML report endpoint) |
| What does the dashboard show? | `api/dashboard.py` (Telegram Mini App, served as HTML) |
| Where's the meal-analysis pipeline? | `api/webhook.py::handle_meal_type_callback` → `analyze_photo` / `analyze_text` → `_send_analysis_preview` → `mod:accept` callback → `save_meal` |
| How do I add a new cron? | 1. Copy a recent `api/cron_*.py` as skeleton (use Phase B+ shape) <br> 2. Add to `vercel.json` crons array <br> 3. Add to `_check_cron_firing` specs in `api/cron_health_monitor.py` <br> 4. Add to `test_all_cron_modules_import_cleanly` |
| How do I add a backfill script? | `scripts/backfill_*.py` — see § 3 "Backfill scripts" for the contract |
| Where is the health-monitor section text built? | `api/cron_health_monitor.py::_build_report` |

---

## 7. Required `.env` keys

Local development scripts and one-shot backfills load `.env` via
`python-dotenv`. Keys that production has but local often misses:

| Key | Used by | Symptom if missing |
|---|---|---|
| `DATABASE_URL` | Everything | Connection refused |
| `TELEGRAM_BOT_TOKEN` | Everything | Telegram API 401 |
| `WEBHOOK_SECRET` | Webhook auth | 403 on Telegram updates |
| `CRON_SECRET` | Cron auth | 401 on curl probes |
| `OPENAI_API_KEY` | All AI features | 401 on OpenAI calls |
| `ADMIN_NOTIFY_CHAT_ID` | New-user notification + health monitor | Silent no-op (scar 5.6) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `/api/admin_stats` basic auth | Admin panel returns 401 |

Production Vercel env is the source of truth. Local `.env` should
mirror everything you need for the script you're running.

---

## 8. When in doubt

1. Read [`ARCHITECTURE.md`](./ARCHITECTURE.md) — structural reference,
   tables, components, env vars, module index.
2. `git log --oneline -20` — recent commits often answer "why is this
   like this?"
3. `grep -n <thing>` inside `lib/` or `api/` — usually faster than
   asking a clarifying question.
4. Still stuck → **ask the user** before any destructive change (DB
   writes, file deletions, force pushes). Reads are always safe.

---

## 9. Maintenance discipline for this file

- **No duplication with `ARCHITECTURE.md`.** Link, don't copy. If a
  section grows beyond its scope here, move it to ARCHITECTURE.md and
  link.
- **Every new scar requires the fix commit hash.** Forces real
  grounding. PRs that add a scar without a commit hash should be
  rejected.
- **Quarterly review:** scars older than 6 months with a guarding
  regression test get moved to a "historical" sub-section to keep the
  active list short.
- **Hard rules section is append-mostly.** Removing a rule means the
  underlying problem is resolved at a structural level (e.g. CI check
  enforces what the rule used to require). Document the removal in
  the commit message.

---

_Last updated: see `git log -- CLAUDE.md` for full history._
