"""Message formatting helpers for Telegram replies (Ukrainian, з гумором)."""
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
_UA_MONTHS_FULL = [
    "", "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]
_UA_MONTHS_SHORT = [
    "", "січ", "лют", "бер", "кві", "тра", "чер",
    "лип", "сер", "вер", "жов", "лис", "гру",
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
    name = first_name.strip() if (first_name and first_name.strip()) else "друже"
    return _esc(name)


_CONFIDENCE_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
_SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}

_MEAL_TYPE_UA = {
    "breakfast": "Сніданок",
    "lunch": "Обід",
    "dinner": "Вечеря",
    "snack": "Перекус",
}


# --- Shared helpers ---

def _format_ingredients(analysis: dict) -> list[str]:
    """Build ingredient list lines from analysis.ingredients."""
    ingredients = analysis.get("ingredients") or []
    if not ingredients:
        return []
    lines = ["", "📋 <b>Інгредієнти:</b>"]
    for ing in ingredients:
        name = _esc(ing.get("name", "?"))
        grams = ing.get("estimated_grams")
        if grams:
            lines.append(f"  • {name} — ~{round(grams)}г")
        else:
            lines.append(f"  • {name}")
    return lines


def _format_warnings(analysis: dict) -> list[str]:
    """Build allergen + Crohn warning lines."""
    lines = []
    allergen_flags = analysis.get("allergen_flags") or []
    crohn_flags = analysis.get("crohn_flags") or []

    if allergen_flags:
        lines.append("")
        lines.append("⚠️ <b>УВАГА, АЛЕРГЕН:</b>")
        for a in allergen_flags:
            icon = _CONFIDENCE_ICON.get((a.get("confidence") or "").lower(), "⚠️")
            allergen_name = _esc(str(a.get("allergen", "?")).capitalize())
            confidence = _esc(a.get("confidence", "?"))
            ingredient = _esc(a.get("ingredient", "цієї страви"))
            lines.append(
                f"  {icon} {allergen_name} (впевненість: {confidence}) — у складі: {ingredient}"
            )

    if crohn_flags:
        lines.append("")
        lines.append("💡 <b>Нотатки щодо здоров'я (для кату):</b>")
        for c in crohn_flags:
            icon = _SEVERITY_ICON.get((c.get("severity") or "").lower(), "🟡")
            concern = _esc(c.get("concern", "питання"))
            ingredient = _esc(c.get("ingredient", "?"))
            lines.append(f"  {icon} {concern} ({ingredient})")

    return lines


def _format_nutrition_line(nutrition: dict) -> str:
    return (
        f"🔥 {round(nutrition.get('calories', 0))} ккал | "
        f"🥩 {round(nutrition.get('protein_g', 0))}г Б | "
        f"🍚 {round(nutrition.get('carbs_g', 0))}г В | "
        f"🧈 {round(nutrition.get('fat_g', 0))}г Ж"
    )


_GI_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}
_GI_UA = {"low": "Низький ГІ", "medium": "Середній ГІ", "high": "Високий ГІ"}


def _format_glycemic_line(analysis: dict) -> str | None:
    gi = analysis.get("glycemic_index") or {}
    level = (gi.get("level") or "").lower()
    note = (gi.get("note") or "").strip()
    if not level:
        return None
    icon = _GI_ICON.get(level, "🩸")
    # _GI_UA values are static literals; the level fallback is whatever the LLM
    # returned, so escape it. Note is free-form LLM text and must be escaped.
    label = _GI_UA.get(level, _esc(level.capitalize()))
    safe_note = _esc(note)
    return f"{icon} {label}" + (f" — {safe_note}" if safe_note else "")


# --- Preview (before user accepts) ---

def format_meal_preview(meal_type: str, analysis: dict) -> str:
    """Preview message shown after AI analysis, before user taps Accept."""
    dish = _esc(analysis.get("dish_name") or "Страва")
    # meal_type comes from a callback allowlist; fall back through _esc anyway.
    meal_ua = _MEAL_TYPE_UA.get(meal_type.lower(), _esc(meal_type.capitalize()))
    nutrition = analysis.get("nutrition", {}) or {}

    lines = [
        f"🔍 <b>Попередній перегляд: {dish}</b>",
        f"🕐 {meal_ua}",
    ]

    lines.extend(_format_ingredients(analysis))
    lines.append("")
    lines.append(_format_nutrition_line(nutrition))
    gi_line = _format_glycemic_line(analysis)
    if gi_line:
        lines.append(gi_line)
    lines.extend(_format_warnings(analysis))

    assessment = analysis.get("overall_assessment")
    if assessment:
        lines.append("")
        lines.append(f"💬 {_esc(assessment)}")

    lines.append("")
    lines.append("👇 <b>Підтвердити або виправити:</b>")
    return "\n".join(lines)


# --- Final confirmation (after Accept) ---

def format_meal_logged(
    meal_type: str,
    analysis: dict,
    today_log: dict,
    daily_cal_target: int,
    first_name: str | None = None,
) -> str:
    nutrition = analysis.get("nutrition", {}) or {}
    dish = _esc(analysis.get("dish_name") or "Страва")
    date_display = _ua_date_long(datetime.now(LOCAL_TZ))
    meal_ua = _MEAL_TYPE_UA.get(meal_type.lower(), _esc(meal_type.capitalize()))

    lines = [
        f"✅ <b>Записав: {dish}</b>",
        f"🕐 {meal_ua} — {date_display}",
    ]

    lines.extend(_format_ingredients(analysis))
    lines.append("")
    lines.append(_format_nutrition_line(nutrition))
    gi_line = _format_glycemic_line(analysis)
    if gi_line:
        lines.append(gi_line)
    lines.extend(_format_warnings(analysis))

    assessment = analysis.get("overall_assessment")
    if assessment:
        lines.append("")
        lines.append(f"💬 {_esc(assessment)}")

    lines.append("")
    lines.append(
        f"📊 Разом за день: {round(today_log.get('calories', 0))} / {daily_cal_target} ккал"
    )

    if first_name:
        lines.append(f"<i>Тримайся, {_esc(first_name)}! 💪</i>")

    return "\n".join(lines)


# --- Meal management list ---

