# KusWise Bot — Bilingual Telegram Nutrition Coach

A bilingual (UA / EN) Telegram bot that logs meals from **photos, text, voice, or barcode** and gives personalized calorie / macro / micronutrient coaching with allergen and chronic-condition awareness. Deployed on **Vercel** (Python serverless) with **Neon Postgres**. Uses **GPT-4o** for vision + text analysis, **Whisper** for voice transcription, and **GPT-4o-mini** for chat.

---

## Features

### Meal logging
- 📸 **Photo analysis** — GPT-4o vision estimates portion, calories, macros, glycemic index, and ingredient breakdown. Output in Ukrainian with a light joke.
- 📝 **Text entry** — free-form: "курка 200г, рис 150г, броколі 100г".
- 🎙 **Voice messages** — OpenAI Whisper transcribes UA speech (biased toward Ukrainian dish names), then runs the standard analysis pipeline. ~$0.001 per message.
- 🔄 **Moderation** — after each analysis: ✅ Прийняти / 🔄 Перерахувати / ✏️ Ввести вручну / ❌ Скасувати.
- 💡 **Glycemic index** shown on every logged meal and in chat/suggestion replies.

### Profile-driven targets
- 🚀 **Onboarding** — 6 questions (age, sex, weight, height, gym frequency, goal) → Mifflin-St Jeor BMR × activity multiplier × goal adjustment.
- 🎯 **Calorie target** — user accepts recommended value or enters a custom one.
- 💧 **Water target** — auto-estimated from body weight (30 ml/kg, rounded to 50 ml, clamped 1.5–4 L). Rewrites on weight change only if user hasn't manually overridden.

### Daily habit
- ⭐ **Favorites + Recent re-log** — `/fav` and `/recent` for one-tap meal repeat. Meal type auto-picked by current hour (06–11 breakfast, 11–16 lunch, 16–21 dinner, else snack). 10-min undo.
- 💧 **Water tracker** — `/water` bar + quick-add buttons (+200 / +250 / +300 / +500 / +750), undo-last, target picker (1.5 / 2.0 / 2.5 / 3.0 L).
- 📊 **Day & yesterday** — `/today` / `/yesterday` with progress bars, macros, calorie ring, smart end-of-day tips.
- 🤖 **AI chat** (`/ask`) — answers food/training-nutrition questions with today's intake + profile as context (GPT-4o-mini, 10-turn memory).
- 🍽️ **Meal suggestion** (`/suggest_meal`) — AI recipe sized to close the remaining macros/calories for today.

### Mini App dashboard
- Opens via the bot's chat-menu button. Telegram-signed auth (HMAC-SHA256 on `initData`).
- 3-tab layout (Overview / Meals / Profile) with a 7-day spinner at the top (90-day backward scroll, lazy historical fetch).
- **Overview**: calorie ring, P/C/F + fiber + sugar progress bars, read-only water display, 30-day calorie bar chart (color-coded vs goal), end-of-day "Coach note" card showing the latest AI summary with its date, share-recap button (sends a PNG recap card to chat).
- **Meals**: per-day meal list grouped by meal type. Meals carrying allergen / Crohn warnings show a `⚠️` chip — tap to expand the full warning list.
- **Profile**: anthropometrics, daily targets, all-time averages, adherence %, streak line (`🔥 N · 🏆 best M · ❄️ K freezes`, with proper UA Slavic plurals), goal projection (weeks-to-goal + projected date + on-track/ahead/behind), and a 90-day weight trend chart (raw points + 7-day rolling average; hidden under "log 3+ check-ins" if not enough data).
- Water and meal logging stay in the bot — the dashboard is read-only by design (only the recap-share action mutates).
- Bilingual UA / EN, theme-binds to Telegram's current dark/light scheme.

### Admin panel (`/api/admin_stats`)
- HTTP Basic Auth: username `ADMIN_USERNAME`, password `ADMIN_PASSWORD` (both set in Vercel env). The previous "any username + CRON_SECRET" path was removed so a leaked cron secret can't unlock the admin UI.
- Stat cards: total users, active today, active this week, meals, days, nightly summaries.
- Onboarding funnel with conversion %.
- Top-20 foods with bar chart.
- Full users table: age, sex, weight, height, gym freq, goal, calorie target, adherence % (days within ±15 % of target).
- Full meals history with search, user/type/date filters, CSV export, bulk delete, single delete.
- User detail modal (click any cell) + one-click user deletion (wipes all related rows).
- Auto-refresh every 60 s.

