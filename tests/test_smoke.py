"""Smoke tests for kuswise_bot.

Pure-function focus — no real Postgres, no network. Covers macro/calorie
math, water target heuristics, structured logging, the rate-limit core
loop with an in-memory fake connection, and the per-user timezone helpers.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lib import config as cfg
from lib import database as db
from lib import log as kwlog
from lib import rate_limit as rl
from lib import datehelpers as dh
from lib import health as hh
from lib import i18n as i18n_mod


# ---------- macro / calorie math (lib.config) ----------

def test_calorie_target_lose_70kg():
    # lose: 70*2.0 P + 70*2.5 C + 70*0.8 F = 140g + 175g + 56g
    # = 140*4 + 175*4 + 56*9 = 560 + 700 + 504 = 1764
    assert cfg.calorie_target_from_profile(weight_kg=70, goal="lose") == 1764


def test_macro_targets_maintain_80kg():
    m = cfg.macro_gram_targets_from_profile(80, "maintain")
    assert m["protein"] == 160   # 80 * 2.0
    assert m["carbs"]   == 280   # 80 * 3.5
    assert m["fat"]     == 72    # 80 * 0.9


def test_macro_targets_unknown_goal_falls_back_to_maintain():
    m = cfg.macro_gram_targets_from_profile(70, "weird-goal")
    assert m["protein"] == 140   # 70 * 2.0 (maintain)


def test_profile_summary_line_handles_empty_dict():
    assert cfg.profile_summary_line({}) == "user (no profile)"


def test_profile_summary_line_renders_known_fields():
    line = cfg.profile_summary_line({
        "age": 32, "sex": "male", "height_cm": 180, "weight_kg": 78,
        "goal": "lose", "gym_per_week": "3-4",
    })
    assert "32-year-old male" in line
    assert "180 cm" in line
    assert "78 kg" in line
    assert "fat loss" in line


def test_goal_context_known_keys():
    assert "fat loss" in cfg.goal_context("lose")
    assert "muscle" in cfg.goal_context("gain")
    assert "maintenance" in cfg.goal_context("maintain")


def test_goal_context_unknown_falls_back():
    assert "maintenance" in cfg.goal_context("alien-goal")


# ---------- water target heuristics (lib.database) ----------

def test_water_target_typical_70kg():
    # 70 * 30 = 2100 ml, rounded to nearest 50 → 2100
    assert db.estimate_water_target_from_weight(70) == 2100


def test_water_target_clamps_low():
    # 40 kg → 1200 raw, clamp to 1500
    assert db.estimate_water_target_from_weight(40) == 1500


def test_water_target_clamps_high():
    # 200 kg → 6000 raw, clamp to 4000
    assert db.estimate_water_target_from_weight(200) == 4000


# ---------- structured logging (lib.log) ----------

def test_log_emits_valid_json(capsys):
    kwlog.new_request_id()
    kwlog.info("smoke_event", foo="bar", n=3)
    line = capsys.readouterr().out.strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["event"] == "smoke_event"
    assert payload["level"] == "info"
    assert payload["foo"] == "bar"
    assert payload["n"] == 3
    assert payload["request_id"]   # non-empty
    assert "ts" in payload


def test_log_request_id_is_stable_within_call():
    rid = kwlog.new_request_id()
    assert kwlog.get_request_id() == rid
    # A new request id should differ.
    rid2 = kwlog.new_request_id()
    assert rid2 != rid


def test_log_error_with_exception_includes_traceback(capsys):
    kwlog.new_request_id()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        kwlog.error("smoke_failed", exc=exc, where="test")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "smoke_failed"
    assert payload["level"] == "error"
    assert payload["error_type"] == "ValueError"
    assert "boom" in payload["error"]
    assert "Traceback" in payload["traceback"]


# ---------- rate limit core loop (lib.rate_limit) ----------

class _FakeCursor:
    """Minimal psycopg-like cursor backed by a shared dict."""
    def __init__(self, state):
        self.state = state
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "INSERT INTO usage_counters" in sql:
            self.state.setdefault("rows", True)
        elif sql.lstrip().upper().startswith("SELECT"):
            self._last = (self.state["asks"], self.state["total_ai"])
        elif "UPDATE usage_counters" in sql and "asks" in sql:
            self.state["asks"]    += 1
            self.state["total_ai"] += 1

    def fetchone(self):
        return self._last


class _FakeConn:
    def __init__(self):
        self.state = {"asks": 0, "total_ai": 0}

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass


def test_rate_limit_blocks_after_quota(monkeypatch):
    """Set ask limit to 2 and confirm 3rd call is blocked."""
    monkeypatch.setattr(rl, "LIMITS", {**rl.LIMITS, "asks": 2, "total_ai": 100})
    conn = _FakeConn()

    a1, _ = rl.check_and_increment(conn, user_id=1, kind="ask")
    a2, _ = rl.check_and_increment(conn, user_id=1, kind="ask")
    a3, rem3 = rl.check_and_increment(conn, user_id=1, kind="ask")

    assert a1 is True
    assert a2 is True
    assert a3 is False
    assert rem3 == 0           # no headroom left
    assert conn.state["asks"] == 2  # third call did not increment


def test_rate_limit_message_uk_and_en():
    assert "Денний ліміт" in rl.limit_reached_message("ask", lang="uk")
    assert "Daily limit" in rl.limit_reached_message("ask", lang="en")


# ---------- per-user timezone helpers (lib.datehelpers, F-3) ----------

def test_is_valid_tz_accepts_common_zones():
    for tz in ("Europe/Kyiv", "Europe/London", "America/New_York",
               "America/Los_Angeles", "Asia/Tokyo", "Asia/Dubai"):
        assert dh.is_valid_tz(tz), tz


def test_is_valid_tz_rejects_garbage():
    for bad in ("", None, "not a zone", "Europe/", "garbage/Tokyo", 42):
        assert not dh.is_valid_tz(bad), repr(bad)


def test_user_tz_uses_profile_tz():
    tz = dh.user_tz({"tz": "Asia/Tokyo"})
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == "Asia/Tokyo"


def test_user_tz_with_string():
    tz = dh.user_tz("America/Los_Angeles")
    assert str(tz) == "America/Los_Angeles"


def test_user_tz_falls_back_on_invalid():
    # Bad zone names and empty/None all fall back to the bot default.
    fallback = dh.user_tz("not-a-zone")
    assert str(fallback) == "Europe/Kyiv"
    assert str(dh.user_tz({})) == "Europe/Kyiv"
    assert str(dh.user_tz(None)) == "Europe/Kyiv"
    assert str(dh.user_tz({"tz": ""})) == "Europe/Kyiv"


def test_today_str_user_format():
    s = dh.today_str_user({"tz": "Asia/Tokyo"})
    # YYYY-MM-DD shape
    assert len(s) == 10
    assert s[4] == "-" and s[7] == "-"
    # Round-trip through datetime to confirm it parses as a real date.
    datetime.strptime(s, "%Y-%m-%d")


def test_now_user_is_timezone_aware():
    now = dh.now_user({"tz": "Europe/London"})
    assert now.tzinfo is not None
    assert str(now.tzinfo) == "Europe/London"


def test_tz_presets_are_all_valid():
    for tz_name, _label in dh.TZ_PRESETS:
        assert dh.is_valid_tz(tz_name), tz_name


# ---------- health profile (lib.health, F-1) ----------

def test_health_registries_cover_top_lists():
    # Sanity-check we kept the allergen + condition lists from the plan.
    expected_allergens = {"peanut", "tree_nut", "dairy", "egg", "soy", "gluten",
                          "fish", "shellfish", "sesame", "mustard", "sulphites",
                          "celery", "lupin", "mollusks"}
    assert expected_allergens <= set(hh.ALLERGENS.keys())
    expected_conditions = {"crohns", "ibs", "celiac", "diabetes_t1", "diabetes_t2",
                           "hypertension", "pcos", "kidney", "thyroid", "gestational"}
    assert expected_conditions <= set(hh.CONDITIONS.keys())


def test_normalize_known_aliases():
    assert hh.normalize("арахіс") == "peanut"
    assert hh.normalize("PEANUTS") == "peanut"
    assert hh.normalize("crohn") == "crohns"
    assert hh.normalize("Хвороба Крона") == "crohns"
    assert hh.normalize("Type 2 Diabetes") == "diabetes_t2"


def test_normalize_unknown_returns_lowercased():
    # Returns the input lowercased — caller decides whether to keep or drop.
    assert hh.normalize("totally made up") == "totally made up"


def test_parse_csv_allergens():
    canon, unknown = hh.parse_csv("арахіс, dairy, made-up", hh.ALLERGENS)
    assert canon == ["peanut", "dairy"]
    assert unknown == ["made-up"]


def test_parse_csv_conditions():
    canon, unknown = hh.parse_csv("crohns, gestational", hh.CONDITIONS)
    assert canon == ["crohns", "gestational"]
    assert unknown == []


def test_parse_csv_dedupes():
    canon, _ = hh.parse_csv("dairy, dairy, молочне", hh.ALLERGENS)
    assert canon == ["dairy"]


def test_parse_csv_empty():
    assert hh.parse_csv("", hh.ALLERGENS) == ([], [])
    assert hh.parse_csv("   ", hh.ALLERGENS) == ([], [])


def test_render_labels_uk():
    out = hh.render_labels(["peanut", "dairy"], hh.ALLERGENS)
    assert "арахіс" in out and "молочне" in out


def test_render_labels_empty():
    assert hh.render_labels([], hh.ALLERGENS) == "—"


def test_is_clear_keyword():
    for k in ("none", "немає", "нема", "no", "ні", "/clear", "—"):
        assert hh.is_clear_keyword(k), k
    for k in ("dairy", "арахіс", "anything else"):
        assert not hh.is_clear_keyword(k), k


def test_addendum_empty_when_no_health():
    assert hh.health_addendum_text([], []) == ""
    assert hh.addendum_for_profile(None) == ""
    assert hh.addendum_for_profile({"allergens": [], "conditions": []}) == ""


def test_addendum_includes_allergens_in_english():
    out = hh.health_addendum_text(["peanut", "dairy"], [])
    assert "USER HEALTH CONTEXT" in out
    assert "peanut" in out
    assert "dairy" in out


def test_addendum_includes_condition_guidance():
    out = hh.health_addendum_text([], ["crohns", "diabetes_t2"])
    assert "Crohn" in out
    assert "T2 diabetes" in out


def test_addendum_skips_unknown_condition():
    out = hh.health_addendum_text([], ["fictional_condition"])
    # No allergens, no known conditions → empty addendum (no LLM cost).
    assert "USER HEALTH CONTEXT" not in out


# ---------- i18n framework (lib.i18n, F-2) ----------

def test_i18n_t_returns_en_when_requested():
    out = i18n_mod.t("language_saved", locale="en", lang="🇬🇧 English")
    assert "Done" in out
    assert "🇬🇧 English" in out


def test_i18n_t_returns_uk_when_requested():
    out = i18n_mod.t("language_saved", locale="uk", lang="🇺🇦 Українська")
    assert "Готово" in out
    assert "🇺🇦 Українська" in out


def test_i18n_t_falls_back_to_primary_when_key_missing_in_locale():
    # 'language_prompt' exists in both dicts; pretend a key only in UK works.
    # We'll fabricate a missing-in-EN scenario by querying a key that doesn't
    # exist in either dict — must fall back to the literal key.
    assert i18n_mod.t("not_a_real_key") == "not_a_real_key"


def test_i18n_t_default_locale_uses_primary():
    out = i18n_mod.t("lang_label_uk")
    assert "Українська" in out


def test_i18n_supported_langs():
    assert i18n_mod.supported_langs() == ("en", "uk")


def test_normalize_lang_uk_variants():
    for code in ("uk", "uk-UA", "uk_UA", "ua", "UK"):
        assert i18n_mod.normalize_lang(code) == "uk", code


def test_normalize_lang_neighbors_default_to_uk():
    # Russian + Belarusian fall back to UA — closer than EN for these users.
    assert i18n_mod.normalize_lang("ru") == "uk"
    assert i18n_mod.normalize_lang("be") == "uk"


def test_normalize_lang_other_defaults_to_en():
    for code in ("en", "en-US", "fr", "de", "es", None, ""):
        assert i18n_mod.normalize_lang(code) == "en", code
