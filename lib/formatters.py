"""Message formatting helpers for Telegram replies (UA + EN, with humor)."""
import html
import random
from datetime import datetime

from lib.config import LOCAL_TZ, macro_gram_targets, macro_gram_targets_from_profile


def _esc(value) -> str:
    """HTML-escape any value before interpolation into a Telegram parse_mode=HTML message.

    Bot replies use parse_mode=HTML, which renders a small subset of tags
    (<b>, <i>, <a>, <code>, …). User- or AI-derived strings (dish names,
    descriptions, ingredient names, free-form notes) MUST be escaped before
    interpolation so an attacker can't inject anchors or styling. Returns
    "" for None to avoid printing literal "None".
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _bar(used: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "─" * width
    pct = max(0.0, min(1.0, used / target))
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


def _pct(used: float, target: float) -> int:
    if target <= 0:
        return 0
    return round(100 * used / target)


# --- Ukrainian month names for pretty dates ---
# UA-only by design: only used by _ua_date_long/_ua_date_short helpers, which
# are themselves only invoked from the UA locale render path.
_UA_MONTHS_FULL = [
    "", "січня", "лютого", "березня", "квітня", "травня", "червня",  # noqa: i18n
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",  # noqa: i18n
]
_UA_MONTHS_SHORT = [
    "", "січ", "лют", "бер", "кві", "тра", "чер",  # noqa: i18n
    "лип", "сер", "вер", "жов", "лис", "гру",  # noqa: i18n
]


def _ua_date_long(dt: datetime) -> str:
    return f"{dt.day} {_UA_MONTHS_FULL[dt.month]}"


def _ua_date_short(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.day} {_UA_MONTHS_SHORT[dt.month]}"
    except Exception:
        return date_str


def _name_or_default(first_name: str | None) -> str:
    """Return a safe-for-HTML display name. Telegram first_name is user-controlled
    (set in the user's Telegram profile) and may contain HTML metacharacters."""
    name = first_name.strip() if (first_name and first_name.strip()) else "друже"  # noqa: i18n
    return _esc(name)


_CONFIDENCE_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
_SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}

# Defensive reverse-map: when the LLM ignores the addendum's enum and emits
# the friendly English label in `allergen_flags[].allergen` instead of the
# canonical id, normalise back to the id before the i18n lookup. Keeps old
# DB rows that already store the friendly label rendering correctly too.
_ALLERGEN_LABEL_TO_ID = {
    "gluten / wheat": "gluten", "gluten": "gluten", "wheat": "gluten",
    "dairy / lactose": "dairy", "dairy": "dairy", "lactose": "dairy", "milk": "dairy",
    "tree nuts": "tree_nut", "tree nut": "tree_nut",
    "peanuts": "peanut", "peanut": "peanut",
    "shellfish / crustaceans": "shellfish", "shellfish": "shellfish",
    "crustaceans": "shellfish",
    "mollusks": "mollusks", "molluscs": "mollusks",
    "eggs": "egg", "egg": "egg",
    "soy": "soy", "soya": "soy",
    "fish": "fish",
    "sesame": "sesame",
    "mustard": "mustard",
    "sulphites": "sulphites", "sulfites": "sulphites",
    "celery": "celery",
    "lupin": "lupin",
    "tomato": "tomato", "tomatoes": "tomato",
    "emmental cheese": "emmental", "emmental": "emmental",
    "rye": "rye",
    "rapeseed / canola oil": "rapeseed", "rapeseed": "rapeseed",
    "canola": "rapeseed", "canola oil": "rapeseed",
}

# UA meal-type labels — used only on the UA locale render path; EN uses the
# meal_type.* dict keys via t().
_MEAL_TYPE_UA = {
    "breakfast": "Сніданок",  # noqa: i18n
    "lunch": "Обід",  # noqa: i18n
    "dinner": "Вечеря",  # noqa: i18n
    "snack": "Перекус",  # noqa: i18n
}


# --- Shared helpers ---

def _format_ingredients(analysis: dict, locale: str = "en") -> list[str]:
    """Build ingredient list lines from analysis.ingredients.

    Each line shows ``• name — ~Ng · ~K kcal`` (or UA equivalents) when
    both grams and calories are present, gracefully degrading when either
    is missing.
    """
    ingredients = analysis.get("ingredients") or []
    if not ingredients:
        return []
    from lib.i18n import t
    g_unit = t("macro.gram_short", locale)
    kcal_unit = t("macro.calories_short", locale)
    lines = ["", t("meal.ingredients_header", locale)]
    for ing in ingredients:
        name = _esc(ing.get("name", "?"))
        grams = ing.get("estimated_grams")
        kcal = ing.get("estimated_calories")
        parts = []
        if grams:
            parts.append(f"~{round(float(grams))}{g_unit}")
        if kcal:
            try:
                parts.append(f"~{round(float(kcal))} {kcal_unit}")
            except (TypeError, ValueError):
                pass
        if parts:
            lines.append(f"  • {name} — {' · '.join(parts)}")
        else:
            lines.append(f"  • {name}")
    return lines


def _format_warnings(analysis: dict, locale: str = "en") -> list[str]:
    """Build allergen + Crohn warning lines."""
    from lib.i18n import t
    lines = []
    allergen_flags = analysis.get("allergen_flags") or []
    crohn_flags = analysis.get("crohn_flags") or []

    if allergen_flags:
        lines.append("")
        lines.append(t("meal.warnings_allergen_header", locale))
        for a in allergen_flags:
            icon = _CONFIDENCE_ICON.get((a.get("confidence") or "").lower(), "⚠️")
            # `allergen` is a canonical English ID ("egg", "gluten", …);
            # localise via i18n, falling back to the capitalised id when the
            # dictionary doesn't have an entry (legacy rows, unknown IDs).
            allergen_raw = str(a.get("allergen") or "").strip().lower()
            # Defensive: the LLM occasionally emits the friendly English label
            # ("gluten / wheat") instead of the canonical id ("gluten").
            # Normalise via the reverse-map before the i18n lookup.
            allergen_id = _ALLERGEN_LABEL_TO_ID.get(allergen_raw, allergen_raw)
            i18n_key = f"health.allergens.{allergen_id}"
            translated = t(i18n_key, locale) if allergen_id else "?"
            if translated == i18n_key:
                # No dict entry — last-resort: capitalise whatever we got
                # so it at least renders cleanly.
                translated = (allergen_raw or allergen_id).capitalize() or "?"
            allergen_name = _esc(translated)
            confidence = _esc(a.get("confidence", "?"))
            ingredient = _esc(a.get("ingredient",
                                    t("meal.warnings_default_ingredient", locale)))
            lines.append(t(
                "meal.warnings_allergen_line", locale,
                icon=icon, name=allergen_name, conf=confidence, ing=ingredient,
            ))

    if crohn_flags:
        lines.append("")
        lines.append(t("meal.warnings_health_header", locale))
        default_concern = t("meal.warnings_default_concern", locale)
        for c in crohn_flags:
            icon = _SEVERITY_ICON.get((c.get("severity") or "").lower(), "🟡")
            concern = _esc(c.get("concern", default_concern))
            ingredient = _esc(c.get("ingredient", "?"))
            lines.append(f"  {icon} {concern} ({ingredient})")

    return lines


