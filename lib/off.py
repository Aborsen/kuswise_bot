"""Open Food Facts client (F-8).

Open Food Facts is a free, no-API-key product database covering 3M+
packaged foods worldwide. We use the v2 product endpoint:

    https://world.openfoodfacts.org/api/v2/product/{ean}.json

Returns ``None`` on any failure (404, network issue, malformed JSON,
missing nutriments) — callers fall back to GPT-4o text analysis.

Public surface:
    - ``looks_like_ean(s)`` — cheap pre-flight before hitting OFF
    - ``lookup_product(ean)`` — returns a normalized dict or None

The normalized dict shape:

    {
      "ean": "5449000000996",
      "name": "Coca-Cola Original 330 ml",
      "brand": "Coca-Cola",
      "image_url": "https://...",
      "product_url": "https://world.openfoodfacts.org/product/5449...",
      "per_100g": {
          "calories":  42,
          "protein_g": 0,
          "carbs_g":   10.6,
          "fat_g":     0,
          "fiber_g":   0,
          "sugar_g":   10.6,
      },
      "serving_size_g": 330,   # may be None when unknown
    }
"""
from __future__ import annotations

import re
from typing import Optional

import httpx


_OFF_URL_TEMPLATE = "https://world.openfoodfacts.org/api/v2/product/{ean}.json"
_HTTP_TIMEOUT = 6.0  # OFF is usually fast; cap so a slow lookup doesn't block

# A small custom UA helps OFF triage abuse and gives us a less rate-limited path.
_USER_AGENT = "kuswise-bot/1.0 (https://github.com/Aborsen/kuswise_bot)"


def looks_like_ean(s: str | None) -> bool:
    """Cheap shape check — UPC-A/E (8/12) and EAN-8/13 are all 8-13 digit codes."""
    if not s:
        return False
    s = str(s).strip()
    return bool(re.fullmatch(r"\d{8,13}", s))


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize(product: dict, ean: str) -> Optional[dict]:
    """Map an OFF product dict to our shape. Returns None if unusable."""
    if not isinstance(product, dict):
        return None

    nutriments = product.get("nutriments") or {}

    # Calories: OFF returns either ``energy-kcal_100g`` (preferred) or
    # ``energy-kj_100g`` (when only kJ is reported). Convert kJ → kcal at 4.184.
    kcal = (
        _to_float(nutriments.get("energy-kcal_100g"))
        or _to_float(nutriments.get("energy_100g"))   # plain "energy" is kJ on OFF
    )
    if kcal is None:
        kj = _to_float(nutriments.get("energy-kj_100g"))
        if kj is not None:
            kcal = kj / 4.184

    if kcal is None or kcal <= 0:
        # No usable calorie data — caller falls back to GPT analysis.
        return None

    name = (product.get("product_name") or product.get("generic_name") or "").strip()
    if not name:
        # OFF sometimes only has localized names — try Ukrainian / English fallbacks.
        name = (product.get("product_name_uk")
                or product.get("product_name_ru")
                or product.get("product_name_en")
                or "").strip()
    if not name:
        return None

    brand = (product.get("brands") or "").split(",")[0].strip()

    serving_size = product.get("serving_size") or ""
    serving_size_g = _parse_serving_size_grams(serving_size)

    return {
        "ean":           ean,
        "name":          name[:120],
        "brand":         brand[:80],
        "image_url":     product.get("image_url") or product.get("image_front_url") or "",
        "product_url":   f"https://world.openfoodfacts.org/product/{ean}",
        "per_100g": {
            "calories":  round(kcal, 1),
            "protein_g": round(_to_float(nutriments.get("proteins_100g"))     or 0, 1),
            "carbs_g":   round(_to_float(nutriments.get("carbohydrates_100g")) or 0, 1),
            "fat_g":     round(_to_float(nutriments.get("fat_100g"))           or 0, 1),
            "fiber_g":   round(_to_float(nutriments.get("fiber_100g"))         or 0, 1),
            "sugar_g":   round(_to_float(nutriments.get("sugars_100g"))        or 0, 1),
        },
        "serving_size_g": serving_size_g,
    }


def _parse_serving_size_grams(serving_size: str) -> Optional[float]:
    """Pull a gram value out of OFF's ``serving_size`` free-text field.

    Examples seen in real OFF data: '30 g', '125g', '30g (1 bar)', '1 cup (250ml)'.
    Returns the first plausible gram value, or None.
    """
    if not serving_size:
        return None
    m = re.search(r"(\d{1,4})\s*(?:g|г|грам)", serving_size.lower())
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if 1 <= v <= 5000:  # sanity range for a single serving
        return v
    return None


def lookup_product(ean: str) -> Optional[dict]:
    """Fetch + normalize an OFF product. Returns None on any failure."""
    if not looks_like_ean(ean):
        return None

    url = _OFF_URL_TEMPLATE.format(ean=ean.strip())
    try:
        resp = httpx.get(
            url,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except (ValueError, KeyError):
        return None

    # OFF returns {"status":1, "product":{...}} on hit, {"status":0} on miss.
    if not isinstance(data, dict) or data.get("status") != 1:
        return None

    return _normalize(data.get("product") or {}, ean.strip())


# ---------- Per-100g → portion math ----------

def macros_for_grams(per_100g: dict, grams: float) -> dict:
    """Scale a per-100g nutriments dict to a specific portion size.

    Returns the same shape as the analysis nutrition block so callers can
    plug straight into ``save_meal``.
    """
    factor = float(grams) / 100.0
    return {
        "calories":  round(float(per_100g.get("calories")  or 0) * factor, 1),
        "protein_g": round(float(per_100g.get("protein_g") or 0) * factor, 1),
        "carbs_g":   round(float(per_100g.get("carbs_g")   or 0) * factor, 1),
        "fat_g":     round(float(per_100g.get("fat_g")     or 0) * factor, 1),
        "fiber_g":   round(float(per_100g.get("fiber_g")   or 0) * factor, 1),
        "sugar_g":   round(float(per_100g.get("sugar_g")   or 0) * factor, 1),
    }


def product_to_analysis(product: dict, grams: float) -> dict:
    """Produce an analysis-shaped dict for a barcode meal at the chosen portion.

    Slots into the existing save_meal pipeline so post-meal flows (streaks,
    aliases, daily log) work unchanged.
    """
    nutrition = macros_for_grams(product["per_100g"], grams)
    name = product["name"]
    if product.get("brand"):
        name = f"{product['brand']} {name}".strip()
    return {
        "dish_name":         name,
        "description":       f"{name} ({int(round(grams))}г)",
        "estimated_portion": f"{int(round(grams))}г",
        "portion_reasoning": f"Сканер штрих-коду · {product['ean']}",
        "ingredients":       [],
        "allergen_flags":    [],
        "crohn_flags":       [],
        "nutrition":         nutrition,
        "glycemic_index":    {"level": "", "note": ""},
        "overall_assessment": "",
        # Marker so we can distinguish barcode-sourced meals later.
        "_source": {"kind": "barcode", "ean": product["ean"]},
    }
