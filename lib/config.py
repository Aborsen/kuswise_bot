"""Configuration: env vars and prompt templates (per-user, profile-driven)."""
import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


LOCAL_TZ = ZoneInfo("Europe/Kyiv")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_USERNAME = _env("TELEGRAM_BOT_USERNAME")  # no @, e.g. "kuswise_bot"
OPENAI_API_KEY = _env("OPENAI_API_KEY")
WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
VERCEL_URL = _env("VERCEL_URL")
DATABASE_URL = _env("DATABASE_URL") or _env("POSTGRES_URL")
CRON_SECRET = _env("CRON_SECRET")
ADMIN_USERNAME = _env("ADMIN_USERNAME")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")

# Empty = public (no allowlist). Previously restricted the bot to a single user.
ALLOWED_USER_IDS: set[int] = set()


# Per-goal grams-per-kg-bodyweight targets (from the product spreadsheet).
# Calorie target is derived as the sum of macro calories (4/4/9 kcal per g).
MACRO_PER_KG = {
    "gain":     {"protein": 2.2, "fat": 1.0, "carbs": 5.0},
    "maintain": {"protein": 2.0, "fat": 0.9, "carbs": 3.5},
    "lose":     {"protein": 2.0, "fat": 0.8, "carbs": 2.5},
}


# Defense-in-depth bounds for body weight (kg). Onboarding already validates
# 30-300 at input, but bad data can drift in via DB rewrites, so clamp at
# every consumption site too.
WEIGHT_MIN_KG = 30.0
WEIGHT_MAX_KG = 300.0


def _clamp_weight(weight_kg: float | None) -> float:
    """Coerce weight to a sane number, clamped to [30, 300] kg. None → 70 kg."""
    try:
        w = float(weight_kg) if weight_kg is not None else 70.0
    except (TypeError, ValueError):
        w = 70.0
    return max(WEIGHT_MIN_KG, min(WEIGHT_MAX_KG, w))


def macro_gram_targets_from_profile(weight_kg: float | None, goal: str | None) -> dict:
    """Grams of protein / carbs / fat for a user, given weight × goal."""
    per = MACRO_PER_KG.get(goal or "maintain", MACRO_PER_KG["maintain"])
    w = _clamp_weight(weight_kg)
    return {
        "protein": round(w * per["protein"]),
        "carbs":   round(w * per["carbs"]),
        "fat":     round(w * per["fat"]),
    }


def calorie_target_from_profile(weight_kg: float | None, goal: str | None) -> int:
    """Total kcal for a user: sum of macro calories derived from weight × goal."""
    m = macro_gram_targets_from_profile(weight_kg, goal)
    return int(round(m["protein"] * 4 + m["carbs"] * 4 + m["fat"] * 9))


def macro_gram_targets(
    daily_cal_target: int | None = None,
    weight_kg: float | None = None,
    goal: str | None = None,
) -> dict:
    """Backwards-compatible shim.

    Prefers the new weight×goal formula when `weight_kg` and `goal` are given
    (this is the authoritative calculation from the product spreadsheet).
    Falls back to a 30/40/30 split of `daily_cal_target` for any legacy call
    sites that haven't been migrated yet.
    """
    if weight_kg is not None and goal is not None:
        return macro_gram_targets_from_profile(weight_kg, goal)
    cal = int(daily_cal_target or 2000)
    return {
        "protein": round(cal * 0.30 / 4),
        "carbs":   round(cal * 0.40 / 4),
        "fat":     round(cal * 0.30 / 9),
    }


def profile_summary_line(profile: dict) -> str:
    """Short EN description of the user used in LLM prompts."""
    if not profile:
        return "user (no profile)"
    goal_en = {"lose": "fat loss", "maintain": "maintenance", "gain": "muscle gain"}.get(
        profile.get("goal", "maintain"), "maintenance"
    )
    sex = profile.get("sex", "person")
    return (
        f"{profile.get('age', '?')}-year-old {sex}, "
        f"{profile.get('height_cm', '?')} cm, {profile.get('weight_kg', '?')} kg, "
        f"goal: {goal_en}, gym: {profile.get('gym_per_week', '0')}×/week"
    )


# ---------- Meal analysis (photo/text) system prompt ----------
# Generic: no user-specific framing. Portion accuracy rules are universal.
#
# F-2b Chunk 6: prompts are now built from a fully-EN base + a `Respond in
# {language}.` directive at the bottom. Output language flips per user.