def _format_nutrition_line(nutrition: dict, locale: str = "en") -> str:
    """Single-line kcal + macros readout. EN: '500 kcal | 30g P | 50g C | 18g F'."""
    from lib.i18n import t
    return t(
        "meal.nutrition_line", locale,
        cal=round(nutrition.get("calories", 0)),
        kcal_unit=t("macro.calories_short", locale),
        p=round(nutrition.get("protein_g", 0)),
        c=round(nutrition.get("carbs_g", 0)),
        f=round(nutrition.get("fat_g", 0)),
        g_unit=t("macro.gram_short", locale),
        p_short=t("macro.protein_short", locale),
        c_short=t("macro.carbs_short", locale),
        f_short=t("macro.fat_short", locale),
    )


_GI_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _format_glycemic_line(analysis: dict, locale: str = "en") -> str | None:
    gi = analysis.get("glycemic_index") or {}
    level = (gi.get("level") or "").lower()
    note = (gi.get("note") or "").strip()
    if not level:
        return None
    from lib.i18n import t
    icon = _GI_ICON.get(level, "🩸")
    # gi.* values are static literals; the level fallback is whatever the LLM
    # returned, so escape it. Note is free-form LLM text and must be escaped.
    if level in ("low", "medium", "high"):
        label = t(f"gi.{level}", locale)
    else:
        label = _esc(level.capitalize())
    safe_note = _esc(note)
    return f"{icon} {label}" + (f" — {safe_note}" if safe_note else "")


# --- Preview (before user accepts) ---

def format_meal_preview(meal_type: str, analysis: dict, locale: str = "en") -> str:
    """Preview message shown after AI analysis, before user taps Accept."""
    from lib.i18n import t
    dish = _esc(analysis.get("dish_name") or t("meal.default_name", locale))
    # meal_type comes from a callback allowlist; fall back through _esc anyway.
    meal_label_key = f"meal_type.{meal_type.lower()}"
    meal_label = t(meal_label_key, locale) if meal_type.lower() in (
        "breakfast", "lunch", "dinner", "snack"
    ) else _esc(meal_type.capitalize())
    nutrition = analysis.get("nutrition", {}) or {}

    lines = [
        t("meal.preview_header", locale, dish=dish),
        t("meal.preview_meal_line", locale, meal=meal_label),
    ]

    lines.extend(_format_ingredients(analysis, locale))
    lines.append("")
    lines.append(_format_nutrition_line(nutrition, locale))
    gi_line = _format_glycemic_line(analysis, locale)
    if gi_line:
        lines.append(gi_line)
    lines.extend(_format_warnings(analysis, locale))

    lines.append("")
    lines.append(t("meal.preview_confirm_prompt", locale))
    return "\n".join(lines)


# --- Final confirmation (after Accept) ---

def format_meal_logged(
    meal_type: str,
    analysis: dict,
    today_log: dict,
    daily_cal_target: int,
    first_name: str | None = None,
    locale: str = "en",
    health_profile: dict | None = None,
) -> str:
    """Minimal post-save confirmation: dish, meal type, day total.

    Allergen + Crohn warnings are gated on the user's stored
    ``user_health_profile``: only rendered if the user has the matching
    context configured (allergens list non-empty for allergen warnings;
    ``"crohns"`` in conditions for crohn warnings). Users with no health
    profile see only the three core lines — the AI may still emit flags
    in its JSON but they're suppressed at the display layer.

    Ingredients / nutrition / glycemic / overall_assessment / date /
    encouragement were dropped 2026-04-30 to reduce message volume.
    The fields are still produced by the AI and persisted in the
    ``meals`` table for ``/yesterday``, recap, and audit lookups.
    """
    from lib.i18n import t
    dish = _esc(analysis.get("dish_name") or t("meal.default_name", locale))
    meal_label = t(f"meal_type.{meal_type.lower()}", locale) if meal_type.lower() in (
        "breakfast", "lunch", "dinner", "snack"
    ) else _esc(meal_type.capitalize())

    lines = [
        t("meal.logged_header", locale, dish=dish),
        t("meal.preview_meal_line", locale, meal=meal_label),
    ]

    # Safety warnings — only when the user has actually configured the
    # corresponding health context. Without this gate the AI's generic
    # `crohn_flags` field (the schema labels it "noteworthy health
    # concerns") leaks Crohn-flavoured warnings to users who don't have
    # the condition.
    if health_profile:
        user_allergens = health_profile.get("allergens") or []
        user_conditions = health_profile.get("conditions") or []
        scoped = dict(analysis)
        if not user_allergens:
            scoped["allergen_flags"] = []
        if "crohns" not in user_conditions:
            scoped["crohn_flags"] = []
        lines.extend(_format_warnings(scoped, locale))

    lines.append(t(
        "meal.day_total", locale,
        cal=round(today_log.get("calories", 0)),
        target=daily_cal_target,
        kcal_unit=t("macro.calories_short", locale),
    ))

    return "\n".join(lines)


# --- Meal management list ---

