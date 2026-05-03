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
from lib import goals as gl
from lib import openai_vision as ov
from lib import personalization as pz
from lib import off as off_mod
from lib import mealplan as mp
from lib import recap as recap_mod


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
    assert "Денний ліміт" in rl.limit_reached_message("ask", locale="uk")
    assert "Daily limit" in rl.limit_reached_message("ask", locale="en")


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
    assert expected_allergens <= set(hh.ALLERGENS)
    expected_conditions = {"crohns", "ibs", "celiac", "diabetes_t1", "diabetes_t2",
                           "hypertension", "pcos", "kidney", "thyroid", "gestational"}
    assert expected_conditions <= set(hh.CONDITIONS)


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
    out = hh.render_labels(["peanut", "dairy"], "allergens", locale="uk")
    assert "арахіс" in out and "молочне" in out


def test_render_labels_en():
    out = hh.render_labels(["peanut", "dairy"], "allergens", locale="en")
    assert "peanut" in out and "dairy" in out


def test_render_labels_empty():
    assert hh.render_labels([], "allergens", locale="uk") == "—"


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


# ---------- F-2b Phase 1: plurals + date + lang_confirm ----------

from lib.i18n import plurals as _plurals
from lib.datehelpers import format_date_long, format_date_with_year
from lib.telegram_helpers import lang_confirm_keyboard
from datetime import datetime as _datetime


def test_plurals_uk_basic_three_forms():
    cases = {
        1:  "день",  2: "дні",  3: "дні",  4: "дні",
        5:  "днів", 11: "днів", 12: "днів", 13: "днів", 14: "днів",
        21: "день", 22: "дні", 25: "днів",
        101: "день", 111: "днів",
    }
    for n, expected in cases.items():
        assert _plurals.pluralize(n, "uk", "день", "дні", "днів") == expected, n


def test_plurals_en_two_forms():
    assert _plurals.pluralize(0, "en", "day", many="days") == "days"
    assert _plurals.pluralize(1, "en", "day", many="days") == "day"
    assert _plurals.pluralize(2, "en", "day", many="days") == "days"
    assert _plurals.pluralize(11, "en", "day", many="days") == "days"  # no Slavic exception
    assert _plurals.pluralize(21, "en", "day", many="days") == "days"


def test_plurals_uk_requires_few_and_many():
    import pytest
    with pytest.raises(ValueError):
        _plurals.pluralize(2, "uk", "день")  # missing few + many


def test_plurals_with_count_helper():
    assert _plurals.pluralize_with_count(1, "uk", "день", "дні", "днів") == "1 день"
    assert _plurals.pluralize_with_count(5, "uk", "день", "дні", "днів") == "5 днів"
    assert _plurals.pluralize_with_count(1, "en", "day", many="days") == "1 day"
    assert _plurals.pluralize_with_count(7, "en", "day", many="days") == "7 days"


def test_format_date_long_uk():
    dt = _datetime(2026, 4, 26)
    assert format_date_long(dt, "uk") == "26 квітня"
    # Cover edge months — Jan, Dec, plus the genitive case forms.
    assert format_date_long(_datetime(2026, 1, 1),  "uk") == "1 січня"
    assert format_date_long(_datetime(2026, 12, 31), "uk") == "31 грудня"


def test_format_date_long_en():
    assert format_date_long(_datetime(2026, 4, 26), "en") == "April 26"
    assert format_date_long(_datetime(2026, 1, 1),  "en") == "January 1"


def test_format_date_long_unknown_lang_falls_back_to_en():
    assert format_date_long(_datetime(2026, 4, 26), "fr") == "April 26"
    assert format_date_long(_datetime(2026, 4, 26), None) == "April 26"  # type: ignore[arg-type]


def test_format_date_with_year_appends_year():
    dt = _datetime(2026, 9, 15)
    assert format_date_with_year(dt, "uk") == "15 вересня 2026"
    assert format_date_with_year(dt, "en") == "September 15 2026"


def test_normalize_lang_ru_be_routes_to_uk():
    """Slavic codes (ru/be) still default to uk per 2026-04-26 decision —
    cross-Slavic readability is high and the override step zero is one tap."""
    assert i18n_mod.normalize_lang("ru") == "uk"
    assert i18n_mod.normalize_lang("be") == "uk"
    assert i18n_mod.normalize_lang("ru-RU") == "uk"


def test_lang_confirm_keyboard_primary_button_matches_detected():
    """Primary (top) button should be the auto-detected language so a
    one-tap continue 'just works'."""
    kb_uk = lang_confirm_keyboard("uk")
    assert kb_uk["inline_keyboard"][0][0]["callback_data"] == "onb:lang:uk"
    assert "укра" in kb_uk["inline_keyboard"][0][0]["text"].lower()
    # Override row offers the alternate language.
    assert kb_uk["inline_keyboard"][1][0]["callback_data"] == "onb:lang:en"

    kb_en = lang_confirm_keyboard("en")
    assert kb_en["inline_keyboard"][0][0]["callback_data"] == "onb:lang:en"
    assert "english" in kb_en["inline_keyboard"][0][0]["text"].lower()
    assert kb_en["inline_keyboard"][1][0]["callback_data"] == "onb:lang:uk"


def test_lang_confirm_prompt_renders_in_both_locales():
    """The lang-confirm prompt has its own UA + EN strings so users see
    the question in their detected language."""
    uk_prompt = i18n_mod.t("lang_confirm_prompt", locale="uk")
    en_prompt = i18n_mod.t("lang_confirm_prompt", locale="en")
    assert "KusWise" in uk_prompt
    assert "KusWise" in en_prompt
    # UA prompt mentions "українською", EN prompt mentions "English".
    assert "укра" in uk_prompt.lower()
    assert "english" in en_prompt.lower()


# ---------- F-2b Phase 2: onboarding strings ----------

def test_onboarding_intro_localizes():
    en = i18n_mod.t("onboarding.intro", locale="en")
    uk = i18n_mod.t("onboarding.intro", locale="uk")
    assert "KusWise" in en and "KusWise" in uk
    assert "questions" in en.lower()  # English-specific word
    assert "питань"   in uk.lower()    # Ukrainian-specific word


def test_onboarding_ask_age_localizes():
    en = i18n_mod.t("onboarding.ask_age", locale="en")
    uk = i18n_mod.t("onboarding.ask_age", locale="uk")
    assert en.startswith("1/6")
    assert uk.startswith("1/6")
    assert "old" in en.lower()
    assert "років" in uk.lower()


def test_onboarding_done_format_kwargs():
    """The done message takes a name kwarg — make sure it interpolates."""
    en = i18n_mod.t("onboarding.done", locale="en", name="Vic")
    uk = i18n_mod.t("onboarding.done", locale="uk", name="Віктор")
    assert "<b>Vic</b>"   in en
    assert "<b>Віктор</b>" in uk
    assert "/profile" in en and "/profile" in uk


def test_onboarding_default_name_is_locale_specific():
    """Fallback name when first_name is missing should match locale."""
    assert i18n_mod.t("onboarding.default_name", locale="en") == "friend"
    assert i18n_mod.t("onboarding.default_name", locale="uk") == "друже"