def format_meals_list(
    meals: list[dict],
    log: dict | None = None,
    daily_cal_target: int | None = None,
    macros: dict | None = None,
) -> str:
    """List today's meals with IDs for edit/delete.

    If ``log``, ``daily_cal_target`` and ``macros`` are supplied, a compact
    calorie + macro header is prepended so the user sees their day totals
    without having to switch to /today.
    """
    if not meals:
        return (
            "📋 <b>Сьогодні ще нічого не записано.</b>\n"
            "Надішли фото або напиши, що їв/їла. 📸"
        )

    lines: list[str] = []

    if log and daily_cal_target and macros:
        cal = log.get("calories", 0) or 0
        p = log.get("protein", 0) or 0
        c = log.get("carbs", 0) or 0
        f = log.get("fat", 0) or 0
        lines.append(
            f"📊 <b>Разом за день:</b> {round(cal)} / {daily_cal_target} ккал "
            f"({_pct(cal, daily_cal_target)}%)"
        )
        lines.append(
            f"🥩 {round(p)}/{macros['protein']}г · "
            f"🍚 {round(c)}/{macros['carbs']}г · "
            f"🧈 {round(f)}/{macros['fat']}г"
        )
        lines.append("")

    lines.append("📋 <b>Страви за сьогодні:</b>")
    lines.append("")
    for i, m in enumerate(meals, 1):
        mt = _MEAL_TYPE_UA.get((m.get("meal_type") or "").lower(), "")
        desc = _esc((m.get("description") or "")[:50])
        cal = round(m.get("calories", 0))
        p = round(m.get("protein_g", 0))
        c = round(m.get("carbs_g", 0))
        f = round(m.get("fat_g", 0))
        lines.append(f"{i}. <b>{mt}</b> — {desc}")
        lines.append(f"   🔥 {cal} ккал | 🥩 {p}г Б | 🍚 {c}г В | 🧈 {f}г Ж")
        lines.append("")

    lines.append("👇 Обери дію під кожною стравою:")
    return "\n".join(lines)


# --- Today progress ---

_WELCOME_VARIANTS = [
    "Йо, <b>{name}</b>! Я KusWise Bot — твій особистий джин-харчознавець. 📸 фото або 📝 текст — рахую калорії за секунду. 💪",
    "Привіт, <b>{name}</b>! Три бажання я не виконую, але калорії рахую чесно. Надсилай страву — я перевірю. 🧞",
    "<b>{name}</b>, вітаю! Я як калькулятор із характером: бачу страву — називаю калорії. 📸 / 📝 старт.",
    "Йо, <b>{name}</b>! Від сьогодні жоден бутерброд не пройде повз моє око. 👁️ Надсилай їжу.",
    "<b>{name}</b>, привіт! Я — твій персональний дієтолог, тільки без строгого тону. Фото чи текст?",
    "Йо, <b>{name}</b>! Їжу — мені, поради — від мене. Чесний обмін. 📸",
]


def welcome_message(first_name: str | None = None) -> str:
    name = _name_or_default(first_name)
    return random.choice(_WELCOME_VARIANTS).format(name=name)


# --- Onboarding ---

ONBOARDING_INTRO = (
    "👋 Привіт! Я KusWise Bot — твій персональний джин-харчознавець.\n\n"
    "Щоб порахувати твою ідеальну норму калорій, мені потрібно про тебе трохи дізнатися. "
    "Це займе хвилину — шість коротких питань. Поїхали? 🚀"
)

ONBOARDING_ASK_AGE = (
    "1/6 🎂 <b>Скільки тобі років?</b>\n"
    "Напиши числом (10–100). Обіцяю нікому не казати."
)
ONBOARDING_ASK_SEX = "2/6 🚻 <b>Стать?</b>\nЦе потрібно для формули — різний обмін речовин."
ONBOARDING_ASK_WEIGHT = (
    "3/6 ⚖️ <b>Скільки ти важиш?</b> (у кілограмах)\n"
    "Можна з комою (наприклад: 75.5). Ваги не брешуть, бот теж."
)
ONBOARDING_ASK_HEIGHT = (
    "4/6 📏 <b>Який ти на зріст?</b> (у сантиметрах)\n"
    "Ціле число, 100–250. Не додавай «з носками»."
)
ONBOARDING_ASK_GYM = (
    "5/6 🏋️ <b>Скільки разів на тиждень тренуєшся?</b>\n"
    "Чесно. Диван не рахується."
)
ONBOARDING_ASK_GOAL = "6/6 🎯 <b>Яка твоя мета?</b>"
ONBOARDING_INVALID_NUMBER = "Хм, це не схоже на число. Спробуй ще раз. 🙂"
ONBOARDING_AGE_RANGE = "Вік має бути від 10 до 100. Введи ще раз, будь ласка. 🎂"
ONBOARDING_WEIGHT_RANGE = "Вага має бути від 30 до 300 кг. Введи ще раз. ⚖️"
ONBOARDING_HEIGHT_RANGE = "Зріст має бути від 100 до 250 см. Введи ще раз. 📏"
ONBOARDING_CUSTOM_CAL_PROMPT = (
    "Введи свою цифру калорій (ціле число від 1000 до 6000):"
)
ONBOARDING_CUSTOM_CAL_RANGE = "Калорії мають бути від 1000 до 6000. Спробуй ще раз. 🙂"
ONBOARDING_NEED_BUTTON = "Будь ласка, скористайся кнопкою нижче. 👇"
ONBOARDING_DONE = (
    "🎉 Готово, <b>{name}</b>! Тепер я знаю про тебе все, що треба.\n\n"
    "Надсилай 📸 фото страв або 📝 текстом — я рахуватиму. Команди — у меню «/», твій профіль — /profile."
)

# Onboarding step: timezone (F-3)
ONBOARDING_ASK_TZ = (
    "🌐 <b>Який у тебе часовий пояс?</b>\n\n"
    "Це впливає на час денного підсумку і на те, коли «сьогодні» переходить у «завтра». "
    "Якщо твоєї зони немає в списку — обери «Інша зона» і введи назву IANA вручну."
)
ONBOARDING_TZ_CUSTOM_PROMPT = (
    "Введи свою часову зону у форматі IANA, наприклад:\n"
    "<code>Asia/Tokyo</code>, <code>America/Chicago</code>, <code>Australia/Sydney</code>.\n\n"
    "Повний список: en.wikipedia.org/wiki/List_of_tz_database_time_zones"
)
ONBOARDING_TZ_INVALID = (
    "Не впізнав цю зону 🤔 Перевір написання — потрібен формат <code>Region/City</code>, "
    "наприклад <code>Europe/Madrid</code> або <code>Asia/Tokyo</code>."
)
ONBOARDING_TZ_SAVED = "🌐 Часовий пояс: <b>{tz}</b>"

# /timezone command
TIMEZONE_PROMPT = (
    "🌐 <b>Часовий пояс</b>\n"
    "Поточний: <b>{current}</b>\n\n"
    "Обери нову зону зі списку або «Інша зона» для введення вручну."
)
TIMEZONE_NOT_ONBOARDED = "Спершу пройди /start, щоб налаштувати профіль ☺️"
TIMEZONE_SAVED = "✅ Готово. Часовий пояс: <b>{tz}</b>"
TIMEZONE_CUSTOM_PROMPT = (
    "Введи назву зони у форматі IANA (<code>Region/City</code>), "
    "наприклад <code>Asia/Tokyo</code>. /cancel — щоб скасувати."
)
TIMEZONE_CANCELLED = "Скасовано. Часовий пояс не змінено."

