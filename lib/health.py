"""Health profile utilities (F-1).

Stores allergens + chronic conditions per user. Used to:
1) Render the /health command's status display.
2) Inject context into vision/text/chat prompts so the AI flags triggers.

Canonical IDs are stable strings stored in the DB and used in callbacks.
User-facing labels are Ukrainian (until F-2 i18n adds English).
LLM-facing labels are English (so prompts work regardless of model locale).
"""
from typing import Iterable, Optional


# Canonical allergens (stable IDs ↔ Ukrainian labels).
ALLERGENS: dict[str, str] = {
    "peanut":     "арахіс",
    "tree_nut":   "горіхи",
    "dairy":      "молочне",
    "egg":        "яйце",
    "soy":        "соя",
    "gluten":     "глютен (пшениця)",
    "fish":       "риба",
    "shellfish":  "морепродукти / ракоподібні",
    "sesame":     "кунжут",
    "mustard":    "гірчиця",
    "sulphites":  "сульфіти",
    "celery":     "селера",
    "lupin":      "люпин",
    "mollusks":   "мідії / устриці",
}

# Canonical conditions (top dietary-impact).
CONDITIONS: dict[str, str] = {
    "crohns":         "хвороба Крона",
    "ibs":            "СРК (IBS)",
    "celiac":         "целіакія",
    "diabetes_t1":    "діабет 1-го типу",
    "diabetes_t2":    "діабет 2-го типу",
    "hypertension":   "гіпертонія",
    "pcos":           "СПКЯ (PCOS)",
    "kidney":         "хвороба нирок",
    "thyroid":        "щитоподібна залоза",
    "gestational":    "вагітність",
}

# Per-condition guidance the LLM uses to flag risky ingredients.
_CONDITION_GUIDANCE: dict[str, str] = {
    "crohns":      "For Crohn's, flag insoluble fiber, raw cruciferous, lactose, high-FODMAP, alcohol, caffeine, very spicy.",
    "ibs":         "For IBS, flag high-FODMAP foods, lactose, sorbitol, large legume servings.",
    "celiac":      "For celiac, flag any gluten (wheat / barley / rye / cross-contamination).",
    "diabetes_t1": "For T1 diabetes, mention carb count and rough insulin context; flag added sugars, refined carbs, GI > 70.",
    "diabetes_t2": "For T2 diabetes, flag added sugars, refined carbs, GI > 70, and large carb portions.",
    "hypertension":"For hypertension, flag sodium > 600 mg, processed meats, soy sauce, pickles.",
    "pcos":        "For PCOS, mention insulin response; flag added sugars, refined carbs, very high GI.",
    "kidney":      "For kidney disease, flag high potassium (banana, potato, tomato), high phosphorus (dairy, processed cheese), and excess protein.",
    "thyroid":     "For thyroid issues, mention iodine; flag heavy raw cruciferous if user has hypothyroidism.",
    "gestational": "For gestational concerns, flag raw fish, raw eggs, soft cheese, alcohol, high-mercury fish, deli meats.",
}

# Aliases: Ukrainian + common English variants → canonical id.
_ALIASES: dict[str, str] = {
    # Allergens
    "арахіс": "peanut", "peanuts": "peanut", "земляний горіх": "peanut",
    "горіхи": "tree_nut", "tree nuts": "tree_nut", "горіх": "tree_nut",
    "молочне": "dairy", "молоко": "dairy", "lactose": "dairy", "лактоза": "dairy",
    "яйце": "egg", "яйця": "egg", "eggs": "egg",
    "соя": "soy", "соєвий": "soy",
    "глютен": "gluten", "пшениця": "gluten", "wheat": "gluten",
    "риба": "fish",
    "морепродукти": "shellfish", "креветки": "shellfish", "ракоподібні": "shellfish",
    "кунжут": "sesame", "сезам": "sesame",
    "гірчиця": "mustard",
    "сульфіти": "sulphites", "sulfites": "sulphites",
    "селера": "celery",
    "люпин": "lupin",
    "мідії": "mollusks", "устриці": "mollusks",
    # Conditions
    "крон": "crohns", "crohn": "crohns", "crohns disease": "crohns", "хвороба крона": "crohns",
    "срк": "ibs", "синдром подразненого кишечника": "ibs",
    "целіакія": "celiac", "celiac disease": "celiac",
    "діабет 1": "diabetes_t1", "діабет першого типу": "diabetes_t1",
    "type 1 diabetes": "diabetes_t1", "t1d": "diabetes_t1",
    "діабет 2": "diabetes_t2", "діабет другого типу": "diabetes_t2",
    "type 2 diabetes": "diabetes_t2", "t2d": "diabetes_t2", "діабет": "diabetes_t2",
    "гіпертонія": "hypertension", "тиск": "hypertension", "high bp": "hypertension",
    "спкя": "pcos",
    "нирки": "kidney", "хвороба нирок": "kidney", "kidney disease": "kidney",
    "щитоподібна": "thyroid", "щитовидна": "thyroid",
    "гіпотиреоз": "thyroid", "гіпертиреоз": "thyroid",
    "вагітність": "gestational", "pregnancy": "gestational", "вагітна": "gestational",
}


