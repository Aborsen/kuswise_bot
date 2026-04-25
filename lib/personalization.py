"""Photo-correction learning loop (F-7).

Two surfaces:

1. ``record_correction()`` — audit trail. Called when the user manually
   corrects an analysis (typed text override), recalculates, or picks an
   alternate that wasn't the model's top guess.

2. ``upsert_alias_from_meal()`` + ``aliases_prompt_block()`` — derived
   personalization. After each accepted meal we EWMA-update the user's
   "usual" portion + macros for that dish name. On every subsequent
   vision/text call we prepend the top-N aliases as a few-shot block so
   the model anchors its estimate to the user's actual habits.

Both are best-effort and wrapped at call sites — failures here NEVER
break meal-save UX.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional


# EWMA weight given to the new sample. Lower α = more stable defaults
# but slower to track real change. 0.3 is a reasonable midpoint.
_EWMA_ALPHA = 0.3

# Below this sample count we don't include the alias in the few-shot
# block — one observation isn't enough to generalize.
_MIN_SAMPLES_FOR_PROMPT = 2

# Max aliases prepended to any single analysis prompt. Caps token cost.
_PROMPT_MAX_ALIASES = 8

# Minimum chars / max chars for the alias key after normalization.
_MIN_ALIAS_LEN = 3
_MAX_ALIAS_LEN = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_alias(dish_name: str) -> str:
    """Lowercase + strip + length-clamp. Returns "" if unusable."""
    if not dish_name:
        return ""
    s = dish_name.strip().lower()
    # Collapse internal whitespace.
    s = " ".join(s.split())
    if len(s) < _MIN_ALIAS_LEN:
        return ""
    return s[:_MAX_ALIAS_LEN]


# ---------- Corrections audit trail ----------

def record_correction(
    conn,
    user_id: int,
    source: str,
    original: dict,
    corrected: dict,
) -> None:
    """Persist a correction event.

    ``source`` is one of: "manual" (user typed an override after a photo),
    "recalc" (user requested a recalculation that yielded different macros),
    or "pick_alt" (F-6 user picked a non-top top_guesses candidate).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO corrections
                   (user_id, source, original_json, corrected_json, created_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    user_id,
                    source[:32],
                    json.dumps(original or {}, ensure_ascii=False),
                    json.dumps(corrected or {}, ensure_ascii=False),
                    _now_iso(),
                ),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        # Audit trail is best-effort.


def recent_corrections(conn, user_id: int, limit: int = 20) -> list[dict]:
    """Latest ``limit`` correction rows for ``user_id``, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, source, original_json, corrected_json, created_at
               FROM corrections
               WHERE user_id = %s
               ORDER BY created_at DESC
               LIMIT %s""",
            (user_id, int(limit)),
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            orig = json.loads(r[2] or "{}")
        except (TypeError, ValueError):
            orig = {}
        try:
            corr = json.loads(r[3] or "{}")
        except (TypeError, ValueError):
            corr = {}
        out.append({
            "id": r[0],
            "source": r[1],
            "original": orig,
            "corrected": corr,
            "created_at": r[4],
        })
    return out


# ---------- Aliases (EWMA-tracked usual portions) ----------