# Health profile (F-1)
HEALTH_HEADER = (
    "⚕️ <b>Здоровʼя</b>\n\n"
    "🥜 Алергени: <b>{allergens}</b>\n"
    "🩺 Стани: <b>{conditions}</b>\n\n"
    "Це впливає на попередження в аналізі страв (allergen_flags / crohn_flags) — "
    "кнопки нижче дозволяють відредагувати."
)
HEALTH_NOT_ONBOARDED = "Спершу пройди /start ☺️"
HEALTH_ALLERGENS_PROMPT = (
    "🥜 <b>Алергени</b>\n\n"
    "Введи список через кому. Розпізнаю:\n"
    "<i>peanut, tree_nut, dairy, egg, soy, gluten, fish, shellfish, "
    "sesame, mustard, sulphites, celery, lupin, mollusks</i>\n\n"
    "Або українською: <i>арахіс, горіхи, молочне, яйце, соя, глютен, "
    "риба, морепродукти, кунжут, гірчиця, сульфіти, селера, люпин, мідії</i>\n\n"
    "Напиши <code>немає</code> щоб очистити, /cancel — скасувати."
)
HEALTH_CONDITIONS_PROMPT = (
    "🩺 <b>Хронічні стани</b>\n\n"
    "Введи через кому. Розпізнаю:\n"
    "<i>crohns, ibs, celiac, diabetes_t1, diabetes_t2, hypertension, "
    "pcos, kidney, thyroid, gestational</i>\n\n"
    "Українською: <i>хвороба Крона, СРК, целіакія, діабет 1, діабет 2, "
    "гіпертонія, СПКЯ, нирки, щитоподібна, вагітність</i>\n\n"
    "Напиши <code>немає</code> щоб очистити, /cancel — скасувати."
)
HEALTH_SAVED = "✅ Збережено: <b>{saved}</b>"
HEALTH_SAVED_WITH_HINTS = (
    "✅ Збережено: <b>{saved}</b>\n"
    "Не розпізнав: <i>{unknown}</i> — спробуй з канонічного списку."
)
HEALTH_CLEARED = "🧹 Очищено."
HEALTH_CANCELLED = "Скасовано."
HEALTH_INVALID_ALL = "Не розпізнав жодного значення. Спробуй ще раз або /cancel."


def _sex_ua(sex: str) -> str:
    return {"male": "чоловіча", "female": "жіноча"}.get(sex, sex or "—")


def _goal_ua(goal: str) -> str:
    return {
        "lose": "схуднути",
        "maintain": "підтримувати вагу",
        "gain": "набрати м'язи",
    }.get(goal, goal or "—")


def _gym_ua(freq: str) -> str:
    mapping = {
        "0": "0 разів",
        "1-2": "1–2 рази",
        "3-4": "3–4 рази",
        "5-6": "5–6 разів",
        "7": "7 разів",
    }
    return mapping.get(freq, freq or "—")


def format_recommendation(profile: dict, recommended: int) -> str:
    weight = profile.get("weight_kg") or 0
    goal = profile.get("goal") or "maintain"
    macros = macro_gram_targets_from_profile(weight, goal)
    return (
        "🧮 <b>Порахував!</b>\n\n"
        f"Вік: <b>{profile.get('age', '—')}</b>\n"
        f"Стать: <b>{_sex_ua(profile.get('sex', ''))}</b>\n"
        f"Вага: <b>{profile.get('weight_kg', '—')} кг</b>\n"
        f"Зріст: <b>{profile.get('height_cm', '—')} см</b>\n"
        f"Тренування: <b>{_gym_ua(profile.get('gym_per_week', ''))} на тиждень</b>\n"
        f"Мета: <b>{_goal_ua(profile.get('goal', ''))}</b>\n\n"
        f"🔥 Рекомендована денна норма: <b>{recommended} ккал</b>\n"
        f"🥩 Білки: <b>{macros['protein']}г</b> · 🍚 Вуглеводи: <b>{macros['carbs']}г</b> · 🧈 Жири: <b>{macros['fat']}г</b>\n\n"
        "Число порахував за нормою грамів білків/жирів/вуглеводів на кілограм ваги під твою мету. "
        "Можеш прийняти — або ввести своє. 👇"
    )


def format_profile(profile: dict) -> str:
    if not profile:
        return "Профіль ще не заповнено. Натисни /start. 👋"
    target = profile.get("daily_calorie_target") or 0
    rec = profile.get("recommended_calorie_target") or 0
    weight = profile.get("weight_kg")
    goal = profile.get("goal")
    if weight and goal:
        macros = macro_gram_targets_from_profile(weight, goal)
    else:
        macros = macro_gram_targets(target) if target else {"protein": 0, "carbs": 0, "fat": 0}
    lines = [
        "👤 <b>Твій профіль</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🎂 Вік: <b>{profile.get('age', '—')}</b>",
        f"🚻 Стать: <b>{_sex_ua(profile.get('sex', ''))}</b>",
        f"⚖️ Вага: <b>{profile.get('weight_kg', '—')} кг</b>",
        f"📏 Зріст: <b>{profile.get('height_cm', '—')} см</b>",
        f"🏋️ Тренування: <b>{_gym_ua(profile.get('gym_per_week', ''))} / тиждень</b>",
        f"🎯 Мета: <b>{_goal_ua(profile.get('goal', ''))}</b>",
    ]
    tw = profile.get("target_weight_kg")
    if tw and goal in ("lose", "gain") and weight:
        delta = float(weight) - float(tw)  # positive = need to lose; negative = need to gain
        if goal == "lose":
            togo = max(0.0, delta)
            arrow = "—" if togo <= 0.05 else f"−{togo:.1f} кг"
        else:  # gain
            togo = max(0.0, -delta)
            arrow = "—" if togo <= 0.05 else f"+{togo:.1f} кг"
        if togo <= 0.05:
            lines.append(f"🏁 Цільова вага: <b>{tw} кг</b> (досягнуто 🎉)")
        else:
            lines.append(f"🏁 Цільова вага: <b>{tw} кг</b> ({arrow} до мети)")
    elif tw:
        lines.append(f"🏁 Цільова вага: <b>{tw} кг</b>")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🔥 Денна норма: <b>{target} ккал</b>" + (f" (рекомендовано: {rec})" if rec and rec != target else ""),
        f"🥩 Білки: <b>{macros['protein']}г</b> | 🍚 Вуглеводи: <b>{macros['carbs']}г</b> | 🧈 Жири: <b>{macros['fat']}г</b>",
        "",
        "Щоб оновити — натисни ✏️ нижче.",
        "",
        "📖 Офіційна документація: <a href=\"https://raudar.gitbook.io/djinni\">raudar.gitbook.io/djinni</a>",
    ]
    return "\n".join(lines)


ONBOARDING_REQUIRED = (
    "Спочатку давай познайомимось 👋 Натисни /start — поставлю кілька питань і порахую твою норму."
)


