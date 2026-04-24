"""OpenAI call for /ask chat mode — free-form Q&A with memory + today's intake context."""
from openai import OpenAI

from lib.config import (
    CHAT_SYSTEM_PROMPT,
    OPENAI_API_KEY,
    goal_context,
    macro_gram_targets,
    profile_summary_line,
)

_CHAT_MODEL = "gpt-4.1-mini"
_MAX_TOKENS = 800

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _render_today_intake(today_meals: list[dict]) -> str:
    if not today_meals:
        return "(сьогодні ще нічого не записано)"
    lines = []
    for m in today_meals:
        lines.append(
            f"- {m.get('meal_type', 'meal').capitalize()}: {m.get('description', '')} "
            f"({round(m.get('calories', 0))} kcal, "
            f"{round(m.get('protein_g', 0))}g P, "
            f"{round(m.get('carbs_g', 0))}g C, "
            f"{round(m.get('fat_g', 0))}g F)"
        )
    return "\n".join(lines)


def ask_chat(
    question: str,
    history: list[dict],
    today_log: dict,
    today_meals: list[dict],
    profile: dict,
) -> str:
    """Run one chat turn. System prompt is rebuilt each call so today's intake is always fresh."""
    cal_target = profile.get("daily_calorie_target") or 2000
    macros = macro_gram_targets(cal_target)

    remaining_cal = max(0, cal_target - (today_log.get("calories") or 0))
    remaining_p = max(0, macros["protein"] - (today_log.get("protein") or 0))
    remaining_c = max(0, macros["carbs"] - (today_log.get("carbs") or 0))
    remaining_f = max(0, macros["fat"] - (today_log.get("fat") or 0))

    system = CHAT_SYSTEM_PROMPT.format(
        profile_line=profile_summary_line(profile),
        cal_target=cal_target,
        p_target=macros["protein"],
        c_target=macros["carbs"],
        f_target=macros["fat"],
        goal_context=goal_context(profile.get("goal", "maintain")),
        today_intake=_render_today_intake(today_meals),
        remaining_cal=round(remaining_cal),
        remaining_protein=round(remaining_p),
        remaining_carbs=round(remaining_c),
        remaining_fat=round(remaining_f),
    )

    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": question})

    resp = _get_client().chat.completions.create(
        model=_CHAT_MODEL,
        max_tokens=_MAX_TOKENS,
        messages=messages,
    )
    return (resp.choices[0].message.content or "").strip()