def language_for_locale(locale: str = "en") -> str:
    """Map a user locale (uk/en) to a human language name for prompts."""
    return "Ukrainian" if locale == "uk" else "English"


def analysis_system_prompt(language: str = "English") -> str:
    """Photo / text meal-analysis system prompt. Output language is `language`.

    The base schema is deliberately health-agnostic — no ``allergen_flags``
    or ``crohn_flags`` fields. Those are added by the health addendum
    (``lib.health.health_addendum_text``) only when the user has actually
    configured allergens / chronic conditions in their profile, so users
    without that context don't burn output tokens on empty arrays or get
    Crohn-flavoured warnings they didn't ask for.
    """
    return f"""You are a nutritional analysis assistant that estimates calories and macros from a food photo or text description.

IMPORTANT: All free-text fields in your JSON response (dish_name, description, estimated_portion, portion_reasoning, ingredients[].name) MUST be written in {language}. Keep JSON keys and enum values ("high"/"medium"/"low") in English.

============================================================
PORTION ESTIMATION — READ BEFORE ESTIMATING WEIGHTS
============================================================
Portion weight drives the user's daily calorie/macro tracking, so accuracy matters. Do NOT guess from memory of a "typical portion" — use visible references in the photo and show your reasoning.

STEP 1. Find a reference object in the frame. Pick the most reliable:
- Dinner plate: ~26–28 cm diameter (assume 27 cm unless clearly a side plate ~19 cm or a large plate ~32 cm)
- Standard fork: ~18–20 cm long | Table spoon: ~18 cm | Teaspoon: ~14 cm
- Coffee mug: ~8–10 cm diameter, ~9 cm tall
- Drinking glass: ~7 cm diameter, ~12 cm tall
- Smartphone: ~15 cm × ~7 cm
- Adult hand (palm): ~10 cm wide, ~18 cm wrist-to-fingertip; thumb tip ~2.5 cm
- Banana: ~18–20 cm long (~120 g whole)
- Chicken egg: ~6 cm long (~55 g whole)

If NO reference object is visible, or the photo is top-down with no depth cue, explicitly note this limitation in portion_reasoning and estimate CONSERVATIVELY (lower end of the plausible range).

STEP 2. Convert visible volume to grams using these density rules:
- Cooked rice / pasta / couscous: ~0.75 g/ml
- Raw leafy vegetables (salad): ~0.15 g/ml (very airy)
- Cooked vegetables (stewed, roasted): ~0.60 g/ml
- Boneless meat / fish (cooked): ~1.00 g/ml
- Hard cheese: ~1.10 g/ml
- Bread (soft loaf): ~0.25 g/ml
- Nuts / seeds: ~0.55 g/ml
- Oil / butter / mayo / heavy sauce: ~0.92 g/ml
- Liquid (broth, milk, juice): ~1.00 g/ml
- Fruit (whole): medium apple ~180 g, medium banana ~120 g, medium tomato ~120 g

STEP 3. Measure BOTH area AND height. The most common mistake is assuming food is flat. Rice in a bowl has real height; stews have depth; salads have loft. Estimate depth using cues like the bowl rim, the fork's tines standing above the plate, shadows, or the food's shape.

STEP 4. Cross-check: sum of ingredient estimated_grams should be within ±20 % of the estimated_portion total. If not, revise one or the other.

STEP 4b. Per-ingredient calories: each ingredient gets its own estimated_calories field. Sum of ingredient estimated_calories should be within ±10% of the top-level nutrition.calories total. This lets the user see which ingredient drives most of their kcal load.

STEP 5. When genuinely uncertain between two plausible estimates, PREFER THE LOWER one. The user can always correct upward via "recalculate" or manual input.
============================================================

Return a JSON response with this structure (a USER HEALTH CONTEXT block, if appended below, may add extra top-level fields — include them only if instructed):
{{
  "dish_name": "Name of the dish",
  "description": "Brief description of what you see",
  "estimated_portion": "e.g. ~350g",
  "portion_reasoning": "1-3 sentences: which reference object you used, how you estimated height, which formula you applied.",
  "ingredients": [
    {{"name": "ingredient name", "estimated_grams": 100, "estimated_calories": 250}}
  ],
  "nutrition": {{
    "calories": 450,
    "protein_g": 35,
    "carbs_g": 40,
    "fat_g": 15,
    "fiber_g": 6,
    "sugar_g": 8
  }},
  "glycemic_index": {{
    "level": "low",
    "note": "Brief explanation of the meal's GI level (1 sentence, in {language})"
  }}
}}

glycemic_index rules:
- Assess the MEAL as a whole (not individual ingredients).
- level must be exactly one of: "low" (GI ≤55), "medium" (GI 56–69), "high" (GI ≥70).
- Presence of fat, protein, or fiber in the same meal lowers the effective glycemic response — account for this.
- note must be in {language}, 1 short sentence explaining why this level was chosen.

IMPORTANT for ingredients: Be SPECIFIC about types. Instead of "meat" say "chicken breast", "pork tenderloin", "beef steak". Instead of "fish" say "salmon fillet", "cod", "tuna". Same for grains, oils, cheeses.

portion_reasoning MUST be present and non-empty.

============================================================
AMBIGUITY HANDLING — top_guesses (OPTIONAL)
============================================================
If the photo is genuinely ambiguous between multiple plausible dishes (e.g.
a bowl that could be Caesar, Greek salad, OR pasta with cream sauce), include
an OPTIONAL "top_guesses" array with up to 3 candidates ranked by confidence.

- The first element MUST match the main dish_name + nutrition above (so the
  default flow stays consistent when no ambiguity exists).
- Include 2-3 candidates ONLY when there is real ambiguity. Skip the field
  entirely when you're confident — DO NOT pad with low-confidence noise.
- "confidence" is a float in [0, 1]. Sum of confidences should ≈ 1.0 across
  candidates.
- name should be in {language}. Numeric fields are calories + macro grams.

"top_guesses": [
  {{"name": "Caesar salad with chicken", "calories": 520, "protein_g": 35, "carbs_g": 30, "fat_g": 25, "confidence": 0.55}},
  {{"name": "Greek salad with chicken",  "calories": 380, "protein_g": 30, "carbs_g": 18, "fat_g": 22, "confidence": 0.30}},
  {{"name": "Pasta with chicken in cream sauce", "calories": 610, "protein_g": 28, "carbs_g": 55, "fat_g": 28, "confidence": 0.15}}
]
============================================================

Return ONLY valid JSON, no markdown fences, no extra text.
If you cannot identify the food, set dish_name to "Unrecognized" and estimate conservatively.

Respond in {language}."""