def test_locale_of_helper():
    assert i18n_mod.locale_of(None) == "en"
    assert i18n_mod.locale_of({}) == "en"
    assert i18n_mod.locale_of({"lang": "en"}) == "en"
    assert i18n_mod.locale_of({"lang": "uk"}) == "uk"
    # Unsupported lang stored on the row -> safe fallback to en
    assert i18n_mod.locale_of({"lang": "fr"}) == "en"
    # Non-dict input never crashes
    assert i18n_mod.locale_of("not a dict") == "en"


# ---------- engagement streaks (lib.database, F-4) ----------

class _StreakFakeCursor:
    """psycopg-like cursor backed by a single-row user_streaks dict."""
    def __init__(self, store):
        self.store = store           # {user_id: row_dict} or None
        self._last = None
        self._rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def rowcount(self):
        return self._rowcount

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()  # collapse whitespace for matching
        params = params or ()

        if s.startswith("SELECT") and "FROM USER_STREAKS" in s:
            uid = params[0]
            row = self.store.get(uid)
            if row is None:
                self._last = None
            else:
                self._last = (
                    row["current_streak"],
                    row["longest_streak"],
                    row["last_log_date"],
                    row["freeze_days_remaining"],
                    row["updated_at"],
                )
            return

        if s.startswith("INSERT INTO USER_STREAKS"):
            # INSERT (user_id, current_streak, longest_streak, last_log_date,
            #         freeze_days_remaining, updated_at)
            # VALUES (%s, 1, 1, %s, 3, %s)
            uid, last_log, updated_at = params
            self.store[uid] = {
                "current_streak": 1,
                "longest_streak": 1,
                "last_log_date": last_log,
                "freeze_days_remaining": 3,
                "updated_at": updated_at,
            }
            self._rowcount = 1
            return

        if s.startswith("UPDATE USER_STREAKS"):
            if "WHERE USER_ID" in s:
                # Per-user UPDATE from update_streak_for_meal: params order is
                # (current, longest, last_log, freezes, updated_at, user_id).
                cur, lng, last_log, freezes, updated_at, uid = params
                row = self.store.get(uid)
                if row is None:
                    self._rowcount = 0
                    return
                row.update({
                    "current_streak": cur,
                    "longest_streak": lng,
                    "last_log_date": last_log,
                    "freeze_days_remaining": freezes,
                    "updated_at": updated_at,
                })
                self._rowcount = 1
                return
            # Bulk UPDATE from reset_monthly_freezes: SET freezes = 3 for all rows.
            (updated_at,) = params
            n = 0
            for row in self.store.values():
                row["freeze_days_remaining"] = 3
                row["updated_at"] = updated_at
                n += 1
            self._rowcount = n
            return

        # Other SQL is unexpected here — fail loudly to surface drift.
        raise AssertionError(f"unexpected SQL in streak fake: {sql!r}")

    def fetchone(self):
        return self._last


class _StreakFakeConn:
    def __init__(self):
        self.store = {}

    def cursor(self):
        return _StreakFakeCursor(self.store)

    def commit(self):
        pass


def test_streak_first_meal_creates_row():
    conn = _StreakFakeConn()
    row = db.update_streak_for_meal(conn, user_id=1, today_local="2026-04-25")
    assert row["current_streak"] == 1
    assert row["longest_streak"] == 1
    assert row["last_log_date"] == "2026-04-25"
    assert row["freeze_days_remaining"] == 3


def test_streak_same_day_meal_is_noop():
    conn = _StreakFakeConn()
    db.update_streak_for_meal(conn, 1, "2026-04-25")
    row2 = db.update_streak_for_meal(conn, 1, "2026-04-25")
    assert row2["current_streak"] == 1
    assert row2["freeze_days_remaining"] == 3
    assert row2["last_log_date"] == "2026-04-25"


def test_streak_next_day_increments():
    conn = _StreakFakeConn()
    db.update_streak_for_meal(conn, 1, "2026-04-25")  # Mon
    row = db.update_streak_for_meal(conn, 1, "2026-04-26")  # Tue
    assert row["current_streak"] == 2
    assert row["longest_streak"] == 2
    assert row["freeze_days_remaining"] == 3
    assert row["last_log_date"] == "2026-04-26"


def test_streak_skip_one_day_consumes_freeze():
    conn = _StreakFakeConn()
    db.update_streak_for_meal(conn, 1, "2026-04-25")  # Mon
    row = db.update_streak_for_meal(conn, 1, "2026-04-27")  # Wed (skipped Tue)
    # Streak unchanged, but one freeze consumed.
    assert row["current_streak"] == 1
    assert row["freeze_days_remaining"] == 2
    assert row["last_log_date"] == "2026-04-27"


def test_streak_skip_two_days_resets():
    conn = _StreakFakeConn()
    db.update_streak_for_meal(conn, 1, "2026-04-25")  # Mon
    row = db.update_streak_for_meal(conn, 1, "2026-04-28")  # Thu (skipped Tue+Wed)
    assert row["current_streak"] == 1
    assert row["last_log_date"] == "2026-04-28"


def test_streak_monthly_reset_restores_to_3():
    conn = _StreakFakeConn()
    # Seed a row with depleted freezes.
    db.update_streak_for_meal(conn, 1, "2026-04-25")
    conn.store[1]["freeze_days_remaining"] = 0
    n = db.reset_monthly_freezes(conn)
    assert n == 1
    assert conn.store[1]["freeze_days_remaining"] == 3


# ---------- goals projection (lib.goals, F-5) ----------

from datetime import date as _date

def test_projection_basic_lose():
    # Plan verification: 78 → 70 kg at -0.5/wk = 16 weeks.
    p = gl.compute_projection(78.0, 70.0, -0.5, today=_date(2026, 4, 25))
    assert p.reason == "ok"
    assert p.weeks_to_goal == 16.0
    # 16 weeks = 112 days from 2026-04-25 → 2026-08-15.
    assert p.projected_date == _date(2026, 8, 15)


def test_projection_doubled_delta_halves_weeks():
    # Plan verification: edit to -1 kg/week → 8 weeks.
    p = gl.compute_projection(78.0, 70.0, -1.0, today=_date(2026, 4, 25))
    assert p.weeks_to_goal == 8.0


def test_projection_zero_delta_returns_unknown():
    p = gl.compute_projection(78.0, 70.0, 0.0)
    assert p.reason == "zero_delta"
    assert p.weeks_to_goal is None
    assert p.projected_date is None


def test_projection_wrong_direction_flagged():
    # Wants to lose (target < current) but delta is positive.
    p = gl.compute_projection(78.0, 70.0, +0.5)
    assert p.reason == "wrong_direction"
    assert p.weeks_to_goal is None


def test_projection_at_target_returns_zero_weeks():
    p = gl.compute_projection(70.05, 70.0, -0.5, today=_date(2026, 4, 25))
    assert p.reason == "at_target"
    assert p.weeks_to_goal == 0.0


def test_projection_no_target_or_no_current():
    a = gl.compute_projection(78.0, None, -0.5)
    b = gl.compute_projection(None, 70.0, -0.5)
    assert a.reason == "no_target"
    assert b.reason == "no_current"


def test_default_weekly_delta_per_goal():
    assert gl.default_weekly_delta("lose")     == -0.5
    assert gl.default_weekly_delta("gain")     == +0.3
    assert gl.default_weekly_delta("maintain") == 0.0
    assert gl.default_weekly_delta(None)       == 0.0