### Reply keyboard (persistent)
```
[🤖 Запитати ШІ]  [⭐ Улюблені]
[💧 +250мл]       [📊 День]
[🍽 Ідея страви]  [⚙️ Профіль]
```

### Automation (4 Vercel Cron jobs)
- 🌙 **Daily summary** — `0 20 * * *` UTC. Sends the GPT-4o end-of-day coaching message to every user with `summary_sent=0` + meals today; persists to `daily_recommendations`.
- 🔄 **Midnight cleanup** — `0 0 * * *` UTC. Prunes `usage_quota` (>7 days), `meal_plans` (>90 days), `menu_ocr_results` (>1 hour); resets `freeze_days_remaining` to 3 on the 1st of each month.
- ⚖️ **Weekly weight check-in** — `0 6 * * 1` UTC (Monday 06:00). DMs onboarded users a weight-prompt; updates `user_profiles.weekly_checkin_sent_at`.
- 👋 **Inactivity nudge** — `0 17 * * *` UTC. DMs fully-onboarded, opted-in users (`onboarding_step='done'` AND `nudge_optout=0`) who haven't logged a meal in 24h, with a 7-day cooldown. Auto-opt-out on Telegram 400 / 403 (blocked bot / chat gone). Toggleable from chat via `/quiet`.

---

## Prerequisites