def format_meals_list(
    meals: list[dict],
    log: dict | None = None,
    daily_cal_target: int | None = None,
    macros: dict | None = None,
    locale: str = "en",
) -> str:
    """List today's meals with IDs for edit/delete (UA + EN).

    If ``log``, ``daily_cal_target`` and ``macros`` are supplied, a compact
    calorie + macro header is prepended.
    """
    from lib.i18n import t
    if not meals:
        return t("meals_list.empty", locale)

    g = t("macro.gram_short", locale)
    kcal_unit = t("macro.calories_short", locale)
    p_short = t("macro.protein_short", locale)
    c_short = t("macro.carbs_short", locale)
    f_short = t("macro.fat_short", locale)

    lines: list[str] = []
    if log and daily_cal_target and macros:
        cal = log.get("calories", 0) or 0
        p = log.get("protein", 0) or 0
        c = log.get("carbs", 0) or 0
        f = log.get("fat", 0) or 0
        lines.append(t(
            "meals_list.day_total", locale,
            cal=round(cal), target=daily_cal_target,
            kcal_unit=kcal_unit, pct=_pct(cal, daily_cal_target),
        ))
        lines.append(t(
            "meals_list.macros_line", locale,
            p=round(p), p_target=macros['protein'],
            c=round(c), c_target=macros['carbs'],
            f=round(f), f_target=macros['fat'],
            g=g,
        ))
        lines.append("")

    lines.append(t("meals_list.header", locale))
    lines.append("")
    for i, m in enumerate(meals, 1):
        mt_raw = (m.get("meal_type") or "").lower()
        mt = t(f"meal_type.{mt_raw}", locale) if mt_raw in ("breakfast", "lunch", "dinner", "snack") else ""
        desc = _esc((m.get("description") or "")[:50])
        cal = round(m.get("calories", 0))
        p = round(m.get("protein_g", 0))
        c = round(m.get("carbs_g", 0))
        f = round(m.get("fat_g", 0))
        lines.append(t("meals_list.row_meal", locale, i=i, meal=mt, desc=desc))
        lines.append(t(
            "meals_list.row_macros", locale,
            cal=cal, kcal_unit=kcal_unit,
            p=p, c=c, f=f, g=g,
            p_short=p_short, c_short=c_short, f_short=f_short,
        ))
        lines.append("")

    lines.append(t("meals_list.footer", locale))
    return "\n".join(lines)


# --- Today progress ---

_WELCOME_VARIANT_KEYS = [f"welcome.variant_{i}" for i in range(1, 7)]


def welcome_message(first_name: str | None = None, locale: str = "en") -> str:
    from lib.i18n import t
    name = _name_or_default(first_name)
    return t(random.choice(_WELCOME_VARIANT_KEYS), locale=locale, name=name)


# --- Onboarding ---

# Onboarding strings migrated to lib/i18n/dict_*.json (F-2b Phase 2).
# Use ``api.webhook._t("onboarding.foo", profile)`` at call sites.
# Also includes the timezone substep (F-3) under "onboarding.tz_*".

# TIMEZONE_* + HEALTH_* constants migrated to lib/i18n keys
# (timezone.* / health_view.*) and removed in F-2b Chunk 8 (G1).
# Callers in api/webhook.py use _t("timezone.foo", profile) etc.


def _sex_ua(sex: str, locale: str = "uk") -> str:
    """Return localized sex label. Default 'uk' preserves legacy behaviour
    for callers that don't pass a locale yet."""
    if locale == "en":
        return {"male": "male", "female": "female"}.get(sex, sex or "—")
    return {"male": "чоловіча", "female": "жіноча"}.get(sex, sex or "—")  # noqa: i18n


def _goal_ua(goal: str, locale: str = "uk") -> str:
    if locale == "en":
        return {
            "lose": "lose weight",
            "maintain": "maintain weight",
            "gain": "build muscle",
        }.get(goal, goal or "—")
    return {
        "lose": "схуднути",  # noqa: i18n
        "maintain": "підтримувати вагу",  # noqa: i18n
        "gain": "набрати м'язи",  # noqa: i18n
    }.get(goal, goal or "—")


def _gym_ua(freq: str, locale: str = "uk") -> str:
    if locale == "en":
        mapping = {
            "0": "0 times",
            "1-2": "1–2 times",
            "3-4": "3–4 times",
            "5-6": "5–6 times",
            "7": "7 times",
        }
    else:
        mapping = {
            "0": "0 разів",  # noqa: i18n
            "1-2": "1–2 рази",  # noqa: i18n
            "3-4": "3–4 рази",  # noqa: i18n
            "5-6": "5–6 разів",  # noqa: i18n
            "7": "7 разів",  # noqa: i18n
        }
    return mapping.get(freq, freq or "—")


def format_recommendation(profile: dict, recommended: int, locale: str = "en") -> str:
    from lib.i18n import t
    weight = profile.get("weight_kg") or 0
    goal = profile.get("goal") or "maintain"
    macros = macro_gram_targets_from_profile(weight, goal)
    age_v   = profile.get("age", "—")
    sex_v   = _sex_ua(profile.get("sex", ""), locale=locale)
    wt_v    = profile.get("weight_kg", "—")
    ht_v    = profile.get("height_cm", "—")
    gym_v   = _gym_ua(profile.get("gym_per_week", ""), locale=locale)
    goal_v  = _goal_ua(profile.get("goal", ""), locale=locale)
    return (
        t("recommendation.header", locale=locale) + "\n\n"
        + t("recommendation.age",    locale=locale, value=age_v) + "\n"
        + t("recommendation.sex",    locale=locale, value=sex_v) + "\n"
        + t("recommendation.weight", locale=locale, value=wt_v) + "\n"
        + t("recommendation.height", locale=locale, value=ht_v) + "\n"
        + t("recommendation.gym",    locale=locale, value=gym_v) + "\n"
        + t("recommendation.goal",   locale=locale, value=goal_v) + "\n\n"
        + t("recommendation.daily_norm", locale=locale, cal=recommended) + "\n"
        + t("recommendation.macros", locale=locale, p=macros["protein"], c=macros["carbs"], f=macros["fat"]) + "\n\n"
        + t("recommendation.footer", locale=locale)
    )


