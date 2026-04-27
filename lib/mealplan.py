"""3-day meal plan generation (F-10).

Calls GPT-4o in JSON mode to produce a structured plan with breakfast /
lunch / dinner / snack for each of three days. Each slot is a dict with a
dish name, short recipe, and macros — designed so the user can tap "Log
this" and have the meal flow into ``save_meal`` unchanged.

Public surface:
    - ``MEAL_SLOTS``        — canonical ordered list of slot keys
    - ``generate_meal_plan(...)`` → dict { "days": [ {date_label, slots}, ... ] }
    - ``slot_to_analysis(slot_dict)`` — convert one slot to the analysis
      shape ``save_meal`` expects.
"""
from __future__ import annotations

import json
from typing import Optional

from openai import OpenAI

from lib.config import OPENAI_API_KEY


# Canonical order — used by both prompt + UI rendering. Keys match the
# ``meal_type`` enum used by save_meal().
MEAL_SLOTS = ("breakfast", "lunch", "dinner", "snack")

_SLOT_LABELS_UA = {  # noqa: i18n
    "breakfast": "Сніданок",  # noqa: i18n
    "lunch":     "Обід",  # noqa: i18n
    "dinner":    "Вечеря",  # noqa: i18n
    "snack":     "Перекус",  # noqa: i18n
}

_SLOT_LABELS_EN = {
    "breakfast": "Breakfast",
    "lunch":     "Lunch",
    "dinner":    "Dinner",
    "snack":     "Snack",
}


def slot_label_uk(slot: str) -> str:
    return _SLOT_LABELS_UA.get(slot, slot.capitalize())


def slot_label_for_locale(slot: str, locale: str = "en") -> str:
    table = _SLOT_LABELS_UA if locale == "uk" else _SLOT_LABELS_EN
    return table.get(slot, slot.capitalize())


def _build_system_prompt(language: str = "English") -> str:
    return f"""You generate practical 3-day meal plans tailored to one user.

OUTPUT: ONLY valid JSON, matching this schema EXACTLY (no markdown, no prose):

{{
  "days": [
    {{
      "date_label": "Today" | "Tomorrow" | "Day 3",
      "slots": {{
        "breakfast": {{ "name": "...", "calories": 420, "protein_g": 25, "carbs_g": 50, "fat_g": 12, "recipe": "..." }},
        "lunch":     {{ ... same shape ... }},
        "dinner":    {{ ... }},
        "snack":     {{ ... }}
      }}
    }},
    {{ ... }},
    {{ ... }}
  ],
  "notes": "Optional 1-2 sentence summary in {language}."
}}

RULES:
- Every "name", "recipe", and "notes" string in {language}.
- "date_label" stays in English ("Today" / "Tomorrow" / "Day 3"); the bot translates it client-side.
- "recipe" is 1-3 short steps (one paragraph, ≤120 chars). Not a full how-to.
- Macros are integers (grams + kcal); calories ≈ p*4 + c*4 + f*9 ± 10%.
- Each day's total kcal should be within ±10% of the daily target.
- Each day's protein total must be ≥ 80% of the daily protein target.
- Skip ingredients in the user's allergen list. Respect their dietary
  conditions (FODMAP, celiac, diabetes, etc.) if specified.
- If the user lists pantry items, prefer recipes that USE them — don't
  invent exotic ingredients. Otherwise pick everyday foods.
- Include variety: don't repeat the same protein source 3 days in a row.

NEVER include any text outside the JSON object.
"""