def help_message() -> str:
    return (
        "🤖 <b>Команди</b>\n"
        "/start — привітання та меню\n"
        "/profile — твій профіль (можна змінити)\n"
        "/ask — 💬 запитати ШІ про їжу, рецепти, покупки\n"
        "/today — прогрес за сьогодні\n"
        "/yesterday — вчорашній день\n"
        "/streak — 🔥 серія логів і заморозки\n"
        "/goals — 🎯 цілі та прогноз досягнення\n"
        "/aliases — 📚 твої звичні страви (бот вчиться)\n"
        "/scan — 🔢 сканер штрих-кодів (Open Food Facts)\n"
        "/menu — 📋 OCR меню в кафе/ресторані\n"
        "/plan — 🗓 3-денний план на основі цілі\n"
        "/recap — 📸 PNG-картка тижневих результатів\n"
        "/meals — список страв (видалити / змінити)\n"
        "/fav — ⭐ улюблені страви\n"
        "/recent — 🕘 останні страви (швидкий повтор)\n"
        "/water — 💧 облік води\n"
        "/suggest_meal — ідея страви, яка закриє день\n"
        "/help — показати цей список\n\n"
        "📸 Надішли фото страви — я спитаю, який це прийом їжі, і покажу аналіз на перевірку.\n"
        "📝 Або напиши текстом (наприклад: «курка 200г, рис 150г, броколі 100г»).\n"
        "🎙 Голосове повідомлення — скажи, що ти їв/їла, я розшифрую і запишу.\n"
        "Після аналізу: ✅ Прийняти / 🔄 Перерахувати / ✏️ Ввести вручну.\n\n"
        "📖 Офіційна документація: <a href=\"https://raudar.gitbook.io/djinni\">raudar.gitbook.io/djinni</a>"
    )


def _streak_word_uk(n: int) -> str:
    """Ukrainian plural for 'день' — 1 → 'день', 2-4 → 'дні', 0/5+ → 'днів'.
    Russian-style 11-14 exception applies."""
    n = abs(int(n))
    if 11 <= (n % 100) <= 14:
        return "днів"
    last = n % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дні"
    return "днів"


def _format_streak_line(streak: dict | None) -> str | None:
    """Return the /today header streak line, or None if no streak to show."""
    if not streak:
        return None
    cur = int(streak.get("current_streak") or 0)
    if cur < 1:
        return None
    freezes = int(streak.get("freeze_days_remaining") or 0)
    return f"🔥 Серія: {cur} {_streak_word_uk(cur)} · 🧊 Заморозок: {freezes}"


def format_today_progress(
    log: dict,
    daily_cal_target: int,
    first_name: str | None = None,
    profile: dict | None = None,
    streak: dict | None = None,
) -> str:
    date_display = _ua_date_long(datetime.now(LOCAL_TZ))
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
        quip = "Поки порожньо, як у холодильнику студента перед стипендією. 😅"
    elif cal < daily_cal_target * 0.5:
        quip = "Ще є місце для маневрів (і для курки з рисом). 🍚"
    elif cal < daily_cal_target * 0.9:
        quip = "Цілковита гармонія — продовжуй у тому ж дусі. 💪"
    elif cal <= daily_cal_target * 1.05:
        quip = "Ідеально в ціль, як снайпер по котлеті. 🎯"
    else:
        quip = "Сьогодні ми святкували. Завтра — легше. 😉"

    streak_line = _format_streak_line(streak)
    streak_block = f"{streak_line}\n" if streak_line else ""

    return (
        f"📊 <b>Прогрес на сьогодні ({date_display})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"{streak_block}"
        f"🔥 Калорії:  {round(cal)} / {daily_cal_target} ({_pct(cal, daily_cal_target)}%)\n"
        f"   {_bar(cal, daily_cal_target)}\n"
        f"🥩 Білки:    {round(p)}г / {macros['protein']}г ({_pct(p, macros['protein'])}%)\n"
        f"   {_bar(p, macros['protein'])}\n"
        f"🍚 Вуглеводи:{round(c)}г / {macros['carbs']}г ({_pct(c, macros['carbs'])}%)\n"
        f"   {_bar(c, macros['carbs'])}\n"
        f"🧈 Жири:     {round(f)}г / {macros['fat']}г ({_pct(f, macros['fat'])}%)\n"
        f"   {_bar(f, macros['fat'])}\n"
        f"📈 Клітковина: {round(fib)}г\n"
        f"🍬 Цукор:      {round(sug)}г\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Прийомів їжі: {meals}\n"
        f"Залишилось: ~{round(remaining)} ккал\n\n"
        f"<i>{quip}</i>"
    )