1. **Telegram bot** — create with [@BotFather](https://t.me/BotFather), copy the token.
2. **OpenAI API key** — [platform.openai.com/api-keys](https://platform.openai.com/api-keys). Needs GPT-4o access (vision + Whisper are on the same key).
3. **Vercel account** — free tier. Neon Postgres is installed from the Vercel Marketplace.
4. **GitHub account** — for auto-deploy via Vercel's GitHub integration.
5. **Python 3.11+** locally (only for running `scripts/set_webhook.py`).

---

## Setup

### 1. Clone & install local deps

```bash
git clone https://github.com/Aborsen/kuswise_bot.git
cd kuswise_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Provision Neon Postgres from the Vercel Marketplace

From the Vercel dashboard after the first deploy:

1. Project → **Storage** tab → **Create Database** → **Neon**.
2. Accept the Marketplace terms. Free plan, default region.
3. Vercel auto-injects `DATABASE_URL` (plus aliases) into all environments.

For local `.env`, copy `DATABASE_URL` from **Settings → Environment Variables**.

No manual migrations — the bot runs idempotent `CREATE TABLE IF NOT EXISTS` on every request.

### 3. Generate secrets

```bash
python -c "import secrets; print('WEBHOOK_SECRET=' + secrets.token_urlsafe(32))"
python -c "import secrets; print('CRON_SECRET='    + secrets.token_urlsafe(32))"
python -c "import secrets; print('ADMIN_PASSWORD=' + secrets.token_urlsafe(32))"
# ADMIN_USERNAME can be any string you'll remember (e.g. 'admin').
```

### 4. Push to GitHub

```bash
git add . && git commit -m "Initial commit"
git push -u origin main
```

### 5. Deploy to Vercel

- [vercel.com/new](https://vercel.com/new) → import the GitHub repo.
- Framework preset: **Other**. Vercel detects `vercel.json` and uses `@vercel/python`.
- In **Project → Settings → Environment Variables**, add:
  - `TELEGRAM_BOT_TOKEN`
  - `OPENAI_API_KEY`
  - `WEBHOOK_SECRET`
  - `CRON_SECRET` — used ONLY by the cron endpoints. Not accepted on the admin panel.
  - `ADMIN_USERNAME` and `ADMIN_PASSWORD` — required for the admin panel.
  - `VERCEL_URL` — the public domain without scheme, e.g. `kuswise-bot.vercel.app`
  - (`DATABASE_URL` is auto-injected by the Neon integration — do not set manually.)
- Redeploy to pick up env vars.

### 6. Register the webhook + command menu + Mini App button

From your local machine:

```bash
python scripts/set_webhook.py
```

This runs `setWebhook`, `setMyCommands` (UA + EN fallback), and `setChatMenuButton` for the Mini App dashboard.

### 7. Try it out

1. `/start` in Telegram — complete onboarding (age, sex, weight, height, gym, goal, calorie confirm).
2. Send a photo, text, or voice message of a meal → pick meal type → accept the analysis.
3. Tap `💧 +250мл` to log water.
4. Tap `⭐ Улюблені` or use `/recent` for one-tap re-log.
5. Tap `📊 День` to see progress bars.
6. Open the 📱 Dashboard button (left of the input area) for the Mini App view.

---

## Architecture

```
Telegram ─▶ POST /api/webhook ──▶ process_update
                │
                ├─▶ lib/database.py          ── psycopg ─────▶ Neon Postgres
                ├─▶ lib/openai_vision.py     ── GPT-4o    ───▶ photo/text analysis
                ├─▶ lib/openai_voice.py      ── Whisper-1 ───▶ UA voice transcripts
                ├─▶ lib/openai_chat.py       ── GPT-4o-mini ─▶ /ask replies
                └─▶ lib/openai_nutrition.py  ── GPT-4o    ───▶ nightly summary, recipes

Vercel Cron ─▶ GET /api/cron_daily_summary         (20:00 UTC daily)
Vercel Cron ─▶ GET /api/cron_midnight_reset        (00:00 UTC daily)
Vercel Cron ─▶ GET /api/cron_weekly_weight_checkin (06:00 UTC Mon)
Vercel Cron ─▶ GET /api/cron_inactivity_nudge      (17:00 UTC daily)

Mini App    ─▶ POST /api/dashboard  (Telegram initData HMAC-signed)
Admin       ─▶ GET  /api/admin_stats (HTTP Basic Auth / Bearer token)
```

- **Stateless serverless** — all transient state (`pending_photos`, `pending_analyses`) lives in DB with a 10-min TTL.
- **Webhook auth:** `X-Telegram-Bot-Api-Secret-Token` header.
- **Cron auth:** `Authorization: Bearer $CRON_SECRET`.
- **Admin auth:** HTTP Basic with `ADMIN_USERNAME` / `ADMIN_PASSWORD`, OR `Authorization: Bearer $ADMIN_PASSWORD` for curl. `CRON_SECRET` is no longer accepted on the admin path. CSRF protected via same-origin Origin-header check on POSTs.
- **Mini App auth:** HMAC-SHA256 on `initData` with 24-h freshness window, per Telegram's spec.
- **Webhook always returns 200** (errors logged, never retried by Telegram).

### Database schema

Permanent (long-lived business data):

- `users` — Telegram id + username.
- `user_profiles` — age, sex, weight, height, gym freq, goal, calorie target, onboarding step, `tz`, `lang`, `target_weight_kg`, `weekly_delta_kg`, `weekly_checkin_sent_at`, `last_nudge_sent_at`, `nudge_optout`.
- `user_health_profile` — `allergens TEXT[]`, `conditions TEXT[]`, free-text notes; injected into AI prompts.
- `meals` — description, ingredients (JSON), macros (incl. `fiber_g`, `sugar_g`), `allergen_warnings`, `crohn_warnings`, `is_favorite`, photo file_id, raw AI response.
- `daily_logs` — per-day aggregates incl. fiber + sugar + `summary_sent` flag.
- `daily_recommendations` — nightly summary text history.
- `corrections` — audit trail of every manual edit / recalc / candidate pick (used by personalization).
- `user_food_aliases` — per-user EWMA portion learning (typical grams + macros for each "usual" dish name).
- `user_streaks` — current + longest streak, `last_log_date`, `freeze_days_remaining` (resets monthly).
- `weight_history` — every weight check-in (`weight_kg`, `source`, `recorded_at`).
- `water_logs` / `water_prefs` — water entries + per-user target.
- `meal_plans` — 3-day plan templates (pruned at 90 days).

Transient (TTL-cleaned):

- `pending_photos` / `pending_analyses` — staging tables for in-flight meal flow (10-min TTL).
- `chat_sessions` — `/ask` history (60-min TTL).
- `menu_ocr_results` — cached menu-OCR result while user picks a dish (1-hour TTL).
- `usage_quota` — daily counter per `(user_id, action)` (pruned at 7 days).

---

## Local development

```bash
vercel dev --listen 3000
ngrok http 3000
# Temporarily set VERCEL_URL to the ngrok domain and re-run set_webhook.py.
```

Easier in practice: push to a preview branch, test against the Vercel preview URL.

---

## Files

```
kuswise_bot/
├── vercel.json                        # routes + cron schedule (4 cron jobs)
├── requirements.txt                   # httpx, psycopg, openai, python-dotenv, pillow, qrcode
├── api/
│   ├── webhook.py                     # Telegram updates (commands, callbacks, voice/photo/text)
│   ├── dashboard.py                   # Mini App: per-user dashboard (HTML + initData auth)
│   ├── scan.py                        # Mini App: barcode scanner page
│   ├── barcode.py                     # POST endpoint for OFF barcode lookups
│   ├── admin_stats.py                 # HTML admin dashboard with Basic Auth
│   ├── cron_daily_summary.py          # 20:00 UTC daily GPT-4o end-of-day coaching
│   ├── cron_midnight_reset.py         # 00:00 UTC daily janitorial cleanup
│   ├── cron_weekly_weight_checkin.py  # Monday 06:00 UTC weight-prompt
│   └── cron_inactivity_nudge.py       # 17:00 UTC daily 24h-inactivity nudge
├── lib/
│   ├── config.py                      # env + prompts + LOCAL_TZ (Europe/Kyiv)
│   ├── database.py                    # schema + CRUD (every table; idempotent init_db)
│   ├── telegram_helpers.py            # sendMessage, editMessage, keyboards (incl. nudge_optout)
│   ├── initdata.py                    # Telegram WebApp initData HMAC verification
│   ├── i18n/                          # dict_en.json, dict_uk.json + plural rules
│   ├── bot_commands.py                # COMMAND_NAMES + per-locale rendering
│   ├── openai_vision.py               # GPT-4o photo / text / menu OCR
│   ├── openai_voice.py                # Whisper-1 UA transcription
│   ├── openai_nutrition.py            # daily summaries, recipes, meal plans
│   ├── openai_chat.py                 # /ask multi-turn with profile + day context
│   ├── health.py                      # F-1 allergens + conditions registry
│   ├── personalization.py             # F-7 EWMA portion learning
│   ├── goals.py                       # F-5 weight projection + weekly delta
│   ├── mealplan.py                    # F-10 3-day plan generator
│   ├── recap.py                       # F-12 PNG recap card (Pillow + QR)
│   ├── off.py                         # F-8 Open Food Facts client
│   ├── rate_limit.py                  # daily per-user quotas
│   ├── log.py                         # Sentry + structured logging
│   ├── datehelpers.py                 # Kyiv-local date math + tz validation
│   └── formatters.py                  # bilingual rendering (today, history, plans, recap)
├── scripts/
│   ├── set_webhook.py                 # registers webhook + commands + Mini App button
│   ├── setup_bot_commands.py          # pushes slash menu across (scope × language) matrix
│   ├── check_i18n.sh                  # CI gate: no Cyrillic in user-facing Python sources
│   └── stats.py                       # ad-hoc DB usage reports
└── tests/
    ├── test_smoke.py                  # ~165 unit tests; pure-function focus
    ├── test_i18n_snapshots.py         # UA/EN parity + plural rule + audit-script gate
    └── conftest.py                    # env-var stubs so tests never touch real DB
```

---

## Command reference

| Command | What it does |
|---|---|
| `/start` | Welcome + onboarding (or open menu if already onboarded). |
| `/today` | Today's progress (calories, macros, bars). |
| `/yesterday` | Yesterday's summary + meals. |
| `/history` | Last 7 days at a glance. |
| `/history_detail YYYY-MM-DD` | Meals for a specific day. |
| `/streak` | Current + longest streak + freezes remaining. |
| `/goals` | Weight goal + weekly delta + projection (weeks-to-goal). |
| `/recap` | Sends the weekly PNG recap card to chat (F-12). |
| `/scan` | Opens the barcode scanner Mini App (F-8). |
| `/menu` | OCR a restaurant menu photo into a dish list (F-9). |
| `/plan` | Generate a 3-day meal plan from your goals + pantry (F-10). |
| `/suggest_meal` | AI recipe sized to close today's macros gap (F-11). |
| `/meals` | Today's meals list with delete/edit buttons. |
| `/fav` | Favorite meals with one-tap re-log. |
| `/recent` | Last 10 unique meals for quick repeat. |
| `/water` | Water tracker (bar + quick-add + goal). |
| `/aliases` | Your habitual dishes (the bot's learned EWMA portions, F-7). |
| `/ask` | Chat with the AI about food + training nutrition. |
| `/health` | Edit allergens + chronic conditions; fed into AI prompts (F-1). |
| `/language` | Switch interface language (UA / EN). |
| `/timezone` | Set your local timezone (affects "today" boundary). |
| `/profile` | View/edit profile. |
| `/quiet` | Mute / unmute the inactivity nudge. Also clears any in-flight free-text input. |
| `/cancel` | Abort mid-flow (after tapping ✏️ Ввести вручну). |
| `/help` | Command list. |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Webhook registered but no replies | Check `VERCEL_URL` has no `https://` or trailing slash. Redeploy, re-run `set_webhook.py`. Check Vercel → Logs. |
| `403` from webhook | `WEBHOOK_SECRET` in Vercel env doesn't match the one used by `set_webhook.py`. |
| `psycopg` / DB connection error | Confirm `requirements.txt` is at repo root and `DATABASE_URL` is auto-injected by the Neon integration. |
| Photo/voice analysis times out | Vercel Hobby has a 60 s function cap. Whisper + GPT-4o together are usually ≤ 10 s; rerun. |
| Voice rejected as "задовге" | Messages over 2 MB (~60–90 s) are rejected by design. |
| Cron didn't fire | Crons require a Production deployment. Promote the deploy, or GET the cron URL manually with `Authorization: Bearer $CRON_SECRET`. |
| Admin panel "Unauthorized" | Send Basic Auth: `curl -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" https://kuswise-bot.vercel.app/api/admin_stats` — or `-H "Authorization: Bearer $ADMIN_PASSWORD"`. Avoid `https://user:pass@host/...` URLs (history/log leaks). |
| Water target didn't update after weight change | Expected if user manually set a target before — `target_overridden = 1` locks it. |

---

## Deployment checklist

- [ ] GitHub repo pushed
- [ ] Vercel project imported + first deploy succeeded
- [ ] Neon database provisioned via Vercel → Storage
- [ ] Env vars set: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `WEBHOOK_SECRET`, `CRON_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `VERCEL_URL`
- [ ] Deployment promoted to Production (so crons run)
- [ ] `python scripts/set_webhook.py` succeeded (webhook + commands + Mini App button)
- [ ] `/start` onboarding works end-to-end
- [ ] Photo, text, and voice meal logging each produce an analysis and save to DB
- [ ] `/water`, `/fav`, `/recent` all respond
- [ ] Mini App Dashboard opens (left of input area)
- [ ] Admin panel loads at `/api/admin_stats`

---

## Costs (at current scale)

- **OpenAI**
  - Photo/text meal analysis (GPT-4o): ~$0.005–0.01 each.
  - Voice transcription (Whisper-1): $0.006/min → ~$0.001 per meal log.
  - Chat (GPT-4o-mini): ~$0.001 per `/ask`.
- **Vercel Hobby**: free tier covers the current load.
- **Neon Postgres**: free tier (no data volume concerns at this scale).

At ~100 daily users with ~3 meals each: roughly $1–3/day OpenAI, rest free.

---

## Not yet implemented

- Shortened 3-question onboarding (current flow needs age/weight/height for Mifflin-St Jeor).
- Editable meals from the dashboard (today only via `/meals` in chat).
- Adaptive weekly target updates (MacroFactor-style — recompute `daily_calorie_target` from logged intake + smoothed weight delta). All inputs exist; the loop doesn't yet.
- Multi-stage nudge sequences ("we miss you" at 7 d, 30 d). The 24h inactivity nudge is one-shot with a 7-day cooldown.
- Water reminders (the inactivity nudge fires on meals only).
- In-dashboard `/ask` chat surface (lives only in the bot today).
- CSV / JSON export of a user's history (admin can already export meals).