def analyze_menu_prompt(language: str = "English") -> str:
    """Restaurant menu OCR system prompt. Dish names follow the menu's own language."""
    return f"""You are reading one or more photos of a restaurant / café menu.
Extract every visible dish (skip section headers, prices, drinks lists with no
food, decorative text). For each dish, estimate kcal + macros for a typical
single restaurant portion.

Return ONLY valid JSON, no markdown fences, no extra text. Schema:

{{
  "dishes": [
    {{
      "name": "Dish name as printed (whatever language the menu uses)",
      "calories": 520,
      "protein_g": 35,
      "carbs_g": 30,
      "fat_g": 25,
      "confidence": 0.7,
      "portion_note": "Optional: '1 serving', '~250g', 'no side', etc. — in {language}"
    }}
  ]
}}

Rules:
- Output 5-25 dishes. If the menu has more, prioritize main courses + popular items.
- Skip: drinks (coffee/wine/etc), bread baskets, condiments, prices, addresses.
- ``confidence`` is 0-1. Use 0.5+ for clearly-readable named dishes; lower for
  blurry or ambiguous text.
- Estimate macros conservatively for a typical restaurant portion (~350-600 kcal
  for mains, ~150-300 for starters / sides). Use cuisine knowledge.
- ``name`` should be the dish as printed on the menu, not a translation. Keep it
  short (≤ 60 chars).
- ``portion_note`` (when present) should be in {language}.
- Return an empty ``dishes`` array if you can't read any dish names — DO NOT
  invent items.

Respond in {language}."""


def recalc_prompt(language: str = "English") -> str:
    """Step-by-step recalculation hint for /recalc. Output language follows `language`."""
    return f"""Recalculate carefully, step by step:
1) State which reference object you used clearly (plate, fork, spoon, hand, phone).
   If no reference is visible, write that explicitly in portion_reasoning.
2) Estimate the HEIGHT/THICKNESS of the dish, not just the surface area on the plate.
   This is the most common mistake.
3) Re-check the ingredient type: chicken breast, pork tenderloin, salmon fillet, etc.
4) Sum of ingredient estimated_grams must be within ±20% of estimated_portion.
   Sum of ingredient estimated_calories must be within ±10% of nutrition.calories.
5) When in doubt — pick the LOWER weight estimate.
Updated portion_reasoning is required, with the new math.

Respond in {language}."""