def normalize(value: str) -> str:
    """Canonicalize a user-typed allergen/condition string to a stable id.

    Returns the original lowercased string if no alias matches and it isn't
    already a canonical id — the caller decides whether to keep or drop it.
    """
    if not value:
        return ""
    s = value.strip().lower()
    if s in _ALIASES:
        return _ALIASES[s]
    s_underscored = s.replace("-", "_").replace(" ", "_")
    if s_underscored in ALLERGENS or s_underscored in CONDITIONS:
        return s_underscored
    return s


def parse_csv(raw: str, registry: dict[str, str]) -> tuple[list[str], list[str]]:
    """Parse comma-separated input into ``(canonical_ids, unknown_words)``.

    ``registry`` is one of ``ALLERGENS`` or ``CONDITIONS`` — gates which ids
    are accepted. Returns deduped ids plus the words we couldn't recognise
    so the caller can show them back to the user as a hint.
    """
    if not raw or not raw.strip():
        return [], []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    canon: list[str] = []
    unknown: list[str] = []
    for p in parts:
        nid = normalize(p)
        if nid in registry and nid not in canon:
            canon.append(nid)
        elif p:
            unknown.append(p)
    return canon, unknown


def render_labels(ids: Iterable[str], registry: dict[str, str]) -> str:
    """Render a list of canonical ids as a comma-separated UA label string."""
    labels = [registry[i] for i in ids if i in registry]
    return ", ".join(labels) if labels else "—"


def is_clear_keyword(text: str) -> bool:
    """True if user typed a 'clear / none' marker."""
    if not text:
        return True
    s = text.strip().lower()
    return s in ("none", "немає", "нема", "no", "ні", "0", "-", "—", "/clear", "clear", "/none")


def health_addendum_text(allergens: list[str], conditions: list[str]) -> str:
    """Build the text appended to the analysis system prompt.

    Returns "" when the user has no usable health context — caller skips the
    append. Unknown allergen / condition ids are dropped silently here so the
    LLM doesn't see junk; that's also why an input of only-unknown values
    yields an empty addendum.
    """
    body: list[str] = []
    if allergens:
        eng = ", ".join(
            _canon_allergen_to_english(i) for i in allergens if i in ALLERGENS
        )
        if eng:
            body.append(
                f"- Avoid (allergies): {eng}. "
                f"If any ingredient overlaps, ALWAYS list it in allergen_flags."
            )
    for cid in conditions:
        guidance = _CONDITION_GUIDANCE.get(cid)
        if guidance:
            body.append(f"- {guidance}")
    if not body:
        return ""
    return "\n".join([
        "============================================================",
        "USER HEALTH CONTEXT — apply when filling allergen_flags / crohn_flags:",
        *body,
        "============================================================",
    ])


def addendum_for_profile(health: Optional[dict]) -> str:
    """Convenience wrapper: build the addendum from a user_health_profile row dict."""
    if not health:
        return ""
    allergens = list(health.get("allergens") or [])
    conditions = list(health.get("conditions") or [])
    return health_addendum_text(allergens, conditions)


def _canon_allergen_to_english(canon_id: str) -> str:
    return {
        "peanut":     "peanut",
        "tree_nut":   "tree nuts",
        "dairy":      "dairy / lactose",
        "egg":        "egg",
        "soy":        "soy",
        "gluten":     "gluten / wheat",
        "fish":       "fish",
        "shellfish":  "shellfish / crustaceans",
        "sesame":     "sesame",
        "mustard":    "mustard",
        "sulphites":  "sulphites",
        "celery":     "celery",
        "lupin":      "lupin",
        "mollusks":   "mollusks",
    }.get(canon_id, canon_id.replace("_", " "))