def format_new_user_notification(
    profile: dict,
    username: str | None = None,
    first_name: str | None = None,
) -> str:
    """Admin-channel post for a freshly-onboarded user. English, compact, HTML."""
    goal_lbl = {
        "lose":     "🔥 Lose",
        "maintain": "⚖️ Maintain",
        "gain":     "💪 Gain",
    }.get(profile.get("goal") or "", "—")
    sex_lbl = {"male": "♂", "female": "♀"}.get(profile.get("sex") or "", "—")
    user_id = profile.get("user_id") or "—"
    name = first_name or "—"
    handle = ("@" + username) if username else "—"
    tw = profile.get("target_weight_kg")
    wd = profile.get("weekly_delta_kg")

    lines = [
        "🆕 <b>New user onboarded</b>",
        f"👤 {_esc(name)} ({_esc(handle)}, id <code>{_esc(user_id)}</code>)",
        f"🎯 Goal: {goal_lbl}"
        + (f" → target <b>{_esc(tw)} kg</b>" if tw else ""),
        f"⚖️ Weight: <b>{_esc(profile.get('weight_kg', '—'))} kg</b> · "
        f"📏 Height: <b>{_esc(profile.get('height_cm', '—'))} cm</b>",
        f"🎂 Age: <b>{_esc(profile.get('age', '—'))}</b> · {sex_lbl} · "
        f"🏋️ Gym: <b>{_esc(profile.get('gym_per_week', '—'))}/week</b>",
        f"🔥 Calorie target: <b>{_esc(profile.get('daily_calorie_target', '—'))} kcal/day</b>",
    ]
    if wd:
        lines.append(f"📈 Weekly delta: <b>{wd:+.2f} kg/week</b>")
    return "\n".join(lines)


def format_profile(profile: dict, locale: str = "en") -> str:
    from lib.i18n import t
    if not profile:
        return t("profile.empty", locale)
    target = profile.get("daily_calorie_target") or 0
    rec = profile.get("recommended_calorie_target") or 0
    weight = profile.get("weight_kg")
    goal = profile.get("goal")
    if weight and goal:
        macros = macro_gram_targets_from_profile(weight, goal)
    else:
        macros = macro_gram_targets(target) if target else {"protein": 0, "carbs": 0, "fat": 0}

    g = t("macro.gram_short", locale)
    kcal_unit = t("macro.calories_short", locale)
    sep = "━━━━━━━━━━━━━━━━━━━━━"

    # kg unit is locale-specific (UA / EN), distinct from g (gram).
    kg_unit = "кг" if locale == "uk" else "kg"  # noqa: i18n

    lines = [
        t("profile.header", locale),
        sep,
        t("profile.age",    locale, v=profile.get("age", "—")),
        t("profile.sex",    locale, v=_sex_ua(profile.get("sex", ""), locale)),
        t("profile.weight", locale, v=profile.get("weight_kg", "—")),
        t("profile.height", locale, v=profile.get("height_cm", "—")),
        t("profile.gym",    locale, v=_gym_ua(profile.get("gym_per_week", ""), locale)),
        t("profile.goal",   locale, v=_goal_ua(profile.get("goal", ""), locale)),
    ]
    tw = profile.get("target_weight_kg")
    if tw and goal in ("lose", "gain") and weight:
        delta = float(weight) - float(tw)
        if goal == "lose":
            togo = max(0.0, delta)
            arrow = "—" if togo <= 0.05 else f"-{togo:.1f} {kg_unit}"
        else:
            togo = max(0.0, -delta)
            arrow = "—" if togo <= 0.05 else f"+{togo:.1f} {kg_unit}"
        if togo <= 0.05:
            lines.append(t("profile.target_reached", locale, tw=tw))
        else:
            lines.append(t("profile.target_with_arrow", locale, tw=tw, arrow=arrow))
    elif tw:
        lines.append(t("profile.target_simple", locale, tw=tw))

    lines.append(sep)
    if rec and rec != target:
        lines.append(t("profile.daily_norm_with_rec", locale, cal=target, kcal_unit=kcal_unit, rec=rec))
    else:
        lines.append(t("profile.daily_norm", locale, cal=target, kcal_unit=kcal_unit))
    lines.append(t(
        "profile.macro_targets", locale,
        p=macros['protein'], c=macros['carbs'], f=macros['fat'], g=g,
    ))
    lines.append("")
    lines.append(t("profile.edit_hint", locale))
    lines.append("")
    lines.append(t("profile.docs", locale))
    return "\n".join(lines)


# ONBOARDING_REQUIRED migrated to "onboarding.required" key in lib/i18n.


def help_message(locale: str = "en") -> str:
    """Localized /help text. Single key with the entire message body."""
    from lib.i18n import t
    return t("help.full", locale=locale)


def _streak_word_uk(n: int) -> str:
    """Ukrainian plural for the streak word — 1/2-4/5+ Slavic forms.
    Slavic 11-14 exception applies. EN side uses pluralize_en in lib/i18n/plurals."""
    n = abs(int(n))
    if 11 <= (n % 100) <= 14:
        return "днів"  # noqa: i18n
    last = n % 10
    if last == 1:
        return "день"  # noqa: i18n
    if 2 <= last <= 4:
        return "дні"  # noqa: i18n
    return "днів"  # noqa: i18n


def _format_streak_line(streak: dict | None, locale: str = "en") -> str | None:
    """Return the /today header streak line, or None if no streak to show."""
    if not streak:
        return None
    cur = int(streak.get("current_streak") or 0)
    if cur < 1:
        return None
    freezes = int(streak.get("freeze_days_remaining") or 0)
    from lib.i18n import t
    from lib.i18n.plurals import pluralize
    word = pluralize(
        cur, locale,
        t("streak.day_singular", locale),
        t("streak.day_few",      locale),
        t("streak.day_many",     locale),
    )
    return t("streak.today_line", locale, n=cur, word=word, freezes=freezes)