def test_effective_weekly_delta_falls_back_to_default():
    # NULL override → goal-based default
    p = {"weekly_delta_kg": None, "goal": "lose"}
    assert gl.effective_weekly_delta(p) == -0.5
    # Explicit override wins
    p = {"weekly_delta_kg": -0.7, "goal": "lose"}
    assert gl.effective_weekly_delta(p) == -0.7


def test_effective_weekly_delta_clamps_extreme_values():
    p = {"weekly_delta_kg": -5.0, "goal": "lose"}
    assert gl.effective_weekly_delta(p) == -2.0  # clamped to -_MAX_WEEKLY_DELTA
    p = {"weekly_delta_kg": +5.0, "goal": "gain"}
    assert gl.effective_weekly_delta(p) == +2.0


def test_classify_on_track():
    # Target -0.5/wk, actual -0.45 → on_track (within 0.25 tolerance)
    assert gl.classify_actual_vs_target(-0.45, -0.5) == "on_track"


def test_classify_ahead_when_losing_faster():
    # Target -0.5, actual -0.9 → ahead (faster loss)
    assert gl.classify_actual_vs_target(-0.9, -0.5) == "ahead"


def test_classify_behind_when_losing_slower():
    # Target -0.5, actual -0.1 → behind (slower than planned)
    assert gl.classify_actual_vs_target(-0.1, -0.5) == "behind"


def test_classify_maintain_drift():
    # Target 0, actual +0.5 → behind (drifting away from steady)
    assert gl.classify_actual_vs_target(+0.5, 0.0) == "behind"
    assert gl.classify_actual_vs_target(+0.1, 0.0) == "on_track"


def test_actual_weekly_delta_basic():
    # 4 weeks back: 80kg → today: 78kg → 2 kg lost over 28 days = -0.5/wk
    history = [
        {"weight_kg": 78.0, "recorded_at": _date(2026, 4, 25)},
        {"weight_kg": 80.0, "recorded_at": _date(2026, 3, 28)},
    ]
    actual = gl.actual_weekly_delta(history, window_weeks=4)
    assert actual is not None
    assert abs(actual - (-0.5)) < 0.01


def test_actual_weekly_delta_too_few_points():
    assert gl.actual_weekly_delta([], window_weeks=4) is None
    assert gl.actual_weekly_delta(
        [{"weight_kg": 80.0, "recorded_at": _date(2026, 4, 25)}], window_weeks=4
    ) is None


def test_projection_for_profile_pulls_from_dict():
    profile = {"weight_kg": 78.0, "target_weight_kg": 70.0,
               "weekly_delta_kg": -0.5, "goal": "lose"}
    p = gl.projection_for_profile(profile, today=_date(2026, 4, 25))
    assert p.reason == "ok"
    assert p.weeks_to_goal == 16.0


# ---------- confidence + alternates UI (lib.openai_vision, F-6) ----------

def test_normalize_candidates_returns_empty_when_missing():
    assert ov.normalize_candidates({}) == []
    assert ov.normalize_candidates({"top_guesses": None}) == []
    assert ov.normalize_candidates({"top_guesses": []}) == []


def test_normalize_candidates_drops_invalid_rows():
    raw = {"top_guesses": [
        {"name": "Каша", "calories": 200, "protein_g": 8, "carbs_g": 35, "fat_g": 4, "confidence": 0.6},
        {"name": "",     "calories": 300, "confidence": 0.3},                   # blank name
        "garbage",                                                              # not a dict
        {"name": "Pasta", "calories": "oops", "confidence": 0.1},               # bad number
    ]}
    out = ov.normalize_candidates(raw)
    assert len(out) == 1
    assert out[0]["name"] == "Каша"


def test_normalize_candidates_caps_at_three():
    raw = {"top_guesses": [
        {"name": f"opt{i}", "calories": 100, "confidence": 0.1 * (4 - i)}
        for i in range(5)
    ]}
    out = ov.normalize_candidates(raw)
    assert len(out) == 3


def test_normalize_candidates_sorts_by_confidence_desc():
    raw = {"top_guesses": [
        {"name": "low",  "calories": 100, "confidence": 0.1},
        {"name": "high", "calories": 200, "confidence": 0.7},
        {"name": "mid",  "calories": 150, "confidence": 0.4},
    ]}
    out = ov.normalize_candidates(raw)
    assert [c["name"] for c in out] == ["high", "mid", "low"]


def test_is_ambiguous_below_threshold():
    cands = [
        {"name": "A", "confidence": 0.55},
        {"name": "B", "confidence": 0.30},
    ]
    assert ov.is_ambiguous(cands) is True


def test_is_ambiguous_above_threshold_is_confident():
    cands = [
        {"name": "A", "confidence": 0.92},
        {"name": "B", "confidence": 0.05},
    ]
    assert ov.is_ambiguous(cands) is False


def test_is_ambiguous_single_candidate_never_ambiguous():
    assert ov.is_ambiguous([{"name": "A", "confidence": 0.4}]) is False
    assert ov.is_ambiguous([]) is False


def test_candidate_to_analysis_inherits_from_base():
    base = {
        "estimated_portion": "~300г",
        "portion_reasoning": "Тарілка 27см",
        "ingredients": [{"name": "курка", "estimated_grams": 150}],
        "nutrition": {"fiber_g": 4.0, "sugar_g": 2.0},
    }
    cand = {"name": "Курка з рисом", "calories": 520, "protein_g": 35,
            "carbs_g": 50, "fat_g": 12, "confidence": 0.6}
    out = ov.candidate_to_analysis(cand, base=base)
    assert out["dish_name"] == "Курка з рисом"
    assert out["nutrition"]["calories"] == 520
    assert out["nutrition"]["fiber_g"] == 4.0  # inherited
    assert out["nutrition"]["sugar_g"] == 2.0  # inherited
    assert out["estimated_portion"] == "~300г"  # inherited
    assert out["ingredients"]                    # inherited


def test_candidate_to_analysis_handles_missing_base():
    out = ov.candidate_to_analysis({"name": "Pizza", "calories": 800})
    assert out["dish_name"] == "Pizza"
    assert out["nutrition"]["calories"] == 800
    # Missing base → reasonable empty defaults, not crashes.
    assert out["ingredients"] == []
    assert out["nutrition"]["fiber_g"] == 0


# ---------- personalization (lib.personalization, F-7) ----------