def format_goals(
    profile: dict | None,
    projection,                 # lib.goals.Projection (avoid circular import)
    actual_weekly_delta: float | None = None,
    status: str | None = None,  # "ahead" | "on_track" | "behind"
    first_name: str | None = None,
) -> str:
    """Render the /goals command response.

    ``projection`` is a :class:`lib.goals.Projection` dataclass. ``status`` and
    ``actual_weekly_delta`` come from comparing recent weight history against
    the target rate; pass None when there's not enough data.
    """
    name = _name_or_default(first_name)
    if not profile:
        return GOALS_NO_PROFILE

    current = profile.get("weight_kg")
    target = profile.get("target_weight_kg")
    goal = profile.get("goal") or "maintain"
    weekly_delta = projection.weekly_delta_kg

    lines = [
        GOALS_HEADER.format(name=name),
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    if current is not None:
        lines.append(f"⚖️ Поточна вага: <b>{float(current):.1f} кг</b>")
    if target is not None:
        lines.append(f"🏁 Ціль: <b>{float(target):.1f} кг</b>")
    else:
        lines.append("🏁 Ціль: <i>не задана</i>")

    if goal == "maintain":
        lines.append("🎯 Мета: <b>Підтримувати вагу</b>")
    elif weekly_delta:
        lines.append(f"📈 Тижнева ціль: <b>{weekly_delta:+.2f} кг/тиждень</b>")
    else:
        lines.append("📈 Тижнева ціль: <i>не задана</i>")

    # Projection block.
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    if projection.reason == "ok":
        weeks = projection.weeks_to_goal or 0
        d = projection.projected_date
        date_str = f"{d.day:02d}.{d.month:02d}.{d.year}" if d else "—"
        lines.append(f"⏳ Орієнтовно <b>{weeks:g}</b> тижнів до цілі")
        lines.append(f"📅 Прогноз: <b>{date_str}</b>")
    elif projection.reason == "at_target":
        lines.append(GOALS_PROJECTION_AT_TARGET)
    elif projection.reason == "no_target":
        lines.append(GOALS_NO_TARGET)
    elif projection.reason == "zero_delta":
        lines.append(GOALS_PROJECTION_ZERO_DELTA)
    elif projection.reason == "wrong_direction":
        lines.append(GOALS_PROJECTION_WRONG_DIRECTION)
    elif projection.reason == "no_current":
        lines.append("⚖️ Поточна вага не задана. Запиши через /profile.")

    # Status from actual progress (when available).
    if status == "ahead":
        lines.append(GOALS_STATUS_AHEAD)
    elif status == "on_track":
        lines.append(GOALS_STATUS_ON_TRACK)
    elif status == "behind":
        lines.append(GOALS_STATUS_BEHIND)
    if actual_weekly_delta is not None:
        lines.append(f"<i>Фактично за останні тижні: {actual_weekly_delta:+.2f} кг/тиждень</i>")

    return "\n".join(lines)


def format_projection_line(projection, status: str | None = None) -> str | None:
    """One-line projection summary for the Monday weigh-in reply.

    Returns None when projection isn't meaningful (no target, maintain, etc.) —
    callers omit the line entirely in that case rather than pad noise.
    """
    if projection.reason != "ok":
        return None
    d = projection.projected_date
    if d is None:
        return None
    date_str = f"{d.day:02d}.{d.month:02d}.{d.year}"
    weeks = projection.weeks_to_goal or 0
    head = ""
    if status == "ahead":
        head = "🟢 Випереджаєш план — "
    elif status == "behind":
        head = "🔴 Відстаєш — "
    elif status == "on_track":
        head = "🟡 В графіку — "
    return f"{head}прогноз цілі: <b>{date_str}</b> (~{weeks:g} тижнів)"


# F-9: menu OCR strings
MENU_PROMPT_INTRO = (
    "📋 <b>Сканер меню</b>\n"
    "Сфотографуй меню кафе чи ресторану — я витягну страви з оцінкою калорій. "
    "Поряд з кожною стравою буде кнопка «Залогувати», яка додасть її в твій день.\n\n"
    "<i>Надішли фото зараз. Або /cancel — скасувати.</i>"
)
MENU_NO_DISHES = (
    "🤷 Не вдалось розпізнати жодної страви на цьому фото. "
    "Спробуй чіткіший знімок або введи страву текстом."
)
MENU_OCR_FAILED = "❌ Не вдалось обробити фото. Спробуй ще раз."
MENU_RESULTS_HEADER = "📋 <b>Знайшов {n} страв(и):</b>"
MENU_PENDING_EXPIRED = "Меню більше не активне. Відскануй ще раз через /menu."


def format_menu_dishes_intro(n: int) -> str:
    return MENU_RESULTS_HEADER.format(n=n)


def format_menu_dish_row(dish: dict) -> str:
    """Single-dish line for the menu results message."""
    name = dish.get("name", "")
    kcal = int(round(float(dish.get("calories")  or 0)))
    p    = int(round(float(dish.get("protein_g") or 0)))
    f    = int(round(float(dish.get("fat_g")     or 0)))
    c    = int(round(float(dish.get("carbs_g")   or 0)))
    portion = (dish.get("portion_note") or "").strip()
    portion_part = f" · <i>{portion}</i>" if portion else ""
    return f"<b>{name}</b> — {kcal} ккал · Б{p} Ж{f} В{c}{portion_part}"


# F-8: barcode scanner strings
BARCODE_SCAN_INTRO = (
    "📷 <b>Сканер штрих-кодів</b>\n"
    "Натисни «Відкрити сканер», щоб відкрити камеру в Telegram. "
    "Наведи на штрих-код — я знайду продукт у Open Food Facts (3 млн+ позицій).\n\n"
    "<i>Камера не запускається? Натисни «Ввести цифрами» — і просто набери "
    "цифри під штрих-кодом (8-13 цифр).</i>"
)
BARCODE_FOUND_HEADER = (
    "✓ Знайшов: <b>{name}</b>\n"
    "Бренд: {brand}\n"
    "На 100г: <b>{kcal} ккал</b> · Б{p} Ж{f} В{c}\n\n"
    "Скільки грамів?"
)
BARCODE_NOT_FOUND = (
    "🤷 Не знайшов штрих-код <code>{ean}</code> у базі.\n\n"
    "Спробуй ввести продукт текстом (наприклад: «йогурт натуральний 150г»), "
    "або просто сфотографуй його — я проаналізую візуально."
)
BARCODE_LOOKUP_FAILED = (
    "❌ Не зміг звернутися до бази продуктів. Спробуй ще раз або введи продукт текстом."
)
BARCODE_GRAMS_PROMPT = "✏️ <b>Скільки грамів?</b>\nНапиши число (наприклад: <code>180</code>):"
BARCODE_GRAMS_INVALID = "Кількість має бути числом від 1 до 5000 г. Спробуй ще раз 🙂"
BARCODE_PENDING_EXPIRED = (
    "Сканована позиція уже не активна. Спробуй просканувати ще раз через 🔢 Сканер."
)
BARCODE_MANUAL_PROMPT = (
    "✏️ <b>Введи штрих-код цифрами</b>\n"
    "Цифри під штрих-кодом, без пробілів — наприклад: <code>5449000000996</code>. "
    "8-13 цифр. /cancel — скасувати."
)
BARCODE_MANUAL_INVALID = "Штрих-код має бути 8-13 цифр без літер. Спробуй ще раз 🙂"

# F-10: meal plan strings
PLAN_INTRO = (
    "🗓 <b>3-денний план</b>\n"
    "Я зроблю план з твоїми калоріями + здоров'ям. "
    "Якщо хочеш — напиши, що в тебе є вдома (наприклад: <code>курка, рис, броколі, яйця</code>) — "
    "врахую це. Або просто натисни «Без списку».\n\n"
    "<i>/cancel — скасувати.</i>"
)
PLAN_GENERATING = "🍳 Готую план… це займе 10-20 секунд."
PLAN_FAILED = "❌ Не зміг згенерувати план. Спробуй ще раз через хвилину."
PLAN_PANTRY_TOO_LONG = "Список занадто довгий — обмеж 200 символами."
PLAN_HEADER_NOTES = "📝 <i>{notes}</i>\n\n"
PLAN_DAY_HEADER = "━━━━━━━━━━━━━━━━━━━━━\n📅 <b>{label}</b>"


# F-11: fridge / swap strings
FRIDGE_PROMPT = (
    "🛒 <b>Що є в холодильнику?</b>\n"
    "Напиши через кому — наприклад: <code>курка, рис, броколі, яйця, помідори</code>. "
    "Я придумаю рецепт лише з цих продуктів.\n\n"
    "<i>/cancel — скасувати.</i>"
)
FRIDGE_TOO_LONG = "Список занадто довгий — обмеж 300 символами."
SUGGEST_VARIATION_HINT = (
    "Запропонуй ІНШИЙ рецепт ніж попередній — інший білок (якщо в попередньому "
    "була курка — спробуй рибу/яйця/тофу), інший спосіб приготування, інший набір спецій."
)


def format_meal_plan_day(day: dict, day_idx: int) -> str:
    """Render one day's slots as a single Telegram message body."""
    lines = [PLAN_DAY_HEADER.format(label=day["date_label"])]
    slot_emojis = {"breakfast": "🥣", "lunch": "🍱", "dinner": "🍽️", "snack": "🍎"}
    slot_labels = {"breakfast": "Сніданок", "lunch": "Обід", "dinner": "Вечеря", "snack": "Перекус"}
    for slot_key in ("breakfast", "lunch", "dinner", "snack"):
        slot = day["slots"].get(slot_key)
        if not slot:
            continue
        emoji = slot_emojis[slot_key]
        label = slot_labels[slot_key]
        kcal = int(round(float(slot.get("calories")  or 0)))
        p    = int(round(float(slot.get("protein_g") or 0)))
        f    = int(round(float(slot.get("fat_g")     or 0)))
        c    = int(round(float(slot.get("carbs_g")   or 0)))
        recipe = slot.get("recipe", "")
        lines.append(
            f"\n{emoji} <b>{label}</b> · {kcal} ккал · Б{p} Ж{f} В{c}\n"
            f"<b>{slot['name']}</b>"
        )
        if recipe:
            lines.append(f"<i>{recipe}</i>")
    return "\n".join(lines)


def format_aliases(aliases: list[dict], first_name: str | None = None) -> str:
    """F-7: render the /aliases command response.

    ``aliases`` is the list returned by :func:`lib.personalization.recent_aliases`.
    Empty list → friendly placeholder explaining how the bot learns.
    """
    name = _name_or_default(first_name)
    if not aliases:
        return (
            f"📚 <b>Твої страви</b> · {name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Поки нічого не накопичено. Бот вчиться з прийнятих страв — "
            f"наступного разу, коли логуватимеш «куряча грудка», запам'ятає твою "
            f"звичну порцію.\n\n"
            f"<i>Допомагає вгадувати грами точніше після ~5 повторень одного блюда.</i>"
        )
    lines = [
        f"📚 <b>Твої страви</b> · {name}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "Бот використовує ці звичні порції щоб точніше оцінювати фото.",
        "",
    ]
    for a in aliases[:12]:
        kcal = int(round(float(a.get("default_kcal") or 0)))
        grams = float(a.get("default_grams") or 0)
        portion_txt = f"~{int(round(grams))}г · " if grams > 0 else ""
        samples = int(a.get("sample_count") or 0)
        sample_tag = f" <i>({samples}×)</i>" if samples > 1 else ""
        lines.append(
            f"• <b>{a.get('normalized_name', a.get('alias', ''))}</b> — "
            f"{portion_txt}{kcal} ккал{sample_tag}"
        )
    if len(aliases) > 12:
        lines.append(f"<i>…ще {len(aliases) - 12}</i>")
    return "\n".join(lines)


def format_alternates_intro(meal_type: str, candidates: list[dict]) -> str:
    """F-6: header shown above the alternates keyboard when the photo is ambiguous.

    Lists the candidates as a quick legend so the user can compare numbers
    before tapping a button.
    """
    meal_label_map = {
        "breakfast": "Сніданок", "lunch": "Обід", "dinner": "Вечеря",
        "snack": "Перекус", "other": "Прийом їжі",
    }
    label = meal_label_map.get(meal_type, "Прийом їжі")
    lines = [
        f"🤔 <b>Не на 100% впевнений у стравi ({label})</b>",
        "Обери правильний варіант або введи вручну:",
        "",
    ]
    digits = ("1⃣", "2⃣", "3⃣")
    for i, c in enumerate(candidates[:3]):
        kcal = int(round(float(c.get("calories")  or 0)))
        p    = int(round(float(c.get("protein_g") or 0)))
        cb   = int(round(float(c.get("carbs_g")   or 0)))
        f    = int(round(float(c.get("fat_g")     or 0)))
        conf = int(round(float(c.get("confidence") or 0) * 100))
        lines.append(
            f"{digits[i]} <b>{c.get('name','')}</b> · {kcal} ккал · "
            f"Б{p} Ж{f} В{cb} · ~{conf}%"
        )
    return "\n".join(lines)


def format_streak_summary(streak: dict | None, first_name: str | None = None) -> str:
    """Render the /streak command response.

    ``streak`` is the row dict from :func:`lib.database.get_streak`, or ``None``
    when the user has never logged a meal.
    """
    name = _name_or_default(first_name)
    if not streak or int(streak.get("current_streak") or 0) < 1:
        return (
            f"🔥 <b>Серія</b> · {name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Ще не починали серію — залогуйте першу страву й поїхали! 🚀\n\n"
            f"<i>Серія росте, коли ви заносите хоча б одну страву щодня. "
            f"3 «заморозки» на місяць рятують пропущений день.</i>"
        )
    cur = int(streak.get("current_streak") or 0)
    longest = int(streak.get("longest_streak") or 0)
    freezes = int(streak.get("freeze_days_remaining") or 0)
    last = streak.get("last_log_date") or "—"
    return (
        f"🔥 <b>Серія</b> · {name}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Поточна серія: <b>{cur}</b> {_streak_word_uk(cur)}\n"
        f"Найкраща серія: <b>{longest}</b> {_streak_word_uk(longest)}\n"
        f"🧊 Заморозок цього місяця: <b>{freezes}</b>/3\n"
        f"Останній лог: {last}\n\n"
        f"<i>Заморозки відновлюються 1 числа кожного місяця.</i>"
    )


def format_yesterday(
    log: dict,
    meals: list[dict],
    daily_cal_target: int,
    first_name: str | None = None,
    profile: dict | None = None,
) -> str:
    """Yesterday's progress + meal list in one message."""
    date_str = log.get("date", "")
    try:
        date_display = _ua_date_long(datetime.strptime(date_str, "%Y-%m-%d"))
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
            f"📆 <b>Вчора ({date_display})</b>\n"
            f"Нічого не було записано. Тиша в холодильнику. 🤫"
        )

    meal_lines = []
    for m in meals:
        mt_raw = (m.get("meal_type") or "").lower()
        mt = _MEAL_TYPE_UA.get(mt_raw, _esc(mt_raw.capitalize() or "—"))
        desc = _esc((m.get("description") or "")[:60])
        meal_lines.append(f"• {mt}: {desc} ({round(m.get('calories', 0))} ккал)")
    meal_section = "\n".join(meal_lines)

    if profile and profile.get("weight_kg") and profile.get("goal"):
        macros = macro_gram_targets_from_profile(profile["weight_kg"], profile["goal"])
    else:
        macros = macro_gram_targets(daily_cal_target)
    return (
        f"📆 <b>Вчора ({date_display})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"🔥 Калорії:  {round(cal)} / {daily_cal_target} ({_pct(cal, daily_cal_target)}%)\n"
        f"   {_bar(cal, daily_cal_target)}\n"
        f"🥩 Білки:    {round(p)}г / {macros['protein']}г\n"
        f"   {_bar(p, macros['protein'])}\n"
        f"🍚 Вуглеводи:{round(c)}г / {macros['carbs']}г\n"
        f"   {_bar(c, macros['carbs'])}\n"
        f"🧈 Жири:     {round(f)}г / {macros['fat']}г\n"
        f"   {_bar(f, macros['fat'])}\n"
        f"📈 Клітковина: {round(fib)}г\n"
        f"🍬 Цукор:      {round(sug)}г\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Страви ({meal_count}):</b>\n"
        f"{meal_section}"
    )


