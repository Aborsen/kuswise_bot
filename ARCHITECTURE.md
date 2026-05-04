# KusWise — Architecture

> A bilingual (UA / EN) Telegram nutrition coach. Users send a photo, voice note, text description, or barcode of what they ate; an AI returns calories + macros; the bot tracks daily progress, streaks, weight trajectory, allergens, chronic-condition flags, and 3-day meal plans. Mini Apps surface a richer dashboard, a barcode scanner, and an admin panel.

This document describes the runtime structure end-to-end: stack, components, data, flows, integrations, and security. It contains **no secrets** — only env-var names and their purposes.

---

## Table of contents

1. [What it is](#1-what-it-is)
2. [Top-level system map](#2-top-level-system-map)
3. [Tech stack](#3-tech-stack)
4. [Repositories](#4-repositories)
5. [Components](#5-components)
6. [Database (Postgres / Neon)](#6-database-postgres--neon)
7. [State machine](#7-state-machine)
8. [Meal-log lifecycle](#8-meal-log-lifecycle)
9. [Feature map (F-1 … F-12)](#9-feature-map)
10. [External integrations](#10-external-integrations)
11. [Localization (i18n)](#11-localization-i18n)
12. [Security posture](#12-security-posture)
13. [Deployment & infrastructure](#13-deployment--infrastructure)
14. [Environment variables](#14-environment-variables)
15. [Cron jobs](#15-cron-jobs)
16. [Operational scripts](#16-operational-scripts)
17. [Testing & CI](#17-testing--ci)
18. [Observability](#18-observability)
19. [Module index](#19-module-index)

---

## 1. What it is

KusWise is a Telegram-first nutrition coaching product:

- **Photo / voice / text / barcode → AI analysis → confirmed meal log → daily totals + macro adherence.**
- **Bilingual** out of the box (Ukrainian + English) at every layer — bot replies, dashboards, AI prompt outputs, slash-command menu.
- **Health-aware**: per-user allergen and chronic-condition profile (Crohn's, IBS, celiac, T1/T2 diabetes, hypertension, PCOS, kidney, thyroid, gestational) — the AI is told the user's context and flags problem ingredients per meal.
- **Cost-bounded**: per-user daily quotas on every AI surface (analysis, chat, menu OCR, meal plan, suggest, voice transcribe).
- **Stateless backend**: every request is a serverless function call; all state lives in Postgres (Neon) and Telegram's CDN (for photos).

Three "things" make up the product:

| Thing | What it is | Where it lives |
|---|---|---|
| **The bot** | The Telegram chat interface. Webhook + handlers + 3 cron jobs + an admin endpoint. | `kuswise_bot` repo, deployed as Python serverless functions on Vercel project `kuswise-bot`. |
| **The Mini Apps** | Three Telegram WebApps embedded in the chat: dashboard, barcode scanner, admin panel. Served from the same Python deployment. | `api/dashboard.py`, `api/scan.py` (+ POST helper `api/barcode.py`), `api/admin_stats.py`. |
| **The marketing site** | Public landing page at `kuswise.com`. | `KusWise` repo, Next.js on Vercel project `kuswise`. |

---

## 2. Top-level system map

```
                ┌────────────────────────────────────────┐
                │            Telegram clients             │
                │  (mobile apps, desktop, web)            │
                └────────────────┬────────────────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        │  Bot updates           │   Mini App initData      │
        │  (webhook POST)        │   (HMAC-signed)          │
        ▼                        ▼                          ▼
┌──────────────────┐    ┌────────────────┐     ┌─────────────────┐
│  /api/webhook    │    │ /api/dashboard │     │ /api/scan       │
│  Python handler  │    │  HTML + JSON   │     │  HTML + camera  │
│  3000+ lines     │    │  (Mini App)    │     │  (Mini App)     │
└────────┬─────────┘    └────────┬───────┘     └────────┬────────┘
         │                       │                       │
         │                       │              POST     │
         │                       │             /api/barcode
         │                       │                       │
         └──────────┬────────────┴───────────────────────┘
                    │
                    ▼
         ┌────────────────────────────┐
         │  Postgres on Neon           │   ← `DATABASE_URL`
         │  18 tables, idempotent      │
         │  init via init_db() per     │
         │  cold-start request         │
         └────────────────────────────┘
                    │
        ┌───────────┼───────────┬─────────────┐
        ▼           ▼           ▼             ▼
   OpenAI       Telegram    Open Food     Sentry
   API          Bot API     Facts         (optional)
   (GPT-4o,     (send,      (barcode →
    Whisper)    setMy*,     nutrition)
                getFile)

                    ▲
                    │ scheduled cron
        ┌───────────┴────────────┐
        │  Vercel Cron triggers   │
        │  3 endpoints:           │
        │  - cron_daily_summary   │
        │  - cron_midnight_reset  │
        │  - cron_weekly_weight…  │
        └────────────────────────┘

         ┌──────────────────────┐
         │  Marketing site       │   ← separate Vercel project
         │  Next.js 15 / TS 5    │   (kuswise.com)
         │  no DB, no auth       │
         └──────────────────────┘
```

Every box is a separate Vercel deployment slot; the only shared resource is the Postgres DB.

---

## 3. Tech stack

### Bot (`kuswise_bot`)

| Layer | Choice |
|---|---|
| Language / runtime | Python (CI tests on 3.11; Vercel runs the latest supported Python). Standard library `http.server.BaseHTTPRequestHandler` — no Flask / FastAPI / Django. |
| Vercel runtime adapter | `@vercel/python@4.3.0` |
| Database driver | `psycopg==3.3.3` + `psycopg-binary==3.3.3` (sync, autocommit=False per request) |
| Database engine | PostgreSQL on Neon (serverless, auto-scales, branched per Vercel preview) |
| AI SDK | `openai==1.109.1` |
| HTTP client | `httpx==0.28.1` (Telegram, Open Food Facts) |
| Image processing | `pillow==11.3.0` (recap PNG render, photo handling) |
| QR rendering | `qrcode==8.2` (recap card scan-to-bot link) |
| Validation | `pydantic==2.13.3` |
| Env loading | `python-dotenv==1.2.2` |
| Observability | `sentry-sdk==2.58.0` (optional — disabled when `SENTRY_DSN` empty) |
| Test runner | `pytest>=7.4`, `pytest-asyncio>=0.21` |

### Marketing site (`KusWise`)

| Layer | Choice |
|---|---|
| Framework | Next.js 15.0.0 (App Router) |
| Language | TypeScript 5.6.0, React 19.0.0 |
| Styling | Tailwind CSS 4.0.0 |
| Package manager | pnpm 9.12.0 |
| Vercel insights | `@vercel/analytics` + `@vercel/speed-insights` |
| Linter | ESLint 9.0.0 |

---

## 4. Repositories

| Repo | Location on disk | Vercel project | Purpose |
|---|---|---|---|
| Bot | `~/Desktop/kuswise_bot` | `kuswise-bot` (`prj_pLHIZFX5KZcj3eC2Ozw7H56foYxU`) | All bot logic, mini apps, admin, crons. |
| Marketing | `~/Desktop/KusWise` | `kuswise` (`prj_82MMCwss2hGDNzukoJqHSo1KQTdW`) | Static landing page. |

Both deploy automatically from `main` on push.

---

## 5. Components

### 5.1 Telegram bot webhook — `api/webhook.py`

The single biggest file in the system (~3000 lines). Responsibilities:

- **HTTP entry**: `BaseHTTPRequestHandler.do_POST()` validates Telegram's secret token (`X-Telegram-Bot-Api-Secret-Token`) via constant-time HMAC, caps body size at 256 KB, parses JSON.
- **Dispatch**: `process_update()` routes by update kind — `callback_query`, `message` (text / photo / voice), and message subkinds.
- **Onboarding gate**: any incomplete profile is funneled through `handle_onboarding_text()` until age / sex / weight / height / gym / goal are set.
- **State machine**: a single column `user_profiles.awaiting_input_type` gates inbound text and photos to the right specialised handler (see [section 7](#7-state-machine)).
- **Callback router**: 16+ callback prefixes (`onb:`, `meal_type:`, `mod:`, `pick:`, `barcode:`, `menu:`, `plan:`, `suggest:`, `meal_edit:`, `meal_del:`, `fav:`, `relog:`, `undo:`, `water:`, `prof:`, `tz:`, `h:`, `lang:`).
- **Quota enforcement**: every AI call goes through `_enforce_quota(conn, chat_id, user_id, action)` which atomically increments a `usage_quota` row keyed on `(user_id, action, day)`.
- **Per-user user gating**: optional `ALLOWED_USER_IDS` whitelist (empty = open).

### 5.2 Dashboard Mini App — `api/dashboard.py`

A per-user read-only dashboard rendered as server-side HTML + inline vanilla JS in a single ~1800-line file. Opened from a persistent Telegram menu button.

- **Auth**: Telegram WebApp `initData` HMAC-verified server-side via `lib/initdata.py:verify_init_data()`. Two-step request flow — `do_GET` returns a tiny bootstrap HTML that polls `Telegram.WebApp.initData` and form-POSTs back; `do_POST` verifies the HMAC, optionally dispatches a side-effecting action, then renders the full dashboard.
- **Tabs**: Overview / Meals / Profile, with a 7-day spinner pinned at the top (90-day backward scroll, lazy historical day fetch via `action=day_data`).
  - **Overview** — calorie ring, P/C/F + fiber + sugar progress bars, read-only water display (current ml / target + mini bar), 30-day calorie bar chart (color-coded vs goal: red <70 % or >130 %, amber 70–90 % or 110–130 %, green 90–110 %, low-opacity grey for "no meals" days), collapsible "Coach note" card (latest `daily_recommendations` row with its date in the title), text day-summary, share-recap button.
  - **Meals** — per-day meal list grouped by type. Meals with non-empty `allergen_warnings` or `crohn_warnings` get a `⚠️ N` chip; tap expands the full list.
  - **Profile** — anthropometrics, daily targets, all-time averages, adherence %, streak line (`🔥 N · 🏆 best M · ❄️ K freezes`, server-side rendered with Slavic plurals via `lib.i18n.plurals.pluralize`), goal projection (weeks-to-goal + projected date + on-track / ahead / behind status), 90-day weight-trend SVG (raw points + 7-day rolling average; hidden under an empty hint when n < 3).
- **Mutating actions**: today only `request_recap` (sends a PNG card to chat via `lib/recap.py`). Water logging and meal edits stay in the bot — the dashboard is read-only by design.
- **Pre-rendered dynamic strings**: anything plural-aware (streak line, freeze count) is built server-side and shipped through `data.streak_line` so JS never has to carry the Slavic 1 / 2-4 / 5+ rule.
- **CSP**: per-request nonce on the single inline `<script>`; `<style>` allowed via `'unsafe-inline'`.
- **Theming**: CSS custom properties bind to Telegram's `themeParams` (`--tg-theme-*`), auto-tracking dark / light mode.

### 5.3 Barcode scanner Mini App — `api/scan.py` + `api/barcode.py`

- `scan.py` serves a static HTML page that loads `html5-qrcode` from a CDN, requests camera permission (with iOS-aware fallback messaging), and POSTs the scanned EAN to `api/barcode.py`.
- `barcode.py` validates initData, looks up the EAN against Open Food Facts via `lib/off.py`, stages a pending analysis with the per-100g nutriments, and shows a portion-picker keyboard (100 g / 200 g / 300 g / custom) back in the chat.
- Falls back to manual EAN entry if the user can't scan.

### 5.4 Admin panel — `api/admin_stats.py`

Internal-only HTML dashboard. HTTP Basic Auth (`ADMIN_USERNAME` / `ADMIN_PASSWORD`, both compared in constant time). Shows user count, per-user meal totals, quota usage, recent activity. Hardened with strict CSP (per-request nonce, no inline JS), HSTS, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy` disabling camera/mic.

### 5.5 Cron endpoints

Four Vercel-Cron-triggered Python functions, all gated by `Authorization: Bearer ${CRON_SECRET}` (constant-time, fail-closed when secret unset). Schedules in [section 15](#15-cron-jobs).

### 5.6 Marketing site — separate repo

Next.js App Router, no DB, no auth, mostly static.

---

## 6. Database (Postgres / Neon)

- **Engine**: PostgreSQL via Neon's serverless adapter; connection through `DATABASE_URL` (auto-injected by Vercel's Neon integration).
- **Driver**: `psycopg==3.3.3` (sync, autocommit=False — every handler opens its own connection per request and closes it before responding).
- **Migrations**: there is no Alembic / Prisma — the schema is defined in `lib/database.py:init_db()` as `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE … ADD COLUMN IF NOT EXISTS` statements. `init_db()` runs on every cold-start request (cached after first call via `_SCHEMA_INITIALISED` module flag).
- **Foreign keys**: deliberately **not enforced at the DB layer**. Every `user_id` column is a logical FK to `users.user_id`; the application owns referential integrity. This avoids distributed-transaction issues on serverless cold starts.
- **Time conventions**:
  - `TEXT` columns named `*_at` / `created_at` / `updated_at` store **UTC ISO 8601** strings (`_now_iso()`).
  - `TEXT` columns named `date` / `day` store **Kyiv-local YYYY-MM-DD** (`_today_str()`) — so meals logged at 23:59 Kyiv don't fall on the wrong day after UTC midnight.
  - `TIMESTAMPTZ` columns (used in `water_logs`, `weight_history`) store proper UTC timestamps for chronological ordering within a day.

### 6.1 Schema reference (18 tables)

Permanent (long-lived business data):

| Table | Purpose | Key columns |
|---|---|---|
| `users` | Root record. | `user_id BIGINT PK`, `username TEXT`, `created_at` |
| `user_profiles` | Onboarding state + goals + i18n + tz + nudge prefs. | `user_id BIGINT PK`, `age`, `sex`, `weight_kg`, `height_cm`, `gym_per_week`, `goal`, `daily_calorie_target`, `recommended_calorie_target`, `target_weight_kg`, `weekly_delta_kg`, `tz` (DEFAULT `Europe/Kyiv`), `lang` (DEFAULT `en`), `lang_confirmed_at`, `onboarding_step`, `awaiting_input_type`, `weekly_checkin_sent_at`, `last_nudge_sent_at`, `nudge_optout INTEGER NOT NULL DEFAULT 0`, `created_at`, `updated_at` |
| `user_health_profile` | Allergens + chronic-condition tags injected into AI prompts. | `user_id BIGINT PK`, `allergens TEXT[]` (e.g. `{egg, gluten, crohns_unsafe}`), `conditions TEXT[]` (e.g. `{crohns}`), `notes TEXT`, `updated_at` |
| `meals` | Confirmed meal log. | `id BIGSERIAL PK`, `user_id`, `date` (Kyiv), `meal_type`, `description`, `ingredients TEXT` (JSON), `allergen_warnings`, `crohn_warnings`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `sugar_g`, `photo_file_id` (Telegram file ID), `ai_raw_response` (JSON of the AI analysis), `is_favorite INTEGER`, `created_at` |
| `daily_logs` | Per-user, per-day aggregate totals. | `user_id`, `date`, `total_calories`, `total_protein_g`, `total_carbs_g`, `total_fat_g`, `total_fiber_g`, `total_sugar_g`, `summary_sent INTEGER`. **UNIQUE(user_id, date)**. Always stays in sync with `meals` via `recalc_daily_log` (idempotent UPSERT) or `upsert_daily_log_from_meal` (incremental). |
| `daily_recommendations` | End-of-day AI summaries (cron-generated). | `user_id`, `date`, `recommendation TEXT`, `created_at` |
| `corrections` | Audit trail of every manual meal edit / recalc. | `user_id`, `source` (`manual_edit` / `recalc` / `pick`), `original_json`, `corrected_json`, `created_at`. Index on `(user_id, created_at DESC)`. Used by personalization (F-7). |
| `user_food_aliases` | Per-user EWMA portion learning. | `user_id`, `alias`, `normalized_name`, `default_grams`, `default_kcal`, `default_protein_g`, `default_fat_g`, `default_carbs_g`, `sample_count`, `updated_at`. **PK(user_id, alias)**. |
| `user_streaks` | Engagement streak + monthly freezes. | `user_id PK`, `current_streak`, `longest_streak`, `last_log_date`, `freeze_days_remaining` (resets to 3 monthly), `updated_at` |
| `weight_history` | All weight check-ins. | `id`, `user_id`, `weight_kg`, `source`, `recorded_at TIMESTAMPTZ`. Index on `(user_id, recorded_at DESC)`. |
| `water_logs` | Water-intake events. | `id`, `user_id`, `amount_ml`, `logged_at TIMESTAMPTZ`. Index on `(user_id, logged_at DESC)`. |
| `water_prefs` | User's daily water target. | `user_id PK`, `target_ml DEFAULT 2000`, `target_overridden INTEGER`, `updated_at` |
| `meal_plans` | 3-day plan templates. Pruned at 90 days. | `id`, `user_id`, `plan_json`, `created_at`. Index on `(user_id, created_at DESC)`. |

Transient (TTL-cleaned):

| Table | Purpose | TTL |
|---|---|---|
| `pending_photos` | Staging for an in-flight photo / text before meal-type pick. | 10 min |
| `pending_analyses` | An AI analysis awaiting user moderation (✅ / 🔄 / ✏️). Carries `meal_type`, `analysis_json`, `photo_file_id`, `text_description`, `raw_response`, `awaiting_manual`, `candidates_json` (F-6 alternates), `replaces_meal_id` (`/meals` edit replacement target). | 10 min |
| `chat_sessions` | Multi-turn `/ask` conversation buffer. | 60 min |
| `menu_ocr_results` | Cached menu-OCR result while user picks a dish. | 1 hour |
| `usage_quota` | Daily counter per `(user_id, action)`. PK `(user_id, action, day)`. | 7 days |

### 6.2 Schema-touching operations

- `init_db()` runs on cold start — adds any new columns idempotently.
- A cron job (`cron_midnight_reset.py`) prunes `usage_quota` weekly, `meal_plans` at 90 days, `menu_ocr_results` at 1 hour.
- Per-request inline cleanup: `cleanup_stale_pending`, `cleanup_stale_analyses`, `cleanup_stale_chat` are called opportunistically.

---

## 7. State machine

The `user_profiles.awaiting_input_type` column is the single source of truth for "what does the user's next message mean?". Values + handlers:

| State | Trigger | Handles next… | Routed to |
|---|---|---|---|
| `null` | default | photo / text / voice → meal logging flow | `handle_photo`, `handle_text_entry`, `handle_voice` |
| `weight` | profile edit | numeric weight input | `handle_weight_input` |
| `water_target` | `/water → custom` | numeric ml | `handle_water_target_input` |
| `target_weight` | `/goals → set target` | numeric kg | `handle_target_weight_input` |
| `weekly_delta` | `/goals → set rate` | numeric kg/week | `handle_weekly_delta_input` |
| `timezone` | `/timezone → other` | IANA tz string | `handle_timezone_input` |
| `health_allergens` | `/health → allergens` | comma-separated text | `handle_health_input` |
| `health_conditions` | `/health → conditions` | comma-separated text | `handle_health_input` |
| `barcode_grams` | barcode scanner | numeric grams | `handle_barcode_grams_input` |
| `barcode_manual` | barcode scanner fallback | digit string | `handle_barcode_manual_input` |
| `menu_photo` | `/menu` | photo → menu OCR | `handle_menu_photo` |
| `plan_pantry` | `/plan` | text / **voice / photo** → 3-day plan | `handle_plan_pantry_input` / `handle_plan_pantry_photo` |
| `fridge_ingredients` | `/suggest_meal → fridge` | text / **voice / photo** → recipe | `handle_fridge_input` / `handle_fridge_photo` |

Voice in `plan_pantry` / `fridge_ingredients` is transcribed via Whisper and re-routed to the text handler. Photo in those states triggers `extract_pantry_from_photo` (gpt-4o vision) then re-routes.

State is cleared by every handler on completion or `/cancel`.

---

## 8. Meal-log lifecycle

The end-to-end flow from "user took a photo of their lunch" to a row in `meals`:

```
1. User sends photo (or text, or voice → Whisper transcript, or barcode scan).

2. Bot saves the file_id (or text) to pending_photos and asks meal type.
   ─────────────────────────────────────────────────────────────────────
   pending_photos row created. State: not yet AI-analyzed.

3. User taps meal_type:breakfast (or lunch / dinner / snack).

4. Bot fetches photo bytes, calls analyze_photo() / analyze_text().
   ─────────────────────────────────────────────────────────────────────
   System prompt includes:
     - Base nutrition prompt (lib/config.py:analysis_system_prompt)
     - Health addendum (allergens / conditions, only if user has any)
     - Personalization addendum (top-N learned aliases for this user)
     - Language directive ("Respond in Ukrainian" / "Respond in English")
   AI returns structured JSON: dish_name, ingredients[], nutrition{},
   glycemic_index, allergen_flags (only if user has allergens),
   crohn_flags (only if user has crohns).

5. Bot stages a pending_analyses row + decides: ambiguous or not?

   5a. Ambiguous (top guess confidence < 0.85, ≥2 candidates):
       Show alternates picker ("Was it Caesar salad or Greek salad?").
       User taps a candidate → re-run preview with picked analysis.

   5b. Unambiguous: show moderation preview.
       Buttons: ✅ Accept · 🔄 Recalculate · ✏️ Enter manually

6. User confirms.
   ─────────────────────────────────────────────────────────────────────
   - save_meal() inserts a row into `meals`.
   - upsert_daily_log_from_meal() bumps `daily_logs` totals.
   - update_streak_for_meal() increments engagement streak.
   - upsert_alias_from_meal() does an EWMA update on the user's
     "usual" portion for this dish (F-7).
   - If pending had `replaces_meal_id` (= came from /meals → ✏️ Edit),
     delete the old meal and recalc that date's daily_log.
   - record_correction() if recalc / manual override altered the result.
   - format_meal_logged() sends the minimal 3-line confirmation:
        ✅ Saved: {dish}
        🕐 {meal_type}
        📊 Day total: {cal} / {target} kcal
```

Modify path (`mod:manual` or `/meals → ✏️ Edit`) is symmetric:

- The prior analysis is re-sent to the AI as `previous_analysis` context inside a *modification prompt* (`_build_modification_prompt` in `lib/openai_vision.py`) — telling the model: "patch this, don't replace it".
- `replaces_meal_id` rides through `pending_analyses` so the confirm step deletes the original after the new one is inserted (atomic-feeling swap; cancel-mid-edit leaves the original intact).

---

## 9. Feature map

Each "F-N" tag corresponds to a feature pillar referenced throughout the codebase.

| Tag | Feature | Surface | Key files |
|---|---|---|---|
| F-1 | Health profile (allergens + conditions) | `/health` | `lib/health.py`, `user_health_profile` table |
| F-2b | Bilingual UI + language confirm + per-chat command menu | every message, `/language`, F-2b onboarding step | `lib/i18n/`, `scripts/setup_bot_commands.py`, `tests/test_i18n_snapshots.py` |
| F-4 | Engagement streak + monthly freezes | `/streak`, post-save bump | `user_streaks`, `lib/database.py:update_streak_for_meal` |
| F-5 | Weight projection + weekly delta | `/goals`, `/profile` | `lib/goals.py`, `weight_history` |
| F-6 | Ambiguous-photo alternates picker | inline keyboard | `lib/openai_vision.py:normalize_candidates / is_ambiguous`, `pending_analyses.candidates_json` |
| F-7 | Personalization (EWMA portion learning) | every analysis | `lib/personalization.py`, `user_food_aliases`, `corrections` |
| F-8 | Barcode scanner (Open Food Facts) | `/scan` Mini App | `api/scan.py`, `api/barcode.py`, `lib/off.py` |
| F-9 | Restaurant menu OCR | `/menu` | `lib/openai_vision.py:analyze_menu`, `menu_ocr_results` |
| F-10 | 3-day meal plan with pantry | `/plan`, `plan_pantry` state | `lib/mealplan.py`, `meal_plans` |
| F-11 | Single-meal recipe suggestion | `/suggest_meal`, `fridge_ingredients` state | `lib/openai_nutrition.py:suggest_meal`, `extract_pantry_from_photo` |
| F-12 | Weekly recap PNG card | `/recap` (chat) + share-recap button (Mini App) | `lib/recap.py` (PIL + QR) |
| F-13 | Unified daily re-engagement nudge + `/quiet` opt-out | piggybacks on the hourly `cron_daily_summary` — branch (2) sends one warm `nudge.daily_zero` reminder per user-local day, fired at the user's local 22:00 (`user_profiles.tz`). Covers recent-lapsed, long-dormant, and never-loggers uniformly. Per-day dedup via `last_nudge_sent_at` (now compared in user-tz date space). The only opt-out gates are explicit `/quiet` (`nudge_optout=1`) and the auto-opt-out on TG 400/403 | `api/cron_daily_summary.py`, `user_profiles.tz / nudge_optout / last_nudge_sent_at`, `lib/database.py:get_users_to_nudge / mark_nudge_sent / set_nudge_optout` |
|  — | `/ask` (free-form Q&A) | `/ask` | `lib/openai_chat.py`, `chat_sessions` |
|  — | Water tracking | bot only — `/water` (quick-add buttons) and reply-keyboard `+250мл`. Dashboard shows a read-only display. | `water_logs`, `water_prefs` |

### Major UX side-features

- **Favorites** — every saved meal can be ⭐ favorited; `/fav` lists them; tap relog to clone into today.
- **Recent meals** — `/recent` lists deduplicated descriptions; tap to relog.
- **Meal management** — `/meals` lists today's entries with delete / edit buttons.
- **Edit-from-list** — re-runs the AI in modification mode, preserves the original until the user confirms a replacement, atomically swaps via `replaces_meal_id`.

---

## 10. External integrations

| Service | Calls made | Auth | Where |
|---|---|---|---|
| **Telegram Bot API** | `sendMessage`, `sendPhoto`, `editMessageText`, `answerCallbackQuery`, `setMyCommands` (per-language + per-chat scope), `setChatMenuButton`, `setWebhook`, `getFile` | bot token | `lib/telegram_helpers.py` |
| **OpenAI** | `chat.completions.create` (GPT-4o for vision + text analysis, daily summary, recipe, menu OCR, chat); `audio.transcriptions.create` (Whisper for voice notes) | `OPENAI_API_KEY` | `lib/openai_vision.py`, `lib/openai_voice.py`, `lib/openai_nutrition.py`, `lib/openai_chat.py` |
| **Open Food Facts** | `GET /api/v2/product/{ean}.json` | none (public) | `lib/off.py` |
| **Neon Postgres** | psycopg connection | `DATABASE_URL` | `lib/database.py` |
| **Sentry** *(optional)* | error / performance ingestion | `SENTRY_DSN` | `lib/log.py` |
| **Vercel Cron** | inbound `Authorization: Bearer …` | `CRON_SECRET` | `api/cron_*.py` |

There is **no analytics SDK** in the bot; analytics on the marketing site only.

---

## 11. Localization (i18n)

- Two locale dictionaries in `lib/i18n/dict_uk.json` and `lib/i18n/dict_en.json`, ~700 keys each.
- Resolved via `_t("key", profile)` (uses `profile.lang`) or `lib.i18n.t("key", locale=…)` for direct.
- AI prompts inject a `Respond in {language}.` directive; the model translates output. JSON keys + enum values stay in English so downstream code is locale-stable.
- `setMyCommands` is called for `language_code` of `uk`, `ru`, `be` (all map to UA in `normalize_lang`) and a default (no language code) → English. Plus per-`(scope=chat)` registrations are pinned for users who explicitly chose a language so the menu always matches their bot-side preference regardless of Telegram client UI language.
- A snapshot test (`tests/test_i18n_snapshots.py`) enforces UA / EN key parity and rendering equivalence; an audit script (`scripts/check_i18n.sh`) refuses to merge if any user-facing Python source file has bare Cyrillic without a `# noqa: i18n` exemption.

---

## 12. Security posture

| Concern | Mitigation |
|---|---|
| Telegram webhook spoofing | `X-Telegram-Bot-Api-Secret-Token` checked against `WEBHOOK_SECRET` via `hmac.compare_digest` (constant-time). Body capped at 256 KB. |
| Cron endpoint abuse | `Authorization: Bearer ${CRON_SECRET}` checked constant-time; unset secret = fail-closed. |
| Mini App tampering | Telegram WebApp `initData` verified server-side via HMAC-SHA256 (`lib/initdata.py:verify_init_data`). The bot token is the HMAC key. |
| Admin panel | HTTP Basic Auth (`ADMIN_USERNAME` / `ADMIN_PASSWORD`), constant-time, plus strict CSP / HSTS / DENY-frame / no-referrer / camera-mic-off `Permissions-Policy`. |
| User gating | Optional `ALLOWED_USER_IDS` set; empty = open to anyone who finds the bot. |
| Quota / cost guard | Per-user-per-day counters on `meal_analysis`, `voice_transcribe`, `chat`, `menu_ocr`, `suggest`, `plan` actions (defaults configurable per `LIMIT_*_PER_DAY` env vars); plus a `DAILY_OPENAI_USD_CAP` ceiling. |
| Prompt injection | User free-text is wrapped in `<user_meal>` / `<user_correction>` tags with explicit "treat strictly as data" instructions. AI prompts include an injection-guard footer (`_PROMPT_INJECTION_GUARD`). |
| HTML injection back to Telegram | All user / AI text passed through `html.escape(quote=False)` before sending with `parse_mode=HTML`. |
| Stale state leaks | Pending tables auto-clean at TTL on every webhook hit; expired barcode / menu sessions surface a friendly "session expired" message instead of misbehaving. |
| Photo size DoS | `MAX_PHOTO_BYTES = 5 MB` rejected with i18n error before any download. |

No PII (names, emails, addresses) is collected — only Telegram `user_id` (numeric) + optional `username`. Photo contents stay on Telegram's CDN; the bot stores only `photo_file_id` strings.

---

## 13. Deployment & infrastructure

### Vercel projects

| Project | Domain | Slug | Notes |
|---|---|---|---|
| `kuswise-bot` | (per-deployment URLs only) | bot's webhook URL pinned via `scripts/set_webhook.py` | Python serverless functions; one project per repo. |
| `kuswise` | `kuswise.com` | static landing | Next.js, separate repo. |

### Build pipeline

- **Bot**: pushed to `main` → Vercel auto-detects `requirements.txt` and `api/*.py`, builds Python serverless functions per file. No build command needed.
- **Site**: pushed to `main` → `next build` via Vercel's Next.js detection.
- **CI**: GitHub Actions (`.github/workflows/test.yml`) runs `pytest -v tests/` on Python 3.11 against every push and PR. Tests must pass before any merge to main.

### Function metadata

- Runtime: Python (latest supported by `@vercel/python@4.3.0` — currently 3.13).
- Default execution timeout per function: 300 s (Vercel platform default).
- Region: Vercel default (no region pinning configured).
- Cold-start strategy: each request runs `init_db()` first; idempotent `IF NOT EXISTS` ALTERs make schema migrations zero-downtime — no separate migration step.

### Database

- Provider: Neon (serverless Postgres, auto-scaling, branched per Vercel preview deployment).
- Connection string injected via Vercel's Neon integration as `DATABASE_URL` (`POSTGRES_URL` is honored as fallback).
- Connection pooling: per-request `psycopg.connect()` + close; Neon handles its own pooling on the server side.

---

## 14. Environment variables

> All values are configured in Vercel Project → Settings → Environment Variables. Never commit them. The list below is **names + purposes only**.

### Required at runtime

| Var | Purpose | Source |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot identity. Used for every Telegram API call and as the HMAC key for Mini App initData verification. | BotFather. |
| `WEBHOOK_SECRET` | Secret token Telegram echoes in `X-Telegram-Bot-Api-Secret-Token` for every webhook delivery. | Operator-chosen, registered via `scripts/set_webhook.py`. |
| `OPENAI_API_KEY` | OpenAI authentication for GPT-4o + Whisper. | OpenAI dashboard. |
| `DATABASE_URL` (or `POSTGRES_URL` fallback) | Neon Postgres connection string. | Auto-injected by Vercel ↔ Neon integration. |
| `CRON_SECRET` | Bearer token Vercel Cron sends in `Authorization` header for all 3 cron endpoints. | Vercel Cron config. |

### Admin panel

| Var | Purpose |
|---|---|
| `ADMIN_USERNAME` | HTTP Basic Auth username for `/api/admin_stats`. |
| `ADMIN_PASSWORD` | HTTP Basic Auth password (constant-time-compared). |

### Cost / quota controls (all optional, sensible defaults exist)

| Var | Purpose |
|---|---|
| `LIMIT_PHOTO_PER_DAY` | Photo / text / voice meal-analysis cap per user (default ~50). |
| `LIMIT_ASK_PER_DAY` | `/ask` chat cap (default ~20). |
| `LIMIT_OCR_PER_DAY` | `/menu` OCR cap (default ~5). |
| `LIMIT_PLAN_PER_DAY` | `/plan` cap (default ~3). |
| `LIMIT_TOTAL_AI_PER_DAY` | Hard ceiling across all AI surfaces (default ~100). |
| `DAILY_OPENAI_USD_CAP` | Per-user dollar cap; 0 disables the check. |

### Observability (optional)

| Var | Purpose |
|---|---|
| `SENTRY_DSN` | Empty = disabled. Otherwise Sentry SDK initializes per service. |
| `SENTRY_TRACES_SAMPLE_RATE` | Performance trace sampling (default 0.1). |

### Vercel auto-injected

`VERCEL_URL`, `VERCEL_PROJECT_PRODUCTION_URL`, `VERCEL_ENV`, `VERCEL_GIT_COMMIT_SHA` — used by `scripts/set_webhook.py` to point Telegram at the right deployment URL and to surface deploy-SHA in Sentry tags.

### Site (`kuswise.com`)

| Var | Purpose |
|---|---|
| `NEXT_PUBLIC_SITE_URL` | Canonical domain for OG meta + sitemap (default `https://kuswise.com`). |

---

## 15. Cron jobs

Configured in `kuswise_bot/vercel.json`:

| Path | Cron | Purpose |
|---|---|---|
| `/api/cron_daily_summary` | `0 * * * *` (hourly at :00 UTC) | Per invocation, processes only users whose local clock is in the 22:00 hour (`EXTRACT(HOUR FROM NOW() AT TIME ZONE up.tz) = 22`); each user matches one UTC fire per day. Branch (1): meals on user-local today → OpenAI end-of-day coaching message (`SUMMARY_PROMPT_TEMPLATE`), persisted in `daily_recommendations`, gated by `daily_logs.summary_sent`. Branch (2): zero meals on user-local today → one warm `nudge.daily_zero` reminder, gated by `last_nudge_sent_at < start_of_user_local_today` so each user gets at most one nudge per local day. Carries the inline 🔕 opt-out button. On Telegram `error_code in (400, 403)` for any send, auto-opts the user out (sets `nudge_optout=1`) to stop retrying blocked / gone chats. Per-send 40 ms pacing. `/quiet` toggles `nudge_optout`. |
| `/api/cron_midnight_reset` | `0 0 * * *` (00:00 UTC daily) | Prunes `usage_quota` (>7 days), `meal_plans` (>90 days), `menu_ocr_results` (>1 hour). Bumps `user_streaks.freeze_days_remaining` back to 3 on the 1st of each month. |
| `/api/cron_weekly_weight_checkin` | `0 6 * * 1` (Monday 06:00 UTC) | DMs onboarded users a weight-entry prompt. Updates `user_profiles.weekly_checkin_sent_at`. |

All three verify `CRON_SECRET` and fail-closed when it's unset.

---

## 16. Operational scripts

Under `scripts/`:

| Script | When to run |
|---|---|
| `set_webhook.py` | After first deploy or when `WEBHOOK_SECRET` rotates. Calls Telegram `setWebhook` + delegates to `setup_bot_commands.main()`. |
| `setup_bot_commands.py` | When commands are added / removed / re-translated. Registers commands across `(scope × language)` matrix: EN at default + private; UA at default + private under `language_code` of `uk`, `ru`, `be`. Idempotent. |
| `backfill_chat_commands.py` | One-shot. Walks `user_profiles` and pins a chat-scope command menu per user in their stored language. Run once after F-2b roll-out so existing users don't see English menus when their Telegram client UI is `pl` / `cs` / etc. |
| `migrate_food_user.py` | One-shot. Migrates a single user's data from a legacy "Food" bot into KusWise — meals + health profile (canonical IDs + audit `notes`); preserves the existing KusWise profile. Idempotent (dedups meals on `(user_id, date, meal_type, description, calories)`). |
| `stats.py` | Ad-hoc DB usage statistics. |
| `check_i18n.sh` | CI gate. Greps for Cyrillic in user-facing Python files; passes only when every match is allow-listed via `# noqa: i18n`. |

All scripts read from `.env` / `.env.local` via `python-dotenv` and fail loudly if required env vars are missing.

---

## 17. Testing & CI

- **Test runner**: `pytest`.
- **Suite location**: `tests/` (~165 tests across `test_smoke.py` + `test_i18n_snapshots.py`).
- **Key tests**:
  - `tests/test_smoke.py` — analysis JSON parsing, alternates picker logic, formula calculations (macros from weight × goal), locale routing, callback data parsing, pending-state cleanup, quota enforcement.
  - `tests/test_i18n_snapshots.py` — UA / EN dict key parity, formatter snapshots in both locales, plural-form rules (Slavic 1 / 2-4 / 5+ vs English 1 / many), date-formatting locale, `dash.adherence` & friends, audit-script exit-zero gate.
- **CI**: GitHub Actions runs `pytest -v tests/` on every push to main and every PR.
- **Pre-deploy gate**: tests must pass before Vercel auto-deploys on main.

---

## 18. Observability

- **Sentry** (optional): `lib/log.py:setup_sentry(service_name)` is called at the top of every API handler. Errors are captured with the user_id tag attached when available. Performance traces are sampled at 10% by default.
- **Print logging**: `print(..., flush=True)` for cold-path debug; visible via `vercel logs` and Vercel's deployment log viewer.
- **Errors**: `lib.log.error("event_name", exc=e, **context)` is the canonical "this is unexpected, surface it" call. It writes to both stdout and Sentry.
- **No analytics in the bot.** Marketing site uses `@vercel/analytics` + `@vercel/speed-insights`.

---

## 19. Module index

### `api/` (entry points)

| File | Role |
|---|---|
| `webhook.py` | Telegram update handler. The system's beating heart. |
| `dashboard.py` | Mini App: per-user dashboard (HTML + JSON). |
| `scan.py` | Mini App: barcode scanner page. |
| `barcode.py` | POST endpoint for barcode lookups (Open Food Facts). |
| `admin_stats.py` | Internal admin dashboard (HTTP Basic Auth). |
| `cron_daily_summary.py` | Hourly cron firing at user-local 22:00 (`up.tz`). Branch (1) AI coaching summary for users with meals on their local today; branch (2) one warm `nudge.daily_zero` for users with zero meals on their local today, deduped per-user-local-day via `last_nudge_sent_at`. Auto-opt-out on Telegram 400/403. |
| `cron_midnight_reset.py` | Daily janitorial cron (quota / plan / cache cleanup). |
| `cron_weekly_weight_checkin.py` | Weekly weight-prompt cron. |

### `lib/` (shared modules)

**Data:**
- `database.py` — schema + every CRUD helper.

**Config & locale:**
- `config.py` — env loading, macro formulas, AI prompt templates.
- `i18n/__init__.py` — `t(key, locale)` resolver + `normalize_lang()`.
- `i18n/plurals.py` — Slavic 1 / 2-4 / 5+ rule + EN 1 / many; `pluralize()` and `pluralize_with_count()`. Used wherever a countable noun crosses i18n boundaries (streak line, recap card, etc.).
- `i18n/dict_uk.json`, `i18n/dict_en.json` — locale dictionaries (~700 keys each, parity-tested).
- `bot_commands.py` — slash-menu command list per locale.

**AI surfaces:**
- `openai_vision.py` — `analyze_photo`, `analyze_text`, `analyze_menu`, `extract_pantry_from_photo`, F-6 candidate handling.
- `openai_voice.py` — Whisper wrapper.
- `openai_nutrition.py` — `suggest_meal` (recipe), `generate_daily_summary` (cron).
- `openai_chat.py` — `/ask` multi-turn chat.

**Domain logic:**
- `health.py` — F-1 allergens + conditions registry, prompt addendum.
- `personalization.py` — F-7 EWMA portion learning, alias prompt block.
- `goals.py` — F-5 weight projection + weekly delta.
- `mealplan.py` — F-10 3-day plan generator.
- `recap.py` — F-12 PNG recap card.
- `off.py` — F-8 Open Food Facts client.

**Telegram glue:**
- `telegram_helpers.py` — sendMessage / keyboards / setMyCommands / setChatMenuButton wrappers.
- `initdata.py` — Mini App initData HMAC verification.

**Output:**
- `formatters.py` — every text-rendering function (preview, logged, today, history, profile, plans, recap captions, …).

**Plumbing:**
- `log.py` — Sentry + structured logging.
- `datehelpers.py` — Kyiv-local date arithmetic, IANA tz validation.
- `rate_limit.py` — `consume_quota`.

### `scripts/` — see [section 16](#16-operational-scripts).

### `tests/` — see [section 17](#17-testing--ci).

---

## Glossary

- **F-N tags** — internal feature pillars (F-1 through F-12). Used in code comments + commit history to group related work. See [Feature map](#9-feature-map).
- **Pending analysis** — a row in `pending_analyses` representing an AI result the user hasn't yet confirmed. Carries enough context to re-run, modify, or accept.
- **Health addendum** — string injected into the AI system prompt listing the user's allergens / conditions and extending the JSON schema with `allergen_flags` / `crohn_flags`. Empty for users without a health profile (the AI then doesn't generate those fields, saving tokens).
- **Personalization addendum** — top-N EWMA-learned aliases prepended as few-shot anchors, so the AI matches the user's actual habitual portions for known dishes.
- **Replaces-meal-id** — the meal id a pending analysis is replacing (set by `/meals → ✏️ Edit`). On confirm, the new meal is inserted and the old one is deleted in the same handler turn.

---

## Last updated

This document was last refreshed 2026-05-04 — F-13 moved to per-user-local 22:00 timing: `cron_daily_summary` now runs hourly and uses `EXTRACT(HOUR FROM NOW() AT TIME ZONE up.tz)` to fire each user's branch at their local 22:00 instead of a bot-wide 20:00 UTC. `last_nudge_sent_at` re-introduced as the per-user-local-day dedup gate. Earlier same-day refresh unified the two-tier nudge into a single `nudge.daily_zero`. Schema unchanged. Authoritative source is always the code; prefer file:line references over this document when they disagree.