class _PersonalizationFakeCursor:
    """Tiny psycopg-like cursor for corrections + user_food_aliases."""
    def __init__(self, store):
        self.store = store
        self._last = None
        self._rows = None
        self._rowcount = 0

    def __enter__(self):  return self
    def __exit__(self, *exc): return False

    @property
    def rowcount(self): return self._rowcount

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        params = params or ()

        if s.startswith("INSERT INTO CORRECTIONS"):
            uid, source, orig_json, corr_json, ts = params
            self.store["corrections"].append({
                "id":       len(self.store["corrections"]) + 1,
                "user_id":  uid,
                "source":   source,
                "orig_json": orig_json,
                "corr_json": corr_json,
                "created_at": ts,
            })
            self._rowcount = 1
            return

        if s.startswith("SELECT") and "FROM CORRECTIONS" in s:
            uid, lim = params
            rows = [
                (r["id"], r["source"], r["orig_json"], r["corr_json"], r["created_at"])
                for r in self.store["corrections"] if r["user_id"] == uid
            ]
            rows.sort(key=lambda r: r[4], reverse=True)
            self._rows = rows[:lim]
            return

        if s.startswith("INSERT INTO USER_FOOD_ALIASES"):
            (uid, alias, normalized,
             grams, kcal, protein_g, fat_g, carbs_g,
             ts,
             a1, a2, a3, a4, a5, a6, a7, a8, a9, a10) = params
            key = (uid, alias)
            existing = self.store["aliases"].get(key)
            if existing is None:
                self.store["aliases"][key] = {
                    "alias": alias, "normalized_name": normalized,
                    "default_grams": grams, "default_kcal": kcal,
                    "default_protein_g": protein_g,
                    "default_fat_g": fat_g, "default_carbs_g": carbs_g,
                    "sample_count": 1, "updated_at": ts,
                }
            else:
                a = a1  # all alphas are equal in our call
                blend = lambda old, new: (old or 0) * (1 - a) + new * a
                existing["normalized_name"]   = normalized
                existing["default_grams"]     = blend(existing["default_grams"], grams)
                existing["default_kcal"]      = blend(existing["default_kcal"],  kcal)
                existing["default_protein_g"] = blend(existing["default_protein_g"], protein_g)
                existing["default_fat_g"]     = blend(existing["default_fat_g"],     fat_g)
                existing["default_carbs_g"]   = blend(existing["default_carbs_g"],   carbs_g)
                existing["sample_count"]     += 1
                existing["updated_at"]        = ts
            self._rowcount = 1
            return

        if s.startswith("SELECT") and "FROM USER_FOOD_ALIASES" in s:
            uid, lim = params
            rows = [
                (a["alias"], a["normalized_name"],
                 a["default_grams"], a["default_kcal"],
                 a["default_protein_g"], a["default_fat_g"], a["default_carbs_g"],
                 a["sample_count"], a["updated_at"])
                for (u, _key), a in self.store["aliases"].items() if u == uid  # not used
            ]
            # The above iteration is wrong shape — fix:
            rows = []
            for (u, _alias_key), a in self.store["aliases"].items():
                if u == uid:
                    rows.append((
                        a["alias"], a["normalized_name"],
                        a["default_grams"], a["default_kcal"],
                        a["default_protein_g"], a["default_fat_g"], a["default_carbs_g"],
                        a["sample_count"], a["updated_at"],
                    ))
            rows.sort(key=lambda r: r[8] or "", reverse=True)
            self._rows = rows[:lim]
            return

        if s.startswith("DELETE FROM USER_FOOD_ALIASES"):
            uid, alias = params
            key = (uid, alias)
            if key in self.store["aliases"]:
                del self.store["aliases"][key]
                self._rowcount = 1
            else:
                self._rowcount = 0
            return

        raise AssertionError(f"unexpected SQL in personalization fake: {sql!r}")

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._rows or []


class _PersonalizationFakeConn:
    def __init__(self):
        self.store = {"corrections": [], "aliases": {}}

    def cursor(self):
        return _PersonalizationFakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_record_correction_writes_row():
    conn = _PersonalizationFakeConn()
    pz.record_correction(conn, 1, "manual",
                         original={"dish_name": "Pizza"},
                         corrected={"dish_name": "Margherita"})
    assert len(conn.store["corrections"]) == 1
    row = conn.store["corrections"][0]
    assert row["user_id"] == 1
    assert row["source"] == "manual"
    assert "Pizza" in row["orig_json"]
    assert "Margherita" in row["corr_json"]


def test_record_correction_swallows_errors():
    """Audit trail must never bubble exceptions to the caller."""
    class _BoomConn:
        def cursor(self): raise RuntimeError("db down")
        def commit(self): pass
        def rollback(self): pass
    # Should not raise.
    pz.record_correction(_BoomConn(), 1, "manual", {}, {})


def test_upsert_alias_first_sample_creates_row():
    conn = _PersonalizationFakeConn()
    analysis = {
        "dish_name": "Куряча грудка",
        "nutrition": {"calories": 330, "protein_g": 50, "carbs_g": 0, "fat_g": 8},
        "ingredients": [{"name": "куряча грудка", "estimated_grams": 200}],
    }
    pz.upsert_alias_from_meal(conn, 1, analysis)
    assert len(conn.store["aliases"]) == 1
    a = conn.store["aliases"][(1, "куряча грудка")]
    assert a["sample_count"] == 1
    assert a["default_grams"] == 200
    assert a["default_kcal"] == 330


def test_upsert_alias_ewma_blends_on_subsequent_samples():
    conn = _PersonalizationFakeConn()
    base = {
        "nutrition": {"calories": 330, "protein_g": 50, "carbs_g": 0, "fat_g": 8},
        "ingredients": [{"name": "x", "estimated_grams": 200}],
    }
    pz.upsert_alias_from_meal(conn, 1, {**base, "dish_name": "Курка"})
    # Second sample: 250g, 400 kcal — alpha=0.3 so new value = 200*0.7 + 250*0.3 = 215
    pz.upsert_alias_from_meal(conn, 1, {
        "dish_name": "Курка",
        "nutrition": {"calories": 400, "protein_g": 60, "carbs_g": 0, "fat_g": 10},
        "ingredients": [{"name": "x", "estimated_grams": 250}],
    })
    a = conn.store["aliases"][(1, "курка")]
    assert a["sample_count"] == 2
    assert abs(a["default_grams"] - (200 * 0.7 + 250 * 0.3)) < 0.01
    assert abs(a["default_kcal"]  - (330 * 0.7 + 400 * 0.3)) < 0.01


def test_upsert_alias_skips_zero_kcal():
    conn = _PersonalizationFakeConn()
    pz.upsert_alias_from_meal(conn, 1, {
        "dish_name": "Ничого",
        "nutrition": {"calories": 0},
    })
    assert conn.store["aliases"] == {}


def test_upsert_alias_skips_blank_dish_name():
    conn = _PersonalizationFakeConn()
    pz.upsert_alias_from_meal(conn, 1, {
        "dish_name": "",
        "nutrition": {"calories": 200},
    })
    assert conn.store["aliases"] == {}


def test_upsert_alias_extracts_grams_from_estimated_portion_text():
    """When ingredient grams aren't present, fall back to portion text regex."""
    conn = _PersonalizationFakeConn()
    pz.upsert_alias_from_meal(conn, 1, {
        "dish_name": "Каша",
        "estimated_portion": "приблизно 250г",
        "nutrition": {"calories": 320},
    })
    a = conn.store["aliases"][(1, "каша")]
    assert a["default_grams"] == 250


def test_aliases_prompt_block_empty_for_new_user():
    conn = _PersonalizationFakeConn()
    assert pz.aliases_prompt_block(conn, 1) == ""


def test_aliases_prompt_block_skips_single_sample_aliases():
    conn = _PersonalizationFakeConn()
    pz.upsert_alias_from_meal(conn, 1, {
        "dish_name": "Курка",
        "nutrition": {"calories": 330},
        "ingredients": [{"name": "x", "estimated_grams": 200}],
    })
    # Single sample → not yet in prompt block.
    assert pz.aliases_prompt_block(conn, 1) == ""