def format_history(rows: list[dict], daily_cal_target: int) -> str:
    if not rows:
        return (
            "📅 Історії ще немає.\n"
            "Надішли перше фото — і ми почнемо писати цю кулінарну сагу. 📖🍳"
        )

    lines = ["📅 <b>Останні 7 днів</b>"]
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
            marker = "⚠️ перебір"
        elif cal < daily_cal_target * 0.80:
            marker = "⚠️ замало"
        else:
            marker = "✅"

        lines.append(
            f"{_ua_date_short(r.get('date', ''))}: {round(cal)} ккал — Б:{p_pct}% В:{c_pct}% Ж:{f_pct}% {marker}"
        )
    lines.append("")
    lines.append("<i>Нагадаю: консистенція важливіша за перфекціонізм. 🌱</i>")
    return "\n".join(lines)


def format_day_detail(date: str, meals: list[dict]) -> str:
    if not meals:
        return f"📅 На {_ua_date_short(date)} нічого не записано. Тиша в холодильнику. 🤫"

    lines = [f"📅 <b>Страви за {_ua_date_short(date)}</b>", ""]
    total_cal = 0
    for m in meals:
        total_cal += m.get("calories", 0)
        mt_raw = (m.get("meal_type") or "")
        mt = _MEAL_TYPE_UA.get(mt_raw.lower(), _esc(mt_raw.capitalize()))
        desc = _esc(m.get("description", ""))
        lines.append(f"🕐 <b>{mt}</b> — {desc}")
        lines.append(
            f"   🔥 {round(m.get('calories', 0))} ккал | "
            f"🥩 {round(m.get('protein_g', 0))}г Б | "
            f"🍚 {round(m.get('carbs_g', 0))}г В | "
            f"🧈 {round(m.get('fat_g', 0))}г Ж"
        )
        if m.get("allergen_warnings"):
            names = ", ".join(_esc(a.get("allergen", "?")) for a in m["allergen_warnings"])
            lines.append(f"   ⚠️ Алергени: {names}")
        lines.append("")

    lines.append(f"<b>Разом: {round(total_cal)} ккал</b>")
    return "\n".join(lines)