def format_today_progress(
    log: dict,
    daily_cal_target: int,
    first_name: str | None = None,
    profile: dict | None = None,
    streak: dict | None = None,
) -> str:
    from lib.i18n import t
    from lib.datehelpers import format_date_long

    locale = (profile or {}).get("lang") or "en"
    if locale not in ("en", "uk"):
        locale = "en"

    date_display = format_date_long(datetime.now(LOCAL_TZ), locale)
    if profile and profile.get("weight_kg") and profile.get("goal"):
        macros = macro_gram_targets_from_profile(profile["weight_kg"], profile["goal"])
    else:
        macros = macro_gram_targets(daily_cal_target)
    cal = log.get("calories", 0)
    p = log.get("protein", 0)
    c = log.get("carbs", 0)
    f = log.get("fat", 0)
    fib = log.get("fiber", 0)
    sug = log.get("sugar", 0)
    meals = log.get("meal_count", 0)
    remaining = max(0, daily_cal_target - cal)
    name = _name_or_default(first_name)

    if meals == 0:
        quip = t("today.quip_empty", locale)
    elif cal < daily_cal_target * 0.5:
        quip = t("today.quip_low", locale)
    elif cal < daily_cal_target * 0.9:
        quip = t("today.quip_mid", locale)
    elif cal <= daily_cal_target * 1.05:
        quip = t("today.quip_target", locale)
    else:
        quip = t("today.quip_over", locale)

    streak_line = _format_streak_line(streak, locale)
    streak_block = f"{streak_line}\n" if streak_line else ""

    g = t("macro.gram_short", locale)
    kcal_unit = t("macro.calories_short", locale)
    sep = "━━━━━━━━━━━━━━━━━━━━━"

    return (
        f"{t('today.header', locale, date=date_display)}\n"
        f"{sep}\n"
        f"{t('today.user_line', locale, name=name)}\n"
        f"{streak_block}"
        f"{t('today.cal_line', locale, cur=round(cal), target=daily_cal_target, pct=_pct(cal, daily_cal_target))}\n"
        f"   {_bar(cal, daily_cal_target)}\n"
        f"{t('today.protein_line', locale, cur=round(p), target=macros['protein'], pct=_pct(p, macros['protein']), g=g)}\n"
        f"   {_bar(p, macros['protein'])}\n"
        f"{t('today.carbs_line', locale, cur=round(c), target=macros['carbs'], pct=_pct(c, macros['carbs']), g=g)}\n"
        f"   {_bar(c, macros['carbs'])}\n"
        f"{t('today.fat_line', locale, cur=round(f), target=macros['fat'], pct=_pct(f, macros['fat']), g=g)}\n"
        f"   {_bar(f, macros['fat'])}\n"
        f"{t('today.fiber_line', locale, cur=round(fib), g=g)}\n"
        f"{t('today.sugar_line', locale, cur=round(sug), g=g)}\n"
        f"{sep}\n"
        f"{t('today.meal_count', locale, n=meals)}\n"
        f"{t('today.remaining', locale, n=round(remaining), kcal_unit=kcal_unit)}\n\n"
        f"<i>{quip}</i>"
    )


def format_goals(
    profile: dict | None,
    projection,                 # lib.goals.Projection (avoid circular import)
    actual_weekly_delta: float | None = None,
    status: str | None = None,  # "ahead" | "on_track" | "behind"
    first_name: str | None = None,
    locale: str = "en",
) -> str:
    """Render the /goals command response (UA + EN, F-2b)."""
    from lib.i18n import t

    name = _name_or_default(first_name)
    if not profile:
        return t("goals.no_profile", locale)

    current = profile.get("weight_kg")
    target = profile.get("target_weight_kg")
    goal = profile.get("goal") or "maintain"
    weekly_delta = projection.weekly_delta_kg

    sep = "━━━━━━━━━━━━━━━━━━━━━"
    lines = [t("goals.header", locale, name=name), sep]
    if current is not None:
        lines.append(t("goals.current_weight", locale, w=f"{float(current):.1f}"))
    lines.append(
        t("goals.target_weight", locale, w=f"{float(target):.1f}")
        if target is not None else t("goals.target_unset", locale)
    )

    if goal == "maintain":
        lines.append(t("goals.maintain_label", locale))
    elif weekly_delta:
        lines.append(t("goals.weekly_label", locale, delta=weekly_delta))
    else:
        lines.append(t("goals.weekly_unset", locale))

    # Projection block.
    lines.append(sep)
    if projection.reason == "ok":
        weeks = projection.weeks_to_goal or 0
        d = projection.projected_date
        date_str = f"{d.day:02d}.{d.month:02d}.{d.year}" if d else "—"
        lines.append(t("goals.weeks_to_goal", locale, weeks=f"{weeks:g}"))
        lines.append(t("goals.projected_date", locale, date=date_str))
    elif projection.reason == "at_target":
        lines.append(t("goals.projection_at_target", locale))
    elif projection.reason == "no_target":
        lines.append(t("goals.no_target", locale))
    elif projection.reason == "zero_delta":
        lines.append(t("goals.projection_zero_delta", locale))
    elif projection.reason == "wrong_direction":
        lines.append(t("goals.projection_wrong_direction", locale))
    elif projection.reason == "no_current":
        lines.append(t("goals.no_current_weight", locale))

    # Status from actual progress (when available).
    if status == "ahead":
        lines.append(t("goals.status_ahead", locale))
    elif status == "on_track":
        lines.append(t("goals.status_on_track", locale))
    elif status == "behind":
        lines.append(t("goals.status_behind", locale))
    if actual_weekly_delta is not None:
        lines.append(t("goals.actual_line", locale, delta=actual_weekly_delta))

    return "\n".join(lines)


def format_projection_line(
    projection,
    status: str | None = None,
    locale: str = "en",
) -> str | None:
    """One-line projection summary for the Monday weigh-in reply (UA + EN)."""
    if projection.reason != "ok":
        return None
    d = projection.projected_date
    if d is None:
        return None
    from lib.i18n import t
    date_str = f"{d.day:02d}.{d.month:02d}.{d.year}"
    weeks = projection.weeks_to_goal or 0
    key = "goals.projection_line." + (status if status in ("ahead", "behind", "on_track") else "neutral")
    return t(key, locale, date=date_str, weeks=f"{weeks:g}")


# F-9 menu OCR / F-8 barcode / F-10 meal plan / F-11 fridge constants were
# all dead code at this point — the strings live in lib/i18n/dict_*.json
# (menu.* / barcode.* / plan.* / fridge.* / suggest.*) and callers in
# api/webhook.py use _t("menu.foo", profile) directly. Removed in F-2b
# Chunk 8 (G3).


def format_menu_dishes_intro(n: int, locale: str = "en") -> str:
    from lib.i18n import t
    return t("menu.results_header", locale, n=n)