def test_aliases_prompt_block_includes_repeats():
    conn = _PersonalizationFakeConn()
    base = {"nutrition": {"calories": 330},
            "ingredients": [{"name": "x", "estimated_grams": 200}]}
    pz.upsert_alias_from_meal(conn, 1, {**base, "dish_name": "Курка"})
    pz.upsert_alias_from_meal(conn, 1, {**base, "dish_name": "Курка"})  # 2nd sample
    block = pz.aliases_prompt_block(conn, 1)
    assert "Курка" in block
    assert "USER PERSONALIZATION" in block


def test_recent_aliases_returns_newest_first():
    conn = _PersonalizationFakeConn()
    base = {"nutrition": {"calories": 200}, "ingredients": []}
    # Insert two aliases with different ordering — the second one should appear first.
    import time as _t
    pz.upsert_alias_from_meal(conn, 1, {**base, "dish_name": "Перший"})
    _t.sleep(0.001)  # ensure distinct timestamps
    pz.upsert_alias_from_meal(conn, 1, {**base, "dish_name": "Другий"})
    rows = pz.recent_aliases(conn, 1)
    assert rows[0]["normalized_name"] == "Другий"
    assert rows[1]["normalized_name"] == "Перший"


def test_delete_alias_removes_row():
    conn = _PersonalizationFakeConn()
    pz.upsert_alias_from_meal(conn, 1, {
        "dish_name": "Курка",
        "nutrition": {"calories": 330},
        "ingredients": [{"name": "x", "estimated_grams": 200}],
    })
    assert pz.delete_alias(conn, 1, "Курка") is True
    assert conn.store["aliases"] == {}
    # Idempotent — second delete reports False.
    assert pz.delete_alias(conn, 1, "Курка") is False


# ---------- Open Food Facts (lib.off, F-8) ----------

def test_looks_like_ean_accepts_8_to_13_digits():
    assert off_mod.looks_like_ean("12345678") is True       # EAN-8
    assert off_mod.looks_like_ean("123456789012") is True    # UPC-A
    assert off_mod.looks_like_ean("5449000000996") is True   # EAN-13
    assert off_mod.looks_like_ean(None) is False
    assert off_mod.looks_like_ean("") is False
    assert off_mod.looks_like_ean("12345") is False          # too short
    assert off_mod.looks_like_ean("12345678901234") is False # too long
    assert off_mod.looks_like_ean("ABC1234567") is False     # has letters
    assert off_mod.looks_like_ean("123 456 789 012") is False  # spaces


def test_normalize_kcal_from_kj_when_kcal_missing():
    """OFF sometimes reports only kJ; we should convert at 4.184."""
    raw = {
        "product_name": "Olive oil",
        "nutriments": {"energy-kj_100g": 3700},  # ≈ 884 kcal
    }
    out = off_mod._normalize(raw, "1234567890123")
    assert out is not None
    assert 880 <= out["per_100g"]["calories"] <= 890


def test_normalize_drops_product_with_no_calorie_data():
    raw = {"product_name": "Air", "nutriments": {}}
    assert off_mod._normalize(raw, "1234567890123") is None


def test_normalize_drops_product_with_blank_name():
    raw = {
        "product_name": "",
        "nutriments": {"energy-kcal_100g": 100},
    }
    assert off_mod._normalize(raw, "1234567890123") is None


def test_normalize_falls_back_to_localized_names():
    raw = {
        "product_name": "",
        "product_name_en": "Yogurt",
        "nutriments": {"energy-kcal_100g": 60, "proteins_100g": 4, "fat_100g": 3, "carbohydrates_100g": 4.5},
    }
    out = off_mod._normalize(raw, "1234567890123")
    assert out is not None
    assert out["name"] == "Yogurt"


def test_parse_serving_size_grams_extracts_first_g_value():
    assert off_mod._parse_serving_size_grams("30 g") == 30
    assert off_mod._parse_serving_size_grams("125g") == 125
    assert off_mod._parse_serving_size_grams("30g (1 bar)") == 30
    assert off_mod._parse_serving_size_grams("1 cup (250ml)") is None
    assert off_mod._parse_serving_size_grams("") is None


def test_macros_for_grams_scales_linearly():
    per_100g = {"calories": 100, "protein_g": 10, "carbs_g": 20, "fat_g": 5,
                "fiber_g": 2, "sugar_g": 8}
    out = off_mod.macros_for_grams(per_100g, 250)
    assert out["calories"] == 250
    assert out["protein_g"] == 25
    assert out["fat_g"] == 12.5


def test_normalize_menu_dishes_drops_invalid():
    raw = [
        {"name": "Цезар",   "calories": 520, "protein_g": 35, "carbs_g": 30, "fat_g": 25, "confidence": 0.7},
        {"name": "",        "calories": 300, "confidence": 0.5},      # blank name
        {"name": "Зразок",  "calories": 0,   "confidence": 0.4},      # zero kcal
        "garbage",                                                     # not a dict
        {"name": "Pasta",   "calories": "oops"},                       # bad number
    ]
    out = ov.normalize_menu_dishes(raw)
    assert len(out) == 1
    assert out[0]["name"] == "Цезар"


def test_normalize_menu_dishes_caps_at_25():
    raw = [{"name": f"opt{i}", "calories": 100, "confidence": 0.5} for i in range(50)]
    out = ov.normalize_menu_dishes(raw)
    assert len(out) == 25


def test_normalize_menu_dishes_sorts_by_confidence():
    raw = [
        {"name": "low",  "calories": 100, "confidence": 0.2},
        {"name": "high", "calories": 200, "confidence": 0.9},
        {"name": "mid",  "calories": 150, "confidence": 0.5},
    ]
    out = ov.normalize_menu_dishes(raw)
    assert [d["name"] for d in out] == ["high", "mid", "low"]


def test_parse_menu_response_handles_fenced_json():
    raw = '```json\n{"dishes": [{"name": "Стейк", "calories": 600, "confidence": 0.8}]}\n```'
    out = ov._parse_menu_response(raw)
    assert out is not None
    assert out[0]["name"] == "Стейк"


def test_parse_menu_response_returns_none_on_garbage():
    assert ov._parse_menu_response("not json at all") is None
    assert ov._parse_menu_response("{}") == []  # valid JSON but no dishes
    assert ov._parse_menu_response('{"dishes": "not a list"}') is None


def test_product_to_analysis_yields_save_meal_shape():
    product = {
        "ean": "5449000000996",
        "name": "Coca-Cola Original 330 ml",
        "brand": "Coca-Cola",
        "per_100g": {"calories": 42, "protein_g": 0, "carbs_g": 10.6,
                     "fat_g": 0, "fiber_g": 0, "sugar_g": 10.6},
        "serving_size_g": 330,
    }
    a = off_mod.product_to_analysis(product, 330, locale="uk")
    # Brand prepended once, name not duplicated.
    assert "Coca-Cola" in a["dish_name"]
    assert a["nutrition"]["calories"] == 138.6  # 42 * 3.3
    assert a["estimated_portion"] == "330г"
    assert a["_source"]["kind"] == "barcode"
    assert a["_source"]["ean"] == "5449000000996"
    # EN locale flips both unit and source label.
    en = off_mod.product_to_analysis(product, 330, locale="en")
    assert en["estimated_portion"] == "330g"
    assert "Barcode scanner" in en["portion_reasoning"]


# ---------- F-8 manual-EAN entry validation ----------