def _user_message(
    *,
    cal_target: int,
    p_target:   int,
    c_target:   int,
    f_target:   int,
    remaining:  dict,
    goal:       str,
    pantry:     str,
    health_addendum: str,
) -> str:
    """Build the user-side prompt with the user's specific context."""
    pantry_line = (
        f"PANTRY ITEMS the user wants to use up: {pantry.strip()}\n"
        if pantry.strip() else "PANTRY ITEMS: none specified — pick everyday foods.\n"
    )
    health_line = f"HEALTH CONTEXT:\n{health_addendum}\n" if health_addendum.strip() else ""
    return (
        f"USER GOAL: {goal}\n"
        f"DAILY TARGETS: {cal_target} kcal · P{p_target}g F{f_target}g C{c_target}g\n"
        f"REMAINING TODAY: {remaining.get('calories', 0)} kcal · "
        f"P{remaining.get('protein', 0)} F{remaining.get('fat', 0)} C{remaining.get('carbs', 0)}\n"
        f"{pantry_line}"
        f"{health_line}"
        f"\nGenerate the 3-day plan. Day 1 should respect REMAINING macros for today; "
        f"days 2-3 use full daily targets."
    )


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def generate_meal_plan(
    *,
    cal_target: int,
    p_target:   int,
    c_target:   int,
    f_target:   int,
    remaining:  dict,
    goal:       str,
    pantry:     str = "",
    health_addendum: str = "",
    language:   str = "English",
) -> dict:
    """Call GPT-4o JSON mode and return a normalized plan dict.

    Returns ``{"days": [...], "notes": "..."}``. Raises on parse / API
    failure — caller should catch + show a user-friendly error.
    """
    user_msg = _user_message(
        cal_target=cal_target, p_target=p_target,
        c_target=c_target, f_target=f_target,
        remaining=remaining, goal=goal,
        pantry=pantry, health_addendum=health_addendum,
    )
    resp = _get_client().chat.completions.create(
        model="gpt-4o",
        max_tokens=1800,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _build_system_prompt(language=language)},
            {"role": "user",   "content": user_msg},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    return normalize_plan(json.loads(raw))


def normalize_plan(raw: dict) -> dict:
    """Sanitize an LLM-produced plan into a stable shape callers can render.

    - Always returns 3 day entries (pads with empty slots if the model
      shorted us).
    - Each day always has all 4 slots in canonical order, with floats
      coerced to ints / numbers and missing values filled with 0.
    - Drops slots whose ``name`` is blank.
    """
    if not isinstance(raw, dict):
        raw = {}
    days_in = raw.get("days") or []
    days_out: list[dict] = []
    # date_label is English-language model output; the formatter translates
    # client-side based on user locale (see lib.formatters.format_meal_plan_day).
    default_labels = ("Today", "Tomorrow", "Day 3")
    for i in range(3):
        src = days_in[i] if (i < len(days_in) and isinstance(days_in[i], dict)) else {}
        slots_src = src.get("slots") or {}
        if not isinstance(slots_src, dict):
            slots_src = {}
        slots_out: dict = {}
        for key in MEAL_SLOTS:
            s = slots_src.get(key) or {}
            if not isinstance(s, dict):
                s = {}
            name = (s.get("name") or "").strip()[:80]
            if not name:
                slots_out[key] = None
                continue
            try:
                kcal = float(s.get("calories")  or 0)
                p    = float(s.get("protein_g") or 0)
                c    = float(s.get("carbs_g")   or 0)
                f    = float(s.get("fat_g")     or 0)
            except (TypeError, ValueError):
                kcal = p = c = f = 0.0
            recipe = (s.get("recipe") or "").strip()[:300]
            slots_out[key] = {
                "name":      name,
                "calories":  round(kcal),
                "protein_g": round(p),
                "carbs_g":   round(c),
                "fat_g":     round(f),
                "recipe":    recipe,
            }
        days_out.append({
            "date_label": (src.get("date_label") or default_labels[i])[:40],
            "slots":      slots_out,
        })
    notes = (raw.get("notes") or "").strip()[:300]
    return {"days": days_out, "notes": notes}


def slot_to_analysis(slot: dict) -> dict:
    """Convert one plan slot into the ``save_meal`` analysis shape.

    The pipeline downstream (streak, alias, daily-log) doesn't care that
    this came from the planner — we just feed plausible defaults for the
    fields that don't apply (ingredients, glycemic_index, allergen flags).
    """
    return {
        "dish_name":         slot["name"],
        "description":       slot["name"],
        # Locale-neutral source markers — the bot localises display via t()
        # at render time. Stored as English; treated as data, not display.
        "estimated_portion": "planned meal",
        "portion_reasoning": "from 3-day plan",
        "ingredients":       [],
        "allergen_flags":    [],
        "crohn_flags":       [],
        "nutrition": {
            "calories":  float(slot.get("calories")  or 0),
            "protein_g": float(slot.get("protein_g") or 0),
            "carbs_g":   float(slot.get("carbs_g")   or 0),
            "fat_g":     float(slot.get("fat_g")     or 0),
            "fiber_g":   0.0,
            "sugar_g":   0.0,
        },
        "glycemic_index":     {"level": "", "note": ""},
        "overall_assessment": "",
        "_source": {"kind": "meal_plan"},
    }
