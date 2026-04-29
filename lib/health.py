"""Health profile utilities (F-1).

Stores allergens + chronic conditions per user. Used to:
1) Render the /health command's status display.
2) Inject context into vision/text/chat prompts so the AI flags triggers.

Canonical IDs are stable strings stored in the DB and used in callbacks.
User-facing labels live in lib/i18n/dict_<locale>.json under
``health.allergens.<id>`` / ``health.conditions.<id>`` (F-2b Chunk 8 G2).
LLM-facing labels are English (so prompts work regardless of model locale).
"""
from typing import Iterable, Optional


# Canonical allergen IDs. Membership-only; user-facing labels resolve through
# lib.i18n by id (health.allergens.<id>). Treated as a tuple of stable strings.
ALLERGEN_IDS: tuple[str, ...] = (
    "peanut", "tree_nut", "dairy", "egg", "soy", "gluten", "fish",
    "shellfish", "sesame", "mustard", "sulphites", "celery", "lupin", "mollusks",
    # Added 2026-04-28 for Food-bot migration (user 699256397). These are
    # finer-grained than the EFSA-14 — kept distinct (not collapsed into
    # `gluten`/`dairy`/etc.) so the AI flags the specific item the user
    # actually reacts to, not the broader category.
    "tomato", "emmental", "rye", "rapeseed",
)

# Canonical condition IDs. Same shape as ALLERGEN_IDS.
CONDITION_IDS: tuple[str, ...] = (
    "crohns", "ibs", "celiac", "diabetes_t1", "diabetes_t2",
    "hypertension", "pcos", "kidney", "thyroid", "gestational",
)

# Backwards-compat shims: callers used to receive a {id: UA-label} dict; now we
# expose a frozenset for membership checks. Anywhere the old dict was
# iterated for labels is migrated to ``label_for(kind, id, locale)``.
ALLERGENS: frozenset[str] = frozenset(ALLERGEN_IDS)
CONDITIONS: frozenset[str] = frozenset(CONDITION_IDS)

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
# UA keys are deliberate — this dict is a parser for user-typed input (the
# user can write either UA or EN — "Crohn's disease", "crohn", or the UA  # noqa: i18n
# strings on the LHS are preserved by design. Each line tagged for the audit.
_ALIASES: dict[str, str] = {
    # Allergens
    "арахіс": "peanut", "peanuts": "peanut", "земляний горіх": "peanut",  # noqa: i18n
    "горіхи": "tree_nut", "tree nuts": "tree_nut", "горіх": "tree_nut",  # noqa: i18n
    "молочне": "dairy", "молоко": "dairy", "lactose": "dairy", "лактоза": "dairy",  # noqa: i18n
    "яйце": "egg", "яйця": "egg", "eggs": "egg",  # noqa: i18n
    "соя": "soy", "соєвий": "soy",  # noqa: i18n
    "глютен": "gluten", "пшениця": "gluten", "wheat": "gluten",  # noqa: i18n
    "риба": "fish",  # noqa: i18n
    "морепродукти": "shellfish", "креветки": "shellfish", "ракоподібні": "shellfish",  # noqa: i18n
    "кунжут": "sesame", "сезам": "sesame",  # noqa: i18n
    "гірчиця": "mustard",  # noqa: i18n
    "сульфіти": "sulphites", "sulfites": "sulphites",  # noqa: i18n
    "селера": "celery",  # noqa: i18n
    "люпин": "lupin",  # noqa: i18n
    "мідії": "mollusks", "устриці": "mollusks",  # noqa: i18n
    # Added 2026-04-28 for Food-bot migration (specific items not in EFSA-14).
    "помідор": "tomato", "помідори": "tomato", "томат": "tomato", "томати": "tomato", "tomatoes": "tomato",  # noqa: i18n
    "ементаль": "emmental", "емменталь": "emmental", "emmental cheese": "emmental",  # noqa: i18n
    "жито": "rye", "житній": "rye", "rye flour": "rye",  # noqa: i18n
    "ріпак": "rapeseed", "ріпакова олія": "rapeseed", "канола": "rapeseed", "canola": "rapeseed", "canola oil": "rapeseed",  # noqa: i18n
    # Conditions
    "крон": "crohns", "crohn": "crohns", "crohns disease": "crohns", "хвороба крона": "crohns",  # noqa: i18n
    "срк": "ibs", "синдром подразненого кишечника": "ibs",  # noqa: i18n
    "целіакія": "celiac", "celiac disease": "celiac",  # noqa: i18n
    "діабет 1": "diabetes_t1", "діабет першого типу": "diabetes_t1",  # noqa: i18n
    "type 1 diabetes": "diabetes_t1", "t1d": "diabetes_t1",
    "діабет 2": "diabetes_t2", "діабет другого типу": "diabetes_t2",  # noqa: i18n
    "type 2 diabetes": "diabetes_t2", "t2d": "diabetes_t2", "діабет": "diabetes_t2",  # noqa: i18n
    "гіпертонія": "hypertension", "тиск": "hypertension", "high bp": "hypertension",  # noqa: i18n
    "спкя": "pcos",  # noqa: i18n
    "нирки": "kidney", "хвороба нирок": "kidney", "kidney disease": "kidney",  # noqa: i18n
    "щитоподібна": "thyroid", "щитовидна": "thyroid",  # noqa: i18n
    "гіпотиреоз": "thyroid", "гіпертиреоз": "thyroid",  # noqa: i18n
    "вагітність": "gestational", "pregnancy": "gestational", "вагітна": "gestational",  # noqa: i18n
}


def label_for(kind: str, canon_id: str, locale: str = "en") -> str:
    """Locale-aware allergen / condition label.

    ``kind`` is "allergens" or "conditions". Falls back to the canonical id
    (with underscores → spaces) when the locale dict has no entry — no risk
    of returning the literal key string to the user.
    """
    from lib.i18n import t
    key = f"health.{kind}.{canon_id}"
    label = t(key, locale=locale)
    if label == key:  # not in dict; fall back to canon id, prettified
        return canon_id.replace("_", " ")
    return label


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


def parse_csv(raw: str, registry: frozenset[str] | dict[str, str]) -> tuple[list[str], list[str]]:
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


def render_labels(ids: Iterable[str], kind: str, locale: str = "en") -> str:
    """Render a list of canonical ids as a comma-separated locale-aware string.

    ``kind`` is "allergens" or "conditions" — used to namespace the dict lookup.
    """
    valid_set = ALLERGENS if kind == "allergens" else CONDITIONS
    labels = [label_for(kind, i, locale=locale) for i in ids if i in valid_set]
    return ", ".join(labels) if labels else "—"


def is_clear_keyword(text: str) -> bool:
    """True if user typed a 'clear / none' marker (UA + EN)."""
    if not text:
        return True
    s = text.strip().lower()
    return s in (
        "none", "немає", "нема", "no", "ні", "0", "-", "—", "/clear", "clear", "/none",  # noqa: i18n
    )


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
        "tomato":     "tomato",
        "emmental":   "emmental cheese",
        "rye":        "rye",
        "rapeseed":   "rapeseed / canola oil",
    }.get(canon_id, canon_id.replace("_", " "))