def test_manual_ean_strips_spaces_and_hyphens():
    """The manual entry handler accepts user-friendly formatting."""
    # The webhook handler does cleaned = text.strip().replace(" ", "").replace("-", "")
    # then looks_like_ean. Ensure looks_like_ean accepts the cleaned form.
    cleaned = "5449 000 000-996".replace(" ", "").replace("-", "")
    assert cleaned == "5449000000996"
    assert off_mod.looks_like_ean(cleaned)


def test_manual_ean_rejects_non_digits():
    # User typed "abc1234567890" or similar — should fail validation.
    cleaned = "abc1234567890".replace(" ", "").replace("-", "")
    assert not off_mod.looks_like_ean(cleaned)


def test_manual_ean_rejects_short():
    cleaned = "1234567"  # 7 digits — UPC/EAN minimum is 8
    assert not off_mod.looks_like_ean(cleaned)


def test_manual_ean_rejects_long():
    cleaned = "12345678901234"  # 14 digits — > EAN-13 max
    assert not off_mod.looks_like_ean(cleaned)


# ---------- F-10 meal plan parsing (lib.mealplan) ----------

def test_normalize_plan_pads_short_days():
    raw = {"days": [{"date_label": "Today", "slots": {
        "breakfast": {"name": "Овсянка", "calories": 300, "protein_g": 12, "carbs_g": 50, "fat_g": 7},
    }}]}
    out = mp.normalize_plan(raw)
    assert len(out["days"]) == 3
    # F-2b Chunk 6: default labels are EN tokens; the formatter translates
    # them client-side based on user locale.
    assert out["days"][1]["date_label"] == "Tomorrow"
    assert out["days"][2]["date_label"] == "Day 3"


def test_normalize_plan_drops_blank_slot_names():
    raw = {"days": [{"slots": {
        "breakfast": {"name": "", "calories": 100},
        "lunch":     {"name": "Salad", "calories": 350, "protein_g": 25, "carbs_g": 20, "fat_g": 18},
    }}]}
    out = mp.normalize_plan(raw)
    assert out["days"][0]["slots"]["breakfast"] is None
    assert out["days"][0]["slots"]["lunch"]["name"] == "Salad"


def test_normalize_plan_handles_garbage_input():
    # Non-dict input → returns the same 3-day skeleton (all-None slots).
    out = mp.normalize_plan(None)  # type: ignore[arg-type]
    assert len(out["days"]) == 3
    for day in out["days"]:
        assert all(slot is None for slot in day["slots"].values())


def test_normalize_plan_coerces_macros_to_ints():
    raw = {"days": [{"slots": {
        "breakfast": {"name": "Test", "calories": "300.7", "protein_g": "12.3",
                       "carbs_g": "50.5", "fat_g": "7.9"},
    }}]}
    out = mp.normalize_plan(raw)
    slot = out["days"][0]["slots"]["breakfast"]
    assert slot["calories"] == 301      # rounded
    assert slot["protein_g"] == 12      # rounded
    assert slot["fat_g"] == 8


def test_slot_to_analysis_yields_save_meal_shape():
    slot = {"name": "Курка з рисом", "calories": 540, "protein_g": 50,
            "carbs_g": 60, "fat_g": 12, "recipe": "Запекти"}
    a = mp.slot_to_analysis(slot)
    assert a["dish_name"] == "Курка з рисом"
    assert a["nutrition"]["calories"] == 540
    assert a["_source"]["kind"] == "meal_plan"
    assert a["nutrition"]["fiber_g"] == 0


# ---------- F-12 weekly recap stats (lib.recap) ----------

def test_recap_avg_kcal_basic():
    end = _date(2026, 4, 25)
    meals = [
        {"date": "2026-04-25", "description": "Курка",  "calories": 600},
        {"date": "2026-04-25", "description": "Салат",  "calories": 200},
        {"date": "2026-04-24", "description": "Курка",  "calories": 800},
        {"date": "2026-04-23", "description": "Риба",   "calories": 700},
    ]
    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=meals,
        weight_history_recent=[],
        streak_row={"current_streak": 5},
        end_date=end,
    )
    assert stats["days_logged"] == 3
    # 3 days totaling 800 + 800 + 700 = 2300 → 2300/3 ≈ 767
    assert stats["avg_kcal"] == 767
    assert stats["streak"] == 5


def test_recap_macro_distribution_pct_sums_to_100():
    """Each macro rounds independently; we patch fat_pct to make the
    three values sum to 100 exactly so the share card never reads 99% / 101%."""
    end = _date(2026, 4, 25)
    # 100g protein (400 kcal) + 100g carbs (400 kcal) + 50g fat (450 kcal)
    # → total 1250 kcal → 32% / 32% / 36%
    meals = [
        {"date": "2026-04-25", "description": "x", "calories": 1250,
         "protein_g": 100, "carbs_g": 100, "fat_g": 50},
    ]
    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=meals,
        weight_history_recent=[],
        streak_row=None,
        end_date=end,
    )
    assert stats["protein_pct"] == 32
    assert stats["carbs_pct"] == 32
    assert stats["fat_pct"] == 36
    assert stats["protein_pct"] + stats["carbs_pct"] + stats["fat_pct"] == 100


def test_recap_macro_pct_none_when_no_macros():
    """No macros logged → pct fields are None, not zero (avoids 0/0/0 spam)."""
    end = _date(2026, 4, 25)
    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=[], weight_history_recent=[],
        streak_row=None, end_date=end,
    )
    assert stats["protein_pct"] is None
    assert stats["carbs_pct"] is None
    assert stats["fat_pct"] is None


def test_recap_weight_delta_basic():
    end = _date(2026, 4, 25)
    meals = [{"date": "2026-04-25", "description": "x", "calories": 100}]
    weights = [
        {"weight_kg": 78.0, "recorded_at": _date(2026, 4, 19)},
        {"weight_kg": 77.4, "recorded_at": _date(2026, 4, 25)},
    ]
    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=meals,
        weight_history_recent=weights,
        streak_row=None,
        end_date=end,
    )
    assert stats["weight_delta"] == -0.6


def test_recap_handles_empty_inputs():
    """Cron calls this for every user — must never crash on empty data."""
    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=[],
        weight_history_recent=[],
        streak_row=None,
        end_date=_date(2026, 4, 25),
    )
    assert stats["days_logged"] == 0
    assert stats["avg_kcal"] == 0
    assert stats["streak"] == 0
    assert stats["weight_delta"] is None
    assert stats["protein_pct"] is None


def test_recap_window_excludes_old_meals():
    end = _date(2026, 4, 25)
    meals = [
        {"date": "2026-04-25", "description": "in window",  "calories": 500},
        {"date": "2026-04-10", "description": "out window", "calories": 999},
    ]
    stats = recap_mod.compute_weekly_stats(
        meals_last_7d=meals,
        weight_history_recent=[],
        streak_row=None,
        end_date=end,
    )
    assert stats["days_logged"] == 1  # only the in-window day
    assert stats["avg_kcal"] == 500


# ---------- per-ingredient calories rendering ----------

from lib import formatters as fm