def format_menu_dish_row(dish: dict, locale: str = "en") -> str:
    """Single-dish line for the menu results message."""
    from lib.i18n import t
    name = dish.get("name", "")
    kcal = int(round(float(dish.get("calories")  or 0)))
    p    = int(round(float(dish.get("protein_g") or 0)))
    f    = int(round(float(dish.get("fat_g")     or 0)))
    c    = int(round(float(dish.get("carbs_g")   or 0)))
    portion = (dish.get("portion_note") or "").strip()
    portion_part = t("menu.dish_row_portion", locale, portion=portion) if portion else ""
    return t(
        "menu.dish_row", locale,
        name=name, kcal=kcal,
        kcal_unit=t("macro.calories_short", locale),
        p=p, f=f, c=c,
        p_short=t("macro.protein_short", locale),
        f_short=t("macro.fat_short",     locale),
        c_short=t("macro.carbs_short",   locale),
        portion_part=portion_part,
    )




def format_meal_plan_day(day: dict, day_idx: int, locale: str = "en") -> str:
    """Render one day's slots as a single Telegram message body (UA + EN)."""
    from lib.i18n import t
    # Model emits English tokens for date_label ("Today" / "Tomorrow" / "Day 3");
    # translate per user locale here.
    raw = (day.get("date_label") or "").strip()
    label_map = {"Today": "meal_plan.day_today", "Tomorrow": "meal_plan.day_tomorrow", "Day 3": "meal_plan.day_3"}
    label = t(label_map[raw], locale) if raw in label_map else raw
    lines = [t("plan.day_header", locale, label=label)]
    slot_emojis = {"breakfast": "🥣", "lunch": "🍱", "dinner": "🍽️", "snack": "🍎"}
    kcal_unit = t("macro.calories_short", locale)
    p_short   = t("macro.protein_short",  locale)
    f_short   = t("macro.fat_short",      locale)
    c_short   = t("macro.carbs_short",    locale)
    for slot_key in ("breakfast", "lunch", "dinner", "snack"):
        slot = day["slots"].get(slot_key)
        if not slot:
            continue
        emoji = slot_emojis[slot_key]
        label = t(f"meal_type.{slot_key}", locale)
        kcal = int(round(float(slot.get("calories")  or 0)))
        p    = int(round(float(slot.get("protein_g") or 0)))
        f    = int(round(float(slot.get("fat_g")     or 0)))
        c    = int(round(float(slot.get("carbs_g")   or 0)))
        recipe = slot.get("recipe", "")
        lines.append(t(
            "meal_plan.slot_row", locale,
            emoji=emoji, label=label, kcal=kcal,
            kcal_unit=kcal_unit,
            p=p, f=f, c=c,
            p_short=p_short, f_short=f_short, c_short=c_short,
            name=slot["name"],
        ))
        if recipe:
            lines.append(t("meal_plan.slot_recipe", locale, recipe=recipe))
    return "\n".join(lines)


def format_aliases(
    aliases: list[dict],
    first_name: str | None = None,
    locale: str = "en",
) -> str:
    """F-7: render the /aliases command response (UA + EN, F-2b).

    ``aliases`` is the list returned by :func:`lib.personalization.recent_aliases`.
    Empty list → friendly placeholder explaining how the bot learns.
    """
    from lib.i18n import t

    name = _name_or_default(first_name)
    sep = "━━━━━━━━━━━━━━━━━━━━━"
    header = t("aliases.header", locale, name=name)

    if not aliases:
        return f"{header}\n{sep}\n" + t("aliases.empty", locale)

    # Localized gram + kcal units inline. Tiny so we don't push them through dict.
    g_unit = "г" if locale == "uk" else "g"  # noqa: i18n

    lines = [header, sep, t("aliases.intro", locale), ""]
    for a in aliases[:12]:
        kcal = int(round(float(a.get("default_kcal") or 0)))
        grams = float(a.get("default_grams") or 0)
        portion = f"~{int(round(grams))}{g_unit} · " if grams > 0 else ""
        samples = int(a.get("sample_count") or 0)
        sample_tag = f" <i>({samples}×)</i>" if samples > 1 else ""
        # Reuse the row template — kcal unit pre-formatted so the template
        # stays language-neutral.
        kcal_unit = "ккал" if locale == "uk" else "kcal"  # noqa: i18n
        row = (
            f"• <b>{a.get('normalized_name', a.get('alias', ''))}</b> — "
            f"{portion}{kcal} {kcal_unit}{sample_tag}"
        )
        lines.append(row)
    if len(aliases) > 12:
        lines.append(t("aliases.more", locale, n=len(aliases) - 12))
    return "\n".join(lines)


def format_alternates_intro(meal_type: str, candidates: list[dict], locale: str = "en") -> str:
    """F-6: header shown above the alternates keyboard when the photo is ambiguous.

    Lists the candidates as a quick legend so the user can compare numbers
    before tapping a button.
    """
    from lib.i18n import t
    label_key = f"meal_type.{meal_type}" if meal_type else "meal_type.fallback"
    label = t(label_key, locale)
    if label == label_key:  # missing key → fallback to a known meal_type label
        label = t("meal_type.fallback", locale)
    kcal_unit = t("macro.calories_short", locale)
    p_short   = t("macro.protein_short",  locale)
    f_short   = t("macro.fat_short",      locale)
    c_short   = t("macro.carbs_short",    locale)
    lines = [
        t("alternates.header", locale, label=label),
        t("alternates.choose", locale),
        "",
    ]
    digits = ("1⃣", "2⃣", "3⃣")
    for i, c in enumerate(candidates[:3]):
        kcal = int(round(float(c.get("calories")  or 0)))
        p    = int(round(float(c.get("protein_g") or 0)))
        cb   = int(round(float(c.get("carbs_g")   or 0)))
        f    = int(round(float(c.get("fat_g")     or 0)))
        conf = int(round(float(c.get("confidence") or 0) * 100))
        lines.append(t(
            "alternates.row", locale,
            digit=digits[i], name=c.get("name", ""), kcal=kcal,
            kcal_unit=kcal_unit,
            p=p, f=f, c=cb,
            p_short=p_short, f_short=f_short, c_short=c_short,
            conf=conf,
        ))
    return "\n".join(lines)


