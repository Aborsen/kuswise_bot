# KusWise Bot — Ukrainian Calorie Tracker Bot

A Ukrainian-language Telegram bot that logs meals from **photos, text, or voice** and gives personalized calorie/macro coaching. Deployed on **Vercel** (Python serverless) with **Neon Postgres**. Uses **GPT-4o** for vision + text analysis, **Whisper** for voice transcription, and **GPT-4o-mini** for chat.

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
- Day view: calorie/macro bars, **water progress**, smart daily tips, meal list with allergen/health-note badges.
- Yesterday view with same layout.
- 7-day and 30-day aggregates with sortable tables.

### Admin panel (`/api/admin_stats`)
- HTTP Basic Auth (any username, password = `CRON_SECRET`).
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

### Automation
- 🌙 Nightly GPT-4o summary at **20:00 UTC** (Vercel Cron).
- 🔄 Midnight cleanup cron at **00:00 UTC** (marks stale summaries, prunes pending rows).

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
  - `CRON_SECRET`
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

Vercel Cron ─▶ GET /api/cron_daily_summary   (20:00 UTC)
Vercel Cron ─▶ GET /api/cron_midnight_reset  (00:00 UTC)

Mini App    ─▶ POST /api/dashboard  (Telegram initData HMAC-signed)
Admin       ─▶ GET  /api/admin_stats (HTTP Basic Auth / Bearer token)
```

- **Stateless serverless** — all transient state (`pending_photos`, `pending_analyses`) lives in DB with a 10-min TTL.
- **Webhook auth:** `X-Telegram-Bot-Api-Secret-Token` header.
- **Cron auth:** `Authorization: Bearer $CRON_SECRET`.
- **Admin auth:** HTTP Basic (password = `CRON_SECRET`) or Bearer; CSRF via same-origin check on POSTs.
- **Mini App auth:** HMAC-SHA256 on `initData` with 24-h freshness window, per Telegram's spec.
- **Webhook always returns 200** (errors logged, never retried by Telegram).

### Database schema

- `users` — Telegram id + username.
- `user_profiles` — age, sex, weight, height, gym freq, goal, calorie target, onboarding step.
- `meals` — description, ingredients (JSON), macros, `is_favorite`, photo file_id.
- `daily_logs` — per-day aggregates + `summary_sent` flag.
- `daily_recommendations` — nightly summary text history.
- `pending_photos` / `pending_analyses` — transient flow state (10-min TTL).
- `chat_sessions` — `/ask` history (60-min TTL).
- `water_logs` / `water_prefs` — water entries + per-user target.

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
├── vercel.json                        # routes + cron schedule
├── requirements.txt                   # httpx, psycopg[binary], openai, python-dotenv
├── api/
│   ├── webhook.py                     # Telegram updates (commands, callbacks, voice/photo/text)
│   ├── dashboard.py                   # Telegram Mini App (HTML + initData auth)
│   ├── admin_stats.py                 # HTML admin dashboard with Basic Auth
│   ├── cron_daily_summary.py          # 20:00 UTC nightly GPT-4o summary
│   └── cron_midnight_reset.py         # 00:00 UTC cleanup
├── lib/
│   ├── config.py                      # env + prompts + LOCAL_TZ (Europe/Kyiv)
│   ├── database.py                    # schema + CRUD (users, meals, water, favorites, pending)
│   ├── telegram_helpers.py            # sendMessage, editMessage, sendChatAction, keyboards
│   ├── openai_vision.py               # GPT-4o photo + text analysis (JSON with GI, portion)
│   ├── openai_voice.py                # Whisper-1 UA transcription
│   ├── openai_nutrition.py            # summaries + recipes
│   ├── openai_chat.py                 # /ask chat with history + profile context
│   └── formatters.py                  # HTML templates, progress bars, menu labels
└── scripts/
    └── set_webhook.py                 # registers webhook + commands + Mini App button
```

---

## Command reference

| Command | What it does |
|---|---|
| `/start` | Welcome + onboarding (or open menu if already onboarded). |
| `/today` | Today's progress (calories, macros, bars). |
| `/yesterday` | Yesterday's summary + meals. |
| `/meals` | Today's meals list with delete/edit buttons. |
| `/fav` | Favorite meals with one-tap re-log. |
| `/recent` | Last 10 unique meals for quick repeat. |
| `/water` | Water tracker (bar + quick-add + goal). |
| `/history` | Last 7 days at a glance. |
| `/history_detail YYYY-MM-DD` | Meals for a specific day. |
| `/suggest_meal` | AI recipe sized to close today's gap. |
| `/ask` | Chat with the AI about food + training nutrition. |
| `/profile` | View/edit profile. |
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
| Admin panel "Unauthorized" | Use `https://admin:$CRON_SECRET@kuswise-bot.vercel.app/api/admin_stats` or send `Authorization: Bearer $CRON_SECRET`. |
| Water target didn't update after weight change | Expected if user manually set a target before — `target_overridden = 1` locks it. |

---

## Deployment checklist

- [ ] GitHub repo pushed
- [ ] Vercel project imported + first deploy succeeded
- [ ] Neon database provisioned via Vercel → Storage
- [ ] Env vars set: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `WEBHOOK_SECRET`, `CRON_SECRET`, `VERCEL_URL`
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
- Per-user timezone (all time math is Europe/Kyiv).
- Water reminders (V1 is log-only, no scheduled nudges).
- Shareable PNG day-recap cards.
- Confidence-threshold "clarify" buttons on low-confidence photo recognition.
- Personalized vision prompting from user correction history.