def test_format_ingredients_uk_shows_grams_and_kcal_when_both_present():
    out = fm._format_ingredients({
        "ingredients": [
            {"name": "копчений сулугуні", "estimated_grams": 70,  "estimated_calories": 280},
            {"name": "сушена свинина",     "estimated_grams": 50,  "estimated_calories": 250},
            {"name": "пиво",               "estimated_grams": 1000, "estimated_calories": 350},
        ],
    }, locale="uk")
    body = "\n".join(out)
    assert "копчений сулугуні — ~70г · ~280 ккал" in body
    assert "сушена свинина — ~50г · ~250 ккал" in body
    assert "пиво — ~1000г · ~350 ккал" in body


def test_format_ingredients_en_shows_g_and_kcal():
    """EN counterpart: 'g' + 'kcal' instead of 'г' + 'ккал'."""
    out = fm._format_ingredients({
        "ingredients": [
            {"name": "smoked sulguni", "estimated_grams": 70,  "estimated_calories": 280},
            {"name": "dried pork",      "estimated_grams": 50,  "estimated_calories": 250},
        ],
    }, locale="en")
    body = "\n".join(out)
    assert "smoked sulguni — ~70g · ~280 kcal" in body
    assert "dried pork — ~50g · ~250 kcal" in body
    # Ingredients header should also be English
    assert "Ingredients:" in body


def test_format_ingredients_uk_falls_back_when_no_kcal():
    """Older saved meals have only estimated_grams — must still render."""
    out = fm._format_ingredients({
        "ingredients": [
            {"name": "курка", "estimated_grams": 200},
        ],
    }, locale="uk")
    body = "\n".join(out)
    assert "курка — ~200г" in body
    assert "ккал" not in body


def test_format_ingredients_uk_handles_only_kcal():
    """Edge case: model returns kcal but no grams (rare)."""
    out = fm._format_ingredients({
        "ingredients": [
            {"name": "сир", "estimated_calories": 120},
        ],
    }, locale="uk")
    body = "\n".join(out)
    assert "сир — ~120 ккал" in body


def test_format_ingredients_empty_list_returns_no_lines():
    assert fm._format_ingredients({"ingredients": []}, locale="uk") == []
    assert fm._format_ingredients({}, locale="en") == []


def test_format_ingredients_uk_handles_garbage_kcal():
    """A non-numeric estimated_calories should be silently dropped, not crash."""
    out = fm._format_ingredients({
        "ingredients": [
            {"name": "тест", "estimated_grams": 100, "estimated_calories": "oops"},
        ],
    }, locale="uk")
    body = "\n".join(out)
    assert "тест — ~100г" in body
    assert "oops" not in body
    assert "ккал" not in body


def test_phase_e_prompts_carry_language_directive():
    """F-2b Chunk 6: every GPT prompt-builder must produce a `Respond in {language}.`
    directive (or equivalent) so the model output language flips with the user."""
    from lib import config as _cfg
    from lib import mealplan as _mp

    # Functions that build complete system prompts — verify both EN and UK
    # branches contain the language token in the right place.
    en_analysis = _cfg.analysis_system_prompt(language="English")
    uk_analysis = _cfg.analysis_system_prompt(language="Ukrainian")
    assert "Respond in English." in en_analysis
    assert "Respond in Ukrainian." in uk_analysis
    assert "MUST be written in English" in en_analysis
    assert "MUST be written in Ukrainian" in uk_analysis

    en_menu = _cfg.analyze_menu_prompt(language="English")
    assert "Respond in English." in en_menu

    en_recalc = _cfg.recalc_prompt(language="English")
    uk_recalc = _cfg.recalc_prompt(language="Ukrainian")
    assert "Respond in English." in en_recalc
    assert "Respond in Ukrainian." in uk_recalc

    # Templates: the {language} placeholder is consumed by callers' .format().
    assert "{language}" in _cfg.SUMMARY_PROMPT_TEMPLATE
    assert "{language}" in _cfg.CHAT_SYSTEM_PROMPT
    assert "{language}" in _cfg.RECIPE_PROMPT_TEMPLATE

    # Mealplan system prompt builder.
    en_plan = _mp._build_system_prompt(language="English")
    uk_plan = _mp._build_system_prompt(language="Ukrainian")
    assert 'Every "name", "recipe", and "notes" string in English.' in en_plan
    assert 'Every "name", "recipe", and "notes" string in Ukrainian.' in uk_plan


def test_language_for_locale_maps_uk_to_ukrainian_else_english():
    from lib.config import language_for_locale
    assert language_for_locale("uk") == "Ukrainian"
    assert language_for_locale("en") == "English"
    assert language_for_locale("") == "English"
    assert language_for_locale("ru") == "English"  # ru/be users get EN per F-2b decision


def test_btn_label_dispatcher_accepts_both_locales_and_legacy():
    """F-2b Chunk 5: Bilingual reply-keyboard dispatcher accepts UA + EN labels
    plus legacy pre-F-2b UA labels. The 1-hour Telegram cache means a UA user
    with a stale keyboard tapping the old label must still dispatch correctly."""
    # Both locales' "favorites" labels resolve to the same command:
    assert fm.button_text_to_command(fm.btn_label("fav", locale="uk")) == "/fav"
    assert fm.button_text_to_command(fm.btn_label("fav", locale="en")) == "/fav"
    # Legacy pre-F-2b UA labels still dispatch:
    assert fm.button_text_to_command("🔢 Сканер") == "/scan"
    assert fm.button_text_to_command("📋 Меню") == "/menu"
    # Unknown text returns None:
    assert fm.button_text_to_command("hello world") is None
    # menu_button_labels() includes both locales + legacy.
    labels = fm.menu_button_labels()
    assert fm.btn_label("scan", locale="uk") in labels
    assert fm.btn_label("scan", locale="en") in labels
    assert "🔢 Сканер" in labels