# --- Short texts used by webhook.py ---

PHOTO_PROMPT_MEAL_TYPE = "📸 Отримав! Що це за прийом їжі?"
TEXT_PROMPT_MEAL_TYPE = "📝 Записав твій опис! Що це за прийом їжі?"
ANALYZING_WAIT = "🔍 Аналізую страву, хвильку…"
RECALC_WAIT = "🔄 Перераховую уважніше…"
PHOTO_DOWNLOAD_FAILED = "Вибач, не вдалося завантажити фото. Спробуй ще раз. 📷"
PHOTO_ANALYSIS_FAILED = (
    "Не зміг розпізнати страву. Спробуй зробити фото чіткішим — "
    "я ж не кіт, у темряві не бачу. 🐈‍⬛"
)
TEXT_ANALYSIS_FAILED = (
    "Не зміг нормально розпарсити опис. Спробуй написати простіше — "
    "наприклад: «курка 200г, рис 150г, броколі 100г». 🙂"
)
PENDING_EXPIRED = (
    "⏰ Минуло більше 10 хвилин, і я вже забув, що було на фото (у мене "
    "серверна пам'ять — коротка). Надішли ще раз, будь ласка."
)
MANUAL_INPUT_PROMPT = "✏️ Напиши, що ти їв/їла (наприклад: курка 200г, рис 150г, броколі 100г).\nАбо надішли /cancel — передумаєш, я не образюся. 😉"
MEAL_DELETED = "🗑 Видалено: <b>{dish}</b> ({cal} ккал). Денний підрахунок оновлено."
MEAL_EDIT_PROMPT = "✏️ Напиши новий опис страви (замість «{dish}»).\nАбо надішли /cancel щоб скасувати."
MEAL_NOT_FOUND = "Не знайшов цю страву. Можливо, вже видалена."
NO_MEALS_TO_MANAGE = "Сьогодні ще нічого не записано. Надішли фото або текст. 📸"
MEAL_CANCELLED = "Скасовано. Надішли фото або текст, коли будеш готовий. 👌"
UNKNOWN_COMMAND = "Не знаю такої команди. Глянь /help — там усе розписано. 🤓"
SUGGEST_THINKING = "🧠 Думаю над ідеєю, яка закриє твій день…"
SUGGEST_FAILED = "Ідея тимчасово застрягла в моделі. Спробуй за хвилину. 🤖💤"
HISTORY_USAGE = "Використай так: /history_detail РРРР-ММ-ДД (наприклад, /history_detail 2026-04-12)"

# --- Chat mode (/ask) ---
ASK_PROMPT = "💬 Що ти хочеш запитати? Напиши у відповіді — і я врахую твою історію харчування на сьогодні."
ASK_THINKING = "🧠 Думаю над відповіддю…"
ASK_ERROR = "Щось пішло не так з відповіддю. Спробуй ще раз за хвилину. 🤖"

# --- Weekly weight check-in ---
WEIGHT_CHECKIN_PROMPT = (
    "⚖️ Доброго ранку! Новий тиждень — нова вага. "
    "Напиши, скільки ти важиш зараз (кг). Або /skip — пропустити цього тижня."
)
WEIGHT_CHECKIN_SKIPPED = "👌 Пропустив. До наступного понеділка!"
WEIGHT_INPUT_PROMPT = "⚖️ Напиши нову вагу в кілограмах (наприклад: 82.5):"
WEIGHT_INVALID = "Вага має бути від 30 до 300 кг. Спробуй ще раз 🙂"
WEIGHT_NOT_A_NUMBER = "Хм, це не схоже на число. Спробуй так: 82.5 🙂"
GOAL_UPDATE_PROMPT = "🎯 Яка твоя ціль на зараз?"
GOAL_UPDATED = "🎯 Ціль оновив: <b>{goal}</b>."