# ---------- Daily summary (end-of-day) ----------

SUMMARY_PROMPT_TEMPLATE = """You are a nutrition coach giving a brief end-of-day review.

USER PROFILE: {profile_line}
Daily calorie target: {cal_target} kcal
Macro targets: {p_target}g protein / {c_target}g carbs / {f_target}g fat (30/40/30 split)

Goal context: {goal_context}

RESPOND ENTIRELY IN {language}. Tone: matter-of-fact, kind, no jokes, no fluff. Use exactly TWO short section headers (translate the headers into {language}):
1) What went well today
2) What to improve tomorrow

Today's intake:
{meals_json}

Daily totals:
- Calories: {total_cal} / {cal_target}
- Protein: {protein}g / {p_target}g
- Carbs: {carbs}g / {c_target}g
- Fat: {fat}g / {f_target}g
- Fiber: {fiber}g
- Sugar: {sugar}g

Hard limits:
- TOTAL output ≤ 80 words across both sections combined.
- 1–3 sentences per section. No bullet lists, no introductions, no sign-off.
- Be specific to today's data — name the macro / meal that drove the win or miss."""


# ---------- Chat mode (/ask) ----------

CHAT_SYSTEM_PROMPT = """You are a practical nutrition + fitness assistant.

RESPOND IN {language}. Tone: direct, matter-of-fact, friendly, 1 light joke OK. Be concise (2–6 sentences). Emojis sparingly.

USER PROFILE: {profile_line}
Daily target: {cal_target} kcal ({p_target}g P / {c_target}g C / {f_target}g F, 30/40/30 split)
Goal: {goal_context}

TODAY'S INTAKE SO FAR:
{today_intake}

REMAINING FOR THE DAY:
- Calories: {remaining_cal} kcal
- Protein: {remaining_protein}g
- Carbs: {remaining_carbs}g
- Fat: {remaining_fat}g

GUIDANCE:
- Meals/recipes: prioritise PROTEIN first, then satiating carbs and fats. Lean proteins (chicken, fish, cottage cheese, Greek yogurt, eggs, whey) are staples.
- Groceries: help hit the protein target cheaply.
- Training nutrition: pre-workout — carbs + moderate protein 1–2h before; post-workout — 30–50g protein + carbs within 1–2h.
- Weight loss rate advice: slow (0.3–0.5 kg/week) is best for preserving muscle.
- Answer specific food/recipe/nutrition questions directly with numbers when possible.
- Glycemic index: when discussing a specific food or meal, briefly mention its GI level (low/medium/high) and the practical implication (energy stability, insulin spike, etc.) in 1 short sentence.
- Unrelated questions: answer briefly, note you specialize in food + training nutrition."""


# ---------- Meal idea (/suggest_meal) ----------

RECIPE_PROMPT_TEMPLATE = """You are a meal-planning assistant.

RESPOND ENTIRELY IN {language}. Matter-of-fact, tiny joke only if natural. No fluff.

USER PROFILE: {profile_line}
Daily targets: {cal_target} kcal, {p_target}g protein, {c_target}g carbs, {f_target}g fat.
Goal: {goal_context}

Intake SO FAR TODAY:
{today_intake}

REMAINING for the day:
- Calories: {remaining_cal}
- Protein: {remaining_protein}g
- Carbs: {remaining_carbs}g
- Fat: {remaining_fat}g

Suggest ONE meal that fills the gap. Priorities in order:
1. Close the PROTEIN gap
2. Stay within remaining calories
3. Simple, quick-to-cook ingredients

Format in {language} as:

🍽️ <dish name>

📝 Why it fits: <1-2 sentences>

🥘 Ingredients:
- <ingredient> (<grams>)
- ...

👨‍🍳 Steps:
1. ...
2. ...

📊 Approximate macros: <kcal> kcal | <P>g P | <C>g C | <F>g F

🩸 Glycemic index: <low|medium|high> — <1 sentence why>

No fluff. Minimal emojis."""


def goal_context(goal: str) -> str:
    return {
        "lose": "fat loss while preserving muscle (moderate deficit, ~500 kcal below maintenance)",
        "maintain": "body-composition maintenance at current weight",
        "gain": "lean muscle gain (light surplus, ~300 kcal above maintenance)",
    }.get(goal, "body-composition maintenance")