def test_render_recap_png_returns_pngsignature_bytes():
    """Renderer returns valid PNG bytes (smoke; Pillow available in CI)."""
    stats = {
        "end_date": "2026-04-25", "days_logged": 5, "avg_kcal": 1820,
        "streak": 12, "weight_delta": -0.6,
        "protein_pct": 30, "carbs_pct": 45, "fat_pct": 25,
    }
    out = recap_mod.render_recap_png(stats, first_name="Vic")
    assert isinstance(out, bytes)
    assert len(out) > 5000               # any real PNG is well above this
    assert out[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes


# ---------- inactivity nudge helpers (lib.database) ----------

class _NudgeCursor:
    """SQL-capturing cursor: records every (sql, params) and returns
    rows when the seeded query matches a SELECT shape.
    """
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last_sql = sql
    def fetchall(self):
        if "SELECT" in self._last_sql and "user_profiles up" in self._last_sql:
            return self.conn.rows
        return []
    def fetchone(self):
        return None


class _NudgeConn:
    def __init__(self, rows=None):
        self.calls = []
        self.commits = 0
        self.rows = rows or []
    def cursor(self):
        return _NudgeCursor(self)
    def commit(self):
        self.commits += 1


def test_get_inactive_users_filters_and_param_shape():
    """Query gates onboarded users + opt-in + meal-cutoff + nudge-cutoff,
    in that order of params."""
    conn = _NudgeConn(rows=[
        (101, "en", None, None),
        (102, "uk", "2026-04-20T10:00:00+00:00", "2026-05-01T08:00:00+00:00"),
    ])
    out = db.get_inactive_users(conn, hours=24, cooldown_days=7)
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    # Critical filters all present:
    assert "onboarding_step = 'done'" in sql
    assert "daily_calorie_target IS NOT NULL" in sql
    assert "COALESCE(up.nudge_optout, 0) = 0" in sql
    assert "MAX(m.created_at)" in sql
    # Two cutoff params, in (meal, nudge) order — both ISO 8601 with 'T'.
    assert isinstance(params, tuple) and len(params) == 2
    assert "T" in params[0] and "+00:00" in params[0]
    assert "T" in params[1] and "+00:00" in params[1]
    # Row → dict shape.
    assert out == [
        {"user_id": 101, "lang": "en", "last_nudge_sent_at": None, "last_meal_at": None},
        {"user_id": 102, "lang": "uk",
         "last_nudge_sent_at": "2026-04-20T10:00:00+00:00",
         "last_meal_at": "2026-05-01T08:00:00+00:00"},
    ]


def test_mark_nudge_sent_updates_with_iso_timestamp():
    conn = _NudgeConn()
    db.mark_nudge_sent(conn, user_id=42)
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "UPDATE user_profiles" in sql
    assert "last_nudge_sent_at = %s" in sql
    assert params[-1] == 42  # WHERE user_id last
    # First param is an ISO 8601 UTC timestamp.
    ts = params[0]
    assert "T" in ts and "+00:00" in ts


def test_set_nudge_optout_writes_int_flag():
    conn = _NudgeConn()
    db.set_nudge_optout(conn, user_id=42, optout=True)
    db.set_nudge_optout(conn, user_id=42, optout=False)
    assert conn.commits == 2
    # First call: 1 (opt out)
    _, p1 = conn.calls[0]
    assert p1[0] == 1 and p1[-1] == 42
    # Second call: 0 (opt in)
    _, p2 = conn.calls[1]
    assert p2[0] == 0 and p2[-1] == 42


def test_profile_columns_include_nudge_fields():
    """Schema bookkeeping: get_profile() will not surface nudge fields
    unless they're in the column whitelist."""
    assert "nudge_optout" in db.PROFILE_COLUMNS
    assert "last_nudge_sent_at" in db.PROFILE_COLUMNS
    assert "nudge_optout" in db._ALLOWED_PROFILE_FIELDS
    assert "last_nudge_sent_at" in db._ALLOWED_PROFILE_FIELDS


# ---------- dashboard sprint 1 helpers ----------

def test_get_latest_recommendation_select_shape():
    """Helper issues a single SELECT with user_id param and unwraps the row."""
    captured = []

    class _Cur:
        def __init__(self, row): self._row = row
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): captured.append((sql, params))
        def fetchone(self): return self._row

    class _Conn:
        def __init__(self, row): self.row = row
        def cursor(self): return _Cur(self.row)
        def commit(self): pass

    # Row present.
    conn = _Conn(("2026-04-30", "eat more protein"))
    out = db.get_latest_recommendation(conn, user_id=42)
    assert out == {"date": "2026-04-30", "recommendation": "eat more protein"}
    assert captured[0][1] == (42,)
    assert "ORDER BY date DESC LIMIT 1" in captured[0][0]

    # No row → None.
    conn2 = _Conn(None)
    assert db.get_latest_recommendation(conn2, user_id=42) is None


def test_dashboard_normalize_log_includes_fiber_sugar():
    """REV #9: _normalize_log must read renamed 'fiber'/'sugar' keys.

    The DB columns are total_fiber_g / total_sugar_g but get_log_for_date
    renames them on its return dict. If _normalize_log read the column
    names, the bars would render permanently empty.
    """
    from api import dashboard as dash
    log = {
        "date": "2026-05-02",
        "calories": 1800, "protein": 120, "carbs": 200, "fat": 60,
        "fiber": 18, "sugar": 22, "meal_count": 3,
    }
    out = dash._normalize_log(log)
    assert out["fiber"] == 18
    assert out["sugar"] == 22
    # And the contract still holds for everything else.
    assert out["calories"] == 1800
    assert out["meal_count"] == 3


def test_dashboard_meal_to_json_includes_warning_arrays():
    """_meal_to_json must surface allergen_warnings + crohn_warnings as lists.

    By the time meal dicts reach _meal_to_json, get_meals_for_day has
    already json.loads'd the warning columns, so they're Python lists.
    """
    from api import dashboard as dash
    m = {
        "id": 7, "meal_type": "lunch", "description": "salad",
        "calories": 300, "protein_g": 20, "carbs_g": 15, "fat_g": 12,
        "allergen_warnings": ["peanut"],
        "crohn_warnings": ["high-fiber"],
    }
    out = dash._meal_to_json(m)
    assert out["allergen_warnings"] == ["peanut"]
    assert out["crohn_warnings"] == ["high-fiber"]
    # Empty / missing → empty list (defensive).
    assert dash._meal_to_json({})["allergen_warnings"] == []
    assert dash._meal_to_json({})["crohn_warnings"] == []


def test_dashboard_fiber_sugar_targets_per_user():
    """Fiber scales with calorie target (14 g/1000 kcal, clamped 20-45 g);
    sugar uses AHA caps (25 g female / 36 g male, default 36 g unset)."""
    from api import dashboard as dash

    # Female on 1800 kcal → fiber 25 (1800*14/1000 = 25.2 → 25), sugar 25.
    fb, sg = dash._fiber_sugar_targets({"sex": "female"}, 1800)
    assert fb == 25 and sg == 25

    # Male on 2800 kcal → fiber 39 → clamped to... 39 is < 45 so stays 39, sugar 36.
    fb, sg = dash._fiber_sugar_targets({"sex": "male"}, 2800)
    assert fb == 39 and sg == 36

    # Sex unset → defaults to male sugar cap (more lenient).
    _, sg = dash._fiber_sugar_targets({}, 2000)
    assert sg == 36

    # Tiny calorie target → fiber clamped at 20 g floor.
    fb, _ = dash._fiber_sugar_targets({"sex": "female"}, 1000)
    assert fb == 20

    # Very high calorie target → fiber clamped at 45 g ceiling.
    fb, _ = dash._fiber_sugar_targets({"sex": "male"}, 4000)
    assert fb == 45


def test_dashboard_build_streak_line_plural_handling():
    """Streak line is pre-rendered server-side with correct UA plurals."""
    from api import dashboard as dash
    # No streak data → None (row hidden).
    assert dash._build_streak_line(None, "en") is None
    assert dash._build_streak_line({"current_streak": 0, "longest_streak": 0,
                                    "freeze_days_remaining": 0}, "en") is None

    # English: 1 day, no best, no freezes.
    out = dash._build_streak_line(
        {"current_streak": 1, "longest_streak": 1, "freeze_days_remaining": 0},
        "en"
    )
    assert "1 day" in out and "freeze" not in out

    # English: 5 days, best 12, 2 freezes.
    out = dash._build_streak_line(
        {"current_streak": 5, "longest_streak": 12, "freeze_days_remaining": 2},
        "en"
    )
    assert "5 days" in out
    assert "12 days" in out
    assert "2 freezes" in out

    # Ukrainian: 5 must use "днів" (many), 2 → "дні" (few), 21 → "день" (singular).
    out_uk = dash._build_streak_line(
        {"current_streak": 5, "longest_streak": 12, "freeze_days_remaining": 2},
        "uk"
    )
    assert "5" in out_uk
    # 12 hits the 11-14 exception → "many" form ("днів" not "дні")
    assert "12" in out_uk