def format_streak_summary(
    streak: dict | None,
    first_name: str | None = None,
    locale: str = "en",
) -> str:
    """Render the /streak command response.

    ``streak`` is the row dict from :func:`lib.database.get_streak`, or ``None``
    when the user has never logged a meal.
    """
    from lib.i18n import t
    from lib.i18n.plurals import pluralize

    name = _name_or_default(first_name)
    header = t("streak.summary_header", locale, name=name)
    sep = "━━━━━━━━━━━━━━━━━━━━━"

    if not streak or int(streak.get("current_streak") or 0) < 1:
        return f"{header}\n{sep}\n" + t("streak.summary_empty", locale)

    cur = int(streak.get("current_streak") or 0)
    longest = int(streak.get("longest_streak") or 0)
    freezes = int(streak.get("freeze_days_remaining") or 0)
    last = streak.get("last_log_date") or "—"

    def _word(n: int) -> str:
        return pluralize(
            n, locale,
            t("streak.day_singular", locale),
            t("streak.day_few",      locale),
            t("streak.day_many",     locale),
        )

    return "\n".join([
        header,
        sep,
        t("streak.summary_current", locale, cur=cur, word=_word(cur)),
        t("streak.summary_longest", locale, longest=longest, word=_word(longest)),
        t("streak.summary_freezes", locale, freezes=freezes),
        t("streak.summary_last_log", locale, last=last),
        "",
        t("streak.summary_freeze_note", locale),
    ])


def format_yesterday(
    log: dict,
    meals: list[dict],
    daily_cal_target: int,
    first_name: str | None = None,
    profile: dict | None = None,
) -> str:
    """Yesterday's progress + meal list in one message."""
    from lib.i18n import t
    from lib.datehelpers import format_date_long

    locale = (profile or {}).get("lang") or "en"
    if locale not in ("en", "uk"):
        locale = "en"

    date_str = log.get("date", "")
    try:
        date_display = format_date_long(datetime.strptime(date_str, "%Y-%m-%d"), locale)
    except Exception:
        date_display = date_str

    cal = log.get("calories", 0)
    p = log.get("protein", 0)
    c = log.get("carbs", 0)
    f = log.get("fat", 0)
    fib = log.get("fiber", 0)
    sug = log.get("sugar", 0)
    meal_count = log.get("meal_count", 0)
    name = _name_or_default(first_name)

    if meal_count == 0:
        return (
            f"{t('yesterday.header', locale, date=date_display)}\n"
            f"{t('yesterday.empty', locale)}"
        )

    g = t("macro.gram_short", locale)
    kcal_unit = t("macro.calories_short", locale)

    meal_lines = []
    for m in meals:
        mt_raw = (m.get("meal_type") or "").lower()
        if mt_raw in ("breakfast", "lunch", "dinner", "snack"):
            mt = t(f"meal_type.{mt_raw}", locale)
        else:
            mt = _esc(mt_raw.capitalize() or "—")
        desc = _esc((m.get("description") or "")[:60])
        meal_lines.append(t(
            "yesterday.meal_row", locale,
            meal_type=mt, desc=desc,
            cal=round(m.get("calories", 0)), kcal_unit=kcal_unit,
        ))
    meal_section = "\n".join(meal_lines)

    if profile and profile.get("weight_kg") and profile.get("goal"):
        macros = macro_gram_targets_from_profile(profile["weight_kg"], profile["goal"])
    else:
        macros = macro_gram_targets(daily_cal_target)
    sep = "━━━━━━━━━━━━━━━━━━━━━"
    return (
        f"{t('yesterday.header', locale, date=date_display)}\n"
        f"{sep}\n"
        f"{t('today.user_line', locale, name=name)}\n"
        f"{t('yesterday.cal_line', locale, cur=round(cal), target=daily_cal_target, pct=_pct(cal, daily_cal_target))}\n"
        f"   {_bar(cal, daily_cal_target)}\n"
        f"{t('yesterday.protein_line', locale, cur=round(p), target=macros['protein'], g=g)}\n"
        f"   {_bar(p, macros['protein'])}\n"
        f"{t('yesterday.carbs_line', locale, cur=round(c), target=macros['carbs'], g=g)}\n"
        f"   {_bar(c, macros['carbs'])}\n"
        f"{t('yesterday.fat_line', locale, cur=round(f), target=macros['fat'], g=g)}\n"
        f"   {_bar(f, macros['fat'])}\n"
        f"{t('today.fiber_line', locale, cur=round(fib), g=g)}\n"
        f"{t('today.sugar_line', locale, cur=round(sug), g=g)}\n"
        f"{sep}\n"
        f"{t('yesterday.meals_header', locale, n=meal_count)}\n"
        f"{meal_section}"
    )


def format_history(rows: list[dict], daily_cal_target: int, locale: str = "en") -> str:
    from lib.i18n import t
    if not rows:
        return t("history.empty", locale=locale)

    kcal_unit = t("macro.calories_short", locale=locale)
    p_short   = t("macro.protein_short",  locale=locale)
    c_short   = t("macro.carbs_short",    locale=locale)
    f_short   = t("macro.fat_short",      locale=locale)

    lines = [t("history.header", locale=locale)]
    for r in rows:
        cal = r.get("calories", 0)
        p = r.get("protein", 0)
        c = r.get("carbs", 0)
        f = r.get("fat", 0)
        total_macro_cal = p * 4 + c * 4 + f * 9
        if total_macro_cal > 0:
            p_pct = round(100 * p * 4 / total_macro_cal)
            c_pct = round(100 * c * 4 / total_macro_cal)
            f_pct = round(100 * f * 9 / total_macro_cal)
        else:
            p_pct = c_pct = f_pct = 0

        if cal == 0:
            marker = ""
        elif cal > daily_cal_target * 1.05:
            marker = t("history.marker_over", locale=locale)
        elif cal < daily_cal_target * 0.80:
            marker = t("history.marker_under", locale=locale)
        else:
            marker = t("history.marker_ok", locale=locale)

        lines.append(t(
            "history.row", locale=locale,
            date=_ua_date_short(r.get("date", "")),
            cal=round(cal), kcal_unit=kcal_unit,
            p_short=p_short, p_pct=p_pct,
            c_short=c_short, c_pct=c_pct,
            f_short=f_short, f_pct=f_pct,
            marker=marker,
        ))
    lines.append("")
    lines.append(t("history.footer", locale=locale))
    return "\n".join(lines)