def _portion_grams_from_analysis(analysis: dict) -> Optional[float]:
    """Best-effort gram extraction from an analysis dict.

    Prefers the sum of ``ingredients[].estimated_grams``; falls back to a
    cheap regex over ``estimated_portion`` if the ingredient list is empty.
    Returns None when nothing usable is present.
    """
    ings = analysis.get("ingredients") or []
    if isinstance(ings, list) and ings:
        total = 0.0
        any_gram = False
        for ing in ings:
            if not isinstance(ing, dict):
                continue
            try:
                g = float(ing.get("estimated_grams") or 0)
            except (TypeError, ValueError):
                continue
            if g > 0:
                total += g
                any_gram = True
        if any_gram:
            return total

    portion = analysis.get("estimated_portion") or ""
    # Match the first integer (with optional space) before "г" / "g" / "грам".
    import re
    m = re.search(r"(\d{2,4})\s*(?:г|g|грам)", str(portion).lower())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def upsert_alias_from_meal(
    conn,
    user_id: int,
    analysis: dict,
) -> None:
    """EWMA-update the user's alias for the dish in ``analysis``.

    Called when a meal is accepted. Skips silently when the dish name or
    portion can't be extracted. Failures are swallowed — never break meal save.
    """
    try:
        dish = (analysis.get("dish_name") or analysis.get("description") or "").strip()
        alias = _normalize_alias(dish)
        if not alias:
            return

        nutrition = analysis.get("nutrition") or {}
        try:
            kcal = float(nutrition.get("calories") or 0)
        except (TypeError, ValueError):
            kcal = 0.0
        if kcal <= 0:
            return

        try:
            protein_g = float(nutrition.get("protein_g") or 0)
            carbs_g   = float(nutrition.get("carbs_g")   or 0)
            fat_g     = float(nutrition.get("fat_g")     or 0)
        except (TypeError, ValueError):
            protein_g = carbs_g = fat_g = 0.0

        grams = _portion_grams_from_analysis(analysis) or 0.0

        a = _EWMA_ALPHA
        # The CASE expressions handle the "first sample" path: when the row
        # is fresh, default_* columns are zero/null — we replace rather than blend.
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_food_aliases
                       (user_id, alias, normalized_name,
                        default_grams, default_kcal,
                        default_protein_g, default_fat_g, default_carbs_g,
                        sample_count, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                   ON CONFLICT (user_id, alias) DO UPDATE SET
                       normalized_name   = EXCLUDED.normalized_name,
                       default_grams     = COALESCE(user_food_aliases.default_grams, 0) * (1 - %s) + EXCLUDED.default_grams * %s,
                       default_kcal      = COALESCE(user_food_aliases.default_kcal,  0) * (1 - %s) + EXCLUDED.default_kcal  * %s,
                       default_protein_g = COALESCE(user_food_aliases.default_protein_g, 0) * (1 - %s) + EXCLUDED.default_protein_g * %s,
                       default_fat_g     = COALESCE(user_food_aliases.default_fat_g,     0) * (1 - %s) + EXCLUDED.default_fat_g     * %s,
                       default_carbs_g   = COALESCE(user_food_aliases.default_carbs_g,   0) * (1 - %s) + EXCLUDED.default_carbs_g   * %s,
                       sample_count = user_food_aliases.sample_count + 1,
                       updated_at = EXCLUDED.updated_at""",
                (
                    user_id, alias, dish[:80],
                    grams, kcal, protein_g, fat_g, carbs_g,
                    _now_iso(),
                    a, a,  a, a,  a, a,  a, a,  a, a,
                ),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def recent_aliases(conn, user_id: int, limit: int = _PROMPT_MAX_ALIASES) -> list[dict]:
    """Top-N most-recently-touched aliases for the user."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT alias, normalized_name,
                      default_grams, default_kcal,
                      default_protein_g, default_fat_g, default_carbs_g,
                      sample_count, updated_at
               FROM user_food_aliases
               WHERE user_id = %s
               ORDER BY updated_at DESC NULLS LAST
               LIMIT %s""",
            (user_id, int(limit)),
        )
        rows = cur.fetchall()
    return [{
        "alias":             r[0],
        "normalized_name":   r[1],
        "default_grams":     float(r[2] or 0),
        "default_kcal":      float(r[3] or 0),
        "default_protein_g": float(r[4] or 0),
        "default_fat_g":     float(r[5] or 0),
        "default_carbs_g":   float(r[6] or 0),
        "sample_count":      int(r[7] or 0),
        "updated_at":        r[8],
    } for r in rows]


def delete_alias(conn, user_id: int, alias: str) -> bool:
    """User-initiated alias deletion (used by /aliases del flow). Returns True if a row was deleted."""
    norm = _normalize_alias(alias)
    if not norm:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_food_aliases WHERE user_id = %s AND alias = %s",
            (user_id, norm),
        )
        n = cur.rowcount or 0
    conn.commit()
    return n > 0


# ---------- Prompt assembly ----------

def aliases_prompt_block(
    conn,
    user_id: int,
    limit: int = _PROMPT_MAX_ALIASES,
) -> str:
    """Build a few-shot block to prepend to vision/text analysis prompts.

    Returns "" when the user has no usable aliases (fewer than
    ``_MIN_SAMPLES_FOR_PROMPT`` samples, or no aliases at all). Caller
    should append to the system prompt only when this returns non-empty.
    """
    try:
        rows = recent_aliases(conn, user_id, limit=limit)
    except Exception:
        return ""
    body: list[str] = []
    for r in rows:
        if r["sample_count"] < _MIN_SAMPLES_FOR_PROMPT:
            continue
        grams = r["default_grams"]
        kcal  = r["default_kcal"]
        if kcal <= 0:
            continue
        portion_txt = f"~{int(round(grams))}г, " if grams > 0 else ""
        body.append(
            f'- "{r["normalized_name"]}" зазвичай {portion_txt}'
            f'{int(round(kcal))} ккал'
        )
    if not body:
        return ""
    header = (
        "USER PERSONALIZATION (recent meals this user has accepted, with the "
        "portions they typically use):"
    )
    footer = (
        "If the photo / description matches one of these dishes closely, "
        "anchor your portion estimate on the user's usual amount. Use a fresh "
        "estimate when the appearance clearly differs."
    )
    return "\n".join([header, *body, footer])
