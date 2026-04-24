"""Message formatting helpers for Telegram replies (Ukrainian, з гумором)."""
import random
from datetime import datetime

from lib.config import LOCAL_TZ, macro_gram_targets, macro_gram_targets_from_profile


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
    return first_name.strip() if (first_name and first_name.strip()) else "друже"


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
        name = ing.get("name", "?")
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
            lines.append(
                f"  {icon} {a.get('allergen', '?').capitalize()} "
                f"(впевненість: {a.get('confidence', '?')}) — у складі: {a.get('ingredient', 'цієї страви')}"
            )

    if crohn_flags:
        lines.append("")
        lines.append("💡 <b>Нотатки щодо здоров'я (для кату):</b>")
        for c in crohn_flags:
            icon = _SEVERITY_ICON.get((c.get("severity") or "").lower(), "🟡")
            lines.append(
                f"  {icon} {c.get('concern', 'питання')} "
                f"({c.get('ingredient', '?')})"
            )

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
    label = _GI_UA.get(level, level.capitalize())
    return f"{icon} {label}" + (f" — {note}" if note else "")


# --- Preview (before user accepts) ---

def format_meal_preview(meal_type: str, analysis: dict) -> str:
    """Preview message shown after AI analysis, before user taps Accept."""
    dish = analysis.get("dish_name") or "Страва"
    meal_ua = _MEAL_TYPE_UA.get(meal_type.lower(), meal_type.capitalize())
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

    # Show the AI's portion reasoning so the user can sanity-check the grams
    portion_reasoning = (analysis.get("portion_reasoning") or "").strip()
    if portion_reasoning:
        lines.append("")
        lines.append(f"📏 <i>{portion_reasoning}</i>")

    assessment = analysis.get("overall_assessment")
    if assessment:
        lines.append("")
        lines.append(f"💬 {assessment}")

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
    dish = analysis.get("dish_name") or "Страва"
    date_display = _ua_date_long(datetime.now(LOCAL_TZ))
    meal_ua = _MEAL_TYPE_UA.get(meal_type.lower(), meal_type.capitalize())

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
        lines.append(f"💬 {assessment}")

    lines.append("")
    lines.append(
        f"📊 Разом за день: {round(today_log.get('calories', 0))} / {daily_cal_target} ккал"
    )

    if first_name:
        lines.append(f"<i>Тримайся, {first_name}! 💪</i>")

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
        desc = m.get("description", "")[:50]
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


def format_today_progress(
    log: dict,
    daily_cal_target: int,
    first_name: str | None = None,
    profile: dict | None = None,
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

    return (
        f"📊 <b>Прогрес на сьогодні ({date_display})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
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
        mt = _MEAL_TYPE_UA.get(mt_raw, mt_raw.capitalize() or "—")
        desc = (m.get("description") or "")[:60]
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
        mt = _MEAL_TYPE_UA.get((m.get("meal_type") or "").lower(), (m.get("meal_type") or "").capitalize())
        lines.append(f"🕐 <b>{mt}</b> — {m.get('description', '')}")
        lines.append(
            f"   🔥 {round(m.get('calories', 0))} ккал | "
            f"🥩 {round(m.get('protein_g', 0))}г Б | "
            f"🍚 {round(m.get('carbs_g', 0))}г В | "
            f"🧈 {round(m.get('fat_g', 0))}г Ж"
        )
        if m.get("allergen_warnings"):
            names = ", ".join(a.get("allergen", "?") for a in m["allergen_warnings"])
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

MENU_BUTTON_LABELS = {BTN_ASK, BTN_FAV, BTN_WATER, BTN_MEALS, BTN_SUGGEST, BTN_PROFILE}


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
    return f"{star}{desc} · {cal} ккал"