def format_day_detail(date: str, meals: list[dict], locale: str = "en") -> str:
    from lib.i18n import t
    date_str = _ua_date_short(date)
    if not meals:
        return t("day_detail.empty", locale=locale, date=date_str)

    kcal_unit = t("macro.calories_short", locale=locale)
    g_unit    = t("macro.gram_short",     locale=locale)
    p_short   = t("macro.protein_short",  locale=locale)
    c_short   = t("macro.carbs_short",    locale=locale)
    f_short   = t("macro.fat_short",      locale=locale)

    lines = [t("day_detail.header", locale=locale, date=date_str), ""]
    total_cal = 0
    for m in meals:
        total_cal += m.get("calories", 0)
        mt_raw = (m.get("meal_type") or "")
        mt = t(f"meal_type.{mt_raw.lower()}", locale=locale) if mt_raw else _esc(mt_raw.capitalize())
        if mt == f"meal_type.{mt_raw.lower()}":  # no key → fall back to capitalized raw
            mt = _esc(mt_raw.capitalize())
        desc = _esc(m.get("description", ""))
        lines.append(t("day_detail.meal_line", locale=locale, meal_type=mt, desc=desc))
        lines.append(t(
            "day_detail.macros_line", locale=locale,
            cal=round(m.get("calories", 0)), kcal_unit=kcal_unit,
            p=round(m.get("protein_g", 0)), c=round(m.get("carbs_g", 0)), f=round(m.get("fat_g", 0)),
            g=g_unit, p_short=p_short, c_short=c_short, f_short=f_short,
        ))
        if m.get("allergen_warnings"):
            names = ", ".join(_esc(a.get("allergen", "?")) for a in m["allergen_warnings"])
            lines.append(t("day_detail.allergens_line", locale=locale, names=names))
        lines.append("")

    lines.append(t("day_detail.total", locale=locale, total=round(total_cal), kcal_unit=kcal_unit))
    return "\n".join(lines)


# All short / weight / goals constants formerly here were dead code at this
# point — strings live in lib/i18n/dict_*.json under the matching namespaces
# (prompts.* / errors.* / weight.* / target_weight.* / goals.*) and callers
# in api/webhook.py use _t("...", profile). Removed in F-2b Chunk 8 (G3).

# --- Reply-keyboard button labels (must match the strings used in main_menu_keyboard) ---
# When a user taps one of these buttons, Telegram sends its label as a message.
# webhook.py intercepts these labels and routes them to the corresponding command.
#
# Telegram caches reply keyboards per chat for ~1 hour, so the dispatcher
# accepts BOTH locales' labels (UK + EN) plus a small legacy set for the
# older (pre-F-2b) UA labels. btn.* keys are stable; the bilingual dispatcher
# is permanent so users can /language flip without a transitional dead zone.
_BTN_NAMES: tuple[str, ...] = (
    "ask", "fav", "water", "today", "suggest", "profile",
    "yesterday", "meals", "dashboard", "scan", "menu_ocr",
    "recent",
)


def btn_label(name: str, locale: str = "en") -> str:
    """Return the localized reply-keyboard label for one of the 11 buttons."""
    from lib.i18n import t
    return t(f"btn.{name}", locale=locale)


# Legacy labels — kept in the lookup set so users whose phones still have the
# pre-F-2b reply keyboard cached don't get a "I don't understand" reaction
# when they tap. UA + EN current renders are the union of dispatched labels.
_LEGACY_BTN_LABELS: tuple[str, ...] = (
    "🔢 Сканер",  # noqa: i18n
    "📋 Меню",  # noqa: i18n
)


def menu_button_labels() -> set[str]:
    """All accepted reply-keyboard labels: UK + EN current + legacy.

    Used by the webhook dispatcher to decide whether an incoming text
    message is a button tap to route to a command.
    """
    labels: set[str] = set(_LEGACY_BTN_LABELS)
    for locale in ("uk", "en"):
        for name in _BTN_NAMES:
            labels.add(btn_label(name, locale=locale))
    return labels


def button_text_to_command(text: str) -> str | None:
    """Map a tapped button label (any locale) to its canonical /command.

    Returns None if the text doesn't match any known button.
    """
    # Build reverse map: label → command. Cached at first call.
    cache = button_text_to_command.__dict__.get("_cache")
    if cache is None:
        # AI menu merge: "ask" button (the keyboard "🤖 Ask AI" label)
        # now opens the combined /ai chooser instead of going straight to
        # /ask. /ask remains available as a typed slash command.
        name_to_cmd = {
            "ask": "/ai", "fav": "/fav", "meals": "/meals",
            "profile": "/profile", "suggest": "/suggest_meal",
            "scan": "/scan", "menu_ocr": "/menu",
            "today": "/today", "yesterday": "/yesterday",
            "recent": "/recent",
        }
        cache = {}
        for name, cmd in name_to_cmd.items():
            for locale in ("uk", "en"):
                cache[btn_label(name, locale=locale)] = cmd
        # Legacy labels mapped explicitly.
        cache["🔢 Сканер"] = "/scan"  # noqa: i18n
        cache["📋 Меню"] = "/menu"  # noqa: i18n
        button_text_to_command._cache = cache
    return cache.get(text)


# --- Water tracker ---

def format_water(total_ml: int, target_ml: int, locale: str = "en") -> str:
    from lib.i18n import t
    total_ml = max(0, int(total_ml))
    target_ml = max(1, int(target_ml))
    blocks = 10
    ratio = total_ml / target_ml
    filled = max(0, min(blocks, round(ratio * blocks)))
    bar = "▰" * filled + "▱" * (blocks - filled)
    total_l = total_ml / 1000
    target_l = target_ml / 1000
    pct = round(ratio * 100)
    header = t("water.header", locale=locale, total_l=f"{total_l:.2f}", target_l=f"{target_l:.1f}")
    if pct == 100:
        tail = t("water.bar_goal_hit", locale=locale, bar=bar)
    else:
        tail = t("water.bar_progress", locale=locale, bar=bar, pct=pct)
    return f"{header}\n{tail}"


def format_meal_list_entry(m: dict, locale: str = "en") -> str:
    from lib.i18n import t
    desc = (m.get("description") or "").strip()
    if len(desc) > 40:
        desc = desc[:38] + "…"
    cal = round(m.get("calories") or 0)
    star = "⭐ " if m.get("is_favorite") else ""
    return t(
        "meal_list_entry.line",
        locale=locale,
        star=star,
        desc=_esc(desc),
        cal=cal,
        kcal_unit=t("macro.calories_short", locale=locale),
    )
