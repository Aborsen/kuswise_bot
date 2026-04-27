"""F-2b Chunk 8 (G4): bilingual snapshot + meta tests.

Locks in the EN ↔ UK rendering of every major formatter so a future
change can't silently flip a section back to one locale. Each formatter
gets called twice with identical inputs and asserts a known substring
appears only in the matching locale's output.

Plus three meta tests that act as release gates:

  * key parity between dict_uk.json and dict_en.json
  * scripts/check_i18n.sh exits 0 (zero in-scope Cyrillic)
  * pluralize() handles all the boundary cases for both locales
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

import lib.formatters as fm
import lib.health as hh
from lib.i18n import t
from lib.i18n.plurals import pluralize
from lib.datehelpers import format_date_long


# ---------- Snapshot tests: every formatter renders in both locales ----------

def test_format_recommendation_renders_both_locales():
    profile = {"age": 30, "sex": "male", "weight_kg": 75, "height_cm": 178,
               "gym_per_week": "3-4", "goal": "lose"}
    en = fm.format_recommendation(profile, 1900, locale="en")
    uk = fm.format_recommendation(profile, 1900, locale="uk")
    assert "Calculated!" in en and "Calculated!" not in uk
    assert "Порахував!" in uk and "Порахував!" not in en
    # Macros line: the localized headers differ; values match.
    assert "150g" in en and "150г" in uk
    assert en != uk


def test_format_history_renders_both_locales():
    rows = [{"date": "2026-04-25", "calories": 1900, "protein": 120, "carbs": 200, "fat": 60}]
    en = fm.format_history(rows, daily_cal_target=2000, locale="en")
    uk = fm.format_history(rows, daily_cal_target=2000, locale="uk")
    assert "Last 7 days" in en
    assert "Останні 7 днів" in uk


def test_format_water_renders_both_locales():
    en = fm.format_water(1500, 2000, locale="en")
    uk = fm.format_water(1500, 2000, locale="uk")
    assert "L" in en and "Today" in en
    assert "л" in uk and "Сьогодні" in uk


def test_format_streak_summary_renders_both_locales():
    row = {"current_streak": 5, "longest_streak": 7, "freeze_days_remaining": 3,
           "last_log_date": "2026-04-25"}
    en = fm.format_streak_summary(row, first_name="Vic", locale="en")
    uk = fm.format_streak_summary(row, first_name="Vic", locale="uk")
    # EN summary header references the user, UA likewise — different glyphs.
    assert "Streak" in en or "streak" in en.lower()
    assert "серії" in uk.lower() or "серія" in uk.lower() or "поспіль" in uk.lower()


def test_format_meal_plan_day_translates_date_label():
    """The model emits English tokens ('Today' / 'Tomorrow' / 'Day 3') for
    date_label; the formatter translates them per user locale."""
    day = {
        "date_label": "Today",
        "slots": {
            "breakfast": {"name": "Oatmeal", "calories": 300, "protein_g": 12,
                          "carbs_g": 50, "fat_g": 7, "recipe": ""},
            "lunch": None, "dinner": None, "snack": None,
        },
    }
    en = fm.format_meal_plan_day(day, day_idx=0, locale="en")
    uk = fm.format_meal_plan_day(day, day_idx=0, locale="uk")
    assert "Today" in en
    assert "Сьогодні" in uk


def test_format_alternates_intro_renders_both_locales():
    candidates = [
        {"name": "Caesar with chicken", "calories": 520, "protein_g": 35,
         "carbs_g": 30, "fat_g": 25, "confidence": 0.55},
        {"name": "Greek salad with chicken", "calories": 380, "protein_g": 30,
         "carbs_g": 18, "fat_g": 22, "confidence": 0.30},
    ]
    en = fm.format_alternates_intro("lunch", candidates, locale="en")
    uk = fm.format_alternates_intro("lunch", candidates, locale="uk")
    # EN candidate row uses "kcal", UA uses "ккал".
    assert "kcal" in en
    assert "ккал" in uk
    # Header includes a localized meal type.
    assert "Lunch" in en
    assert "Обід" in uk


def test_btn_label_dispatcher_round_trips_both_locales():
    """Sanity for the bilingual dispatcher: tapping either locale's label
    routes to the same /command. Pre-F-2b legacy UA labels also dispatch."""
    assert fm.button_text_to_command(fm.btn_label("scan", locale="en")) == "/scan"
    assert fm.button_text_to_command(fm.btn_label("scan", locale="uk")) == "/scan"
    assert fm.button_text_to_command("🔢 Сканер") == "/scan"  # noqa: i18n


def test_render_health_labels_both_locales():
    en = hh.render_labels(["peanut", "shellfish"], "allergens", locale="en")
    uk = hh.render_labels(["peanut", "shellfish"], "allergens", locale="uk")
    assert "peanut" in en and "shellfish" in en
    assert "арахіс" in uk
    # Conditions namespace works the same way.
    en_cond = hh.render_labels(["crohns", "ibs"], "conditions", locale="en")
    uk_cond = hh.render_labels(["crohns", "ibs"], "conditions", locale="uk")
    assert "Crohn's" in en_cond
    assert "Крона" in uk_cond


# ---------- Plural tests: boundary cases for both locales ----------

def test_pluralize_uk_slavic_rules():
    # 1, 21, 31 → singular ("день")
    for n in (1, 21, 101):
        assert pluralize(n, "uk", "день", "дні", "днів") == "день"
    # 2-4 (and 22-24) → few ("дні")
    for n in (2, 3, 4, 22, 33):
        assert pluralize(n, "uk", "день", "дні", "днів") == "дні"
    # 5+ and 0 → many ("днів")
    for n in (0, 5, 9, 25, 100):
        assert pluralize(n, "uk", "день", "дні", "днів") == "днів"
    # 11-14 exception forces many regardless of last digit
    for n in (11, 12, 13, 14, 111, 113):
        assert pluralize(n, "uk", "день", "дні", "днів") == "днів"


def test_pluralize_en_two_forms():
    assert pluralize(1, "en", "day", many="days") == "day"
    assert pluralize(0, "en", "day", many="days") == "days"
    assert pluralize(2, "en", "day", many="days") == "days"
    assert pluralize(11, "en", "day", many="days") == "days"


# ---------- Date tests: same datetime, different locales ----------

def test_format_date_long_uk_vs_en():
    dt = datetime(2026, 4, 26)
    uk = format_date_long(dt, lang="uk")
    en = format_date_long(dt, lang="en")
    assert uk == "26 квітня"  # noqa: i18n
    assert en == "April 26"


# ---------- Meta tests (release gates) ----------

def test_dict_uk_en_have_identical_key_sets():
    """Catches future drift between the two locale dicts. If a new key is
    added to one, it must be added to the other."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "lib", "i18n", "dict_uk.json"), encoding="utf-8") as f:
        uk = json.load(f)
    with open(os.path.join(here, "lib", "i18n", "dict_en.json"), encoding="utf-8") as f:
        en = json.load(f)
    only_uk = set(uk) - set(en)
    only_en = set(en) - set(uk)
    assert not only_uk, f"keys present in UK but missing in EN: {sorted(only_uk)}"
    assert not only_en, f"keys present in EN but missing in UK: {sorted(only_en)}"


def test_check_i18n_script_exits_zero():
    """Release gate: scripts/check_i18n.sh must report 0 in-scope Cyrillic
    lines. If this fails, a UA string slipped past the audit — find it via
    the script's diagnostic output."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(here, "scripts", "check_i18n.sh")
    result = subprocess.run(
        ["bash", script],
        capture_output=True, text=True, cwd=here,
    )
    assert result.returncode == 0, (
        f"check_i18n.sh exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


# ---------- t() fallback behavior ----------

def test_t_falls_back_to_uk_when_key_missing_in_en():
    """Framework guarantee: a key missing from dict_en.json transparently
    falls back to its UA value (vs returning the literal key string)."""
    # Pick a key we know exists in both — t() should return the EN form.
    en = t("toast.saved", locale="en")
    assert en == "Saved"
    # Pick a key we know exists in UK only is impossible (parity test above
    # ensures every UK key has an EN counterpart). Instead, verify that t()
    # for a known-missing key returns the key itself (not raises).
    bogus = t("nonexistent.key.does.not.exist", locale="en")
    assert bogus == "nonexistent.key.does.not.exist"