TARGET_WEIGHT_ASK_LOSE = "🎯 <b>До якої ваги хочеш схуднути?</b>\nНапиши в кілограмах (наприклад: 75):"
TARGET_WEIGHT_ASK_GAIN = "🎯 <b>Яку вагу хочеш набрати?</b>\nНапиши в кілограмах (наприклад: 80):"
TARGET_WEIGHT_INVALID = "Цільова вага має бути від 30 до 300 кг. Спробуй ще раз 🙂"
TARGET_WEIGHT_LOSE_MISMATCH = "Ціль схуднення, але цільова вага ≥ поточної ({current} кг). Напиши меншу цифру або зміни мету через /profile."
TARGET_WEIGHT_GAIN_MISMATCH = "Ціль набору, але цільова вага ≤ поточної ({current} кг). Напиши більшу цифру або зміни мету через /profile."
TARGET_WEIGHT_SAVED = "🎯 Цільова вага: <b>{target} кг</b>."
TARGET_WEIGHT_CLEARED = "🎯 Для мети «Підтримувати вагу» цільова вага не потрібна — очистив."

# F-5: weekly delta + goals dashboard strings
WEEKLY_DELTA_ASK_LOSE = (
    "📈 <b>Скільки кг на тиждень хочеш скидати?</b>\n"
    "Напиши число — наприклад: <code>0.5</code> (повільне зниження) "
    "або <code>1</code> (агресивне).\n\n"
    "<i>Безпечний діапазон для більшості людей — 0.3-1.0 кг/тиждень.</i>"
)
WEEKLY_DELTA_ASK_GAIN = (
    "📈 <b>Скільки кг на тиждень хочеш набирати?</b>\n"
    "Напиши число — наприклад: <code>0.3</code> (чистий ріст м'язів) "
    "або <code>0.5</code> (з невеликим жиром).\n\n"
    "<i>Безпечний діапазон — 0.2-0.5 кг/тиждень.</i>"
)
WEEKLY_DELTA_INVALID = (
    "Тижнева дельта має бути числом від 0.1 до 2 кг. Спробуй ще раз 🙂"
)
WEEKLY_DELTA_WRONG_SIGN = (
    "Знак не збігається з твоєю метою. Напиши додатне число — я сам "
    "зроблю мінус для схуднення / плюс для набору."
)
WEEKLY_DELTA_SAVED = "📈 Тижнева ціль: <b>{delta:+.2f} кг/тиждень</b>."
WEEKLY_DELTA_NOT_FOR_MAINTAIN = (
    "Для мети «Підтримувати вагу» тижнева дельта не потрібна — пропускаю."
)

GOALS_HEADER = "🎯 <b>Цілі</b> · {name}"
GOALS_NO_PROFILE = (
    "Ще не пройшов онбординг. Натисни /start, я задам кілька питань — "
    "після цього /goals покаже план."
)
GOALS_NO_TARGET = (
    "Цільова вага не задана. Постав її через /profile → 🏁 Цільова вага."
)
GOALS_PROJECTION_AT_TARGET = "🎉 Ти вже на цільовій вазі! Тримай темп."
GOALS_PROJECTION_ZERO_DELTA = (
    "Тижнева дельта = 0 — постав ціль через /goals → 📈 Тижнева ціль."
)
GOALS_PROJECTION_WRONG_DIRECTION = (
    "Тижнева дельта спрямована не туди — наприклад, мета «схуднути», "
    "але ти задав плюс. Поправ через /goals → 📈 Тижнева ціль."
)
GOALS_STATUS_AHEAD    = "🟢 Випереджаєш план"
GOALS_STATUS_ON_TRACK = "🟡 У графіку"
GOALS_STATUS_BEHIND   = "🔴 Відстаєш від плану"

# --- Reply-keyboard button labels (must match the strings used in main_menu_keyboard) ---
# When a user taps one of these buttons, Telegram sends its label as a message.
# webhook.py intercepts these labels and routes them to the corresponding command.
BTN_ASK = "🤖 Запитати ШІ"
BTN_FAV = "⭐ Улюблені"
BTN_WATER = "💧 +250мл"
BTN_TODAY = "📊 День"
BTN_SUGGEST = "🍽️ Ідея страви"
BTN_PROFILE = "⚙️ Профіль"
BTN_YESTERDAY = "📆 Вчора"
BTN_MEALS = "📋 Мої страви"
BTN_DASHBOARD = "📱 Dashboard"
BTN_SCAN = "🔢 Сканер"
BTN_MENU_OCR = "📋 Меню"

MENU_BUTTON_LABELS = {BTN_ASK, BTN_FAV, BTN_WATER, BTN_MEALS, BTN_SUGGEST, BTN_PROFILE, BTN_SCAN, BTN_MENU_OCR}


# --- Water tracker ---

def format_water(total_ml: int, target_ml: int) -> str:
    total_ml = max(0, int(total_ml))
    target_ml = max(1, int(target_ml))
    blocks = 10
    ratio = total_ml / target_ml
    filled = max(0, min(blocks, round(ratio * blocks)))
    bar = "▰" * filled + "▱" * (blocks - filled)
    total_l = total_ml / 1000
    target_l = target_ml / 1000
    pct = round(ratio * 100)
    header = f"💧 <b>Сьогодні: {total_l:.2f} / {target_l:.1f} л</b>"
    if pct > 100:
        tail = f"{bar} ({pct}%)"
    elif pct == 100:
        tail = f"{bar} — ціль! 🎯"
    else:
        tail = f"{bar} ({pct}%)"
    return f"{header}\n{tail}"


WATER_FAV_EMPTY = "⭐ Поки порожньо. Зірочка на будь-якій страві додає її сюди."
WATER_RECENT_EMPTY = "📭 Ще немає записаних страв. Надішли фото або текст."
WATER_UNDO_EMPTY = "Нічого відкочувати сьогодні."
WATER_GOAL_PROMPT = "🎯 Обери денну ціль по воді:"
WATER_GOAL_SAVED = "🎯 Ціль оновлено: {target} мл/день."
RELOG_DONE = "✅ Записав <b>{dish}</b> в {meal_type}. Скасувати можна протягом 10 хв."
RELOG_FAILED = "Не вдалося повторити страву. Спробуй ще раз."
UNDO_EXPIRED = "Минуло більше 10 хв — відкат уже недоступний."
UNDO_DONE = "Повернув ✅"
FAV_ADDED = "⭐ У списку улюблених"
FAV_REMOVED = "Прибрав з улюблених"
FAV_EMPTY_LIST = (
    "⭐ Улюблених поки немає.\n"
    "Запиши будь-яку страву — і тисни зірочку під нею, щоб додати."
)
RECENT_EMPTY_LIST = (
    "📭 Ще немає страв для повтору.\n"
    "Запиши першу — і вона з'явиться тут."
)


def format_meal_list_entry(m: dict) -> str:
    desc = (m.get("description") or "").strip()
    if len(desc) > 40:
        desc = desc[:38] + "…"
    cal = round(m.get("calories") or 0)
    star = "⭐ " if m.get("is_favorite") else ""
    return f"{star}{_esc(desc)} · {cal} ккал"
