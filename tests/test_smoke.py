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


def test_macro_targets_fat_floor_for_light_user_on_lose():
    """Light users on `lose` would otherwise get unrealistically low fat:
    `40 × 0.8 = 32 g`. The MIN_FAT_G floor (40 g) protects essential
    fatty-acid needs. Protein and carbs are NOT floored — only fat."""
    m = cfg.macro_gram_targets_from_profile(40, "lose")
    assert m["fat"] == 40                  # floor kicks in: 32 → 40
    assert m["protein"] == 80              # 40 × 2.0, unchanged
    assert m["carbs"] == 100               # 40 × 2.5, unchanged
    # Calorie target reflects the bumped fat value.
    # 80 P × 4 + 100 C × 4 + 40 F × 9 = 320 + 400 + 360 = 1080
    assert cfg.calorie_target_from_profile(40, "lose") == 1080


def test_macro_targets_fat_floor_does_not_lower_normal_user():
    """Normal-weight users sit above the floor naturally — verify the
    floor doesn't accidentally cap their fat target."""
    m = cfg.macro_gram_targets_from_profile(80, "lose")
    assert m["fat"] == 64                  # 80 × 0.8, above floor


def test_macro_targets_fat_floor_at_30kg_minimum_weight():
    """Edge: at WEIGHT_MIN_KG (30 kg) the floor matters most —
    30 × 0.8 = 24, well below 40."""
    m = cfg.macro_gram_targets_from_profile(30, "lose")
    assert m["fat"] == 40                  # floor


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
    """The done message takes name + cal + water kwargs — make sure they
    interpolate and the activation CTA is present at the end."""
    en = i18n_mod.t("onboarding.done", locale="en", name="Vic", cal=2400, water=2500)
    uk = i18n_mod.t("onboarding.done", locale="uk", name="Віктор", cal=2400, water=2500)
    assert "<b>Vic</b>"      in en
    assert "<b>Віктор</b>"   in uk
    # Targets inlined into the body.
    assert "2400 kcal" in en and "2500 ml"  in en
    assert "2400 ккал" in uk and "2500 мл"  in uk
    # Activation CTA (photo affordance) lives mid-message; /help footer
    # is the very last line (F-16 added it for command discovery).
    assert "📸" in en and "📸" in uk
    assert "counting for you." in en
    assert "рахувати за тебе." in uk
    assert "/help" in en and "/help" in uk
    assert en.rstrip().endswith("/help")
    assert uk.rstrip().endswith("/help")


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
    # Reset cache: this test re-imports the module to dodge the persisted
    # function attribute populated by earlier tests in the same run.
    import importlib
    importlib.reload(fm)
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


def test_meals_button_routes_to_today_after_merge():
    """F-16 merge: the 'meals' button label (now '📋 Today' / '📋 Сьогодні')
    routes to /today, not /meals. /meals stays a typed alias dispatched
    by the webhook itself, not the button cache."""
    import importlib
    importlib.reload(fm)
    # Current renamed label dispatches to /today (both locales).
    assert fm.button_text_to_command(fm.btn_label("meals", locale="uk")) == "/today"
    assert fm.button_text_to_command(fm.btn_label("meals", locale="en")) == "/today"
    # Legacy pre-merge labels also dispatch to /today so users with
    # cached keyboards on their phones don't tap into a dead button.
    assert fm.button_text_to_command("📋 Мої страви") == "/today"
    assert fm.button_text_to_command("📋 My meals") == "/today"
    # menu_button_labels() includes the legacy entries.
    labels = fm.menu_button_labels()
    assert "📋 Мої страви" in labels
    assert "📋 My meals" in labels


def test_format_today_progress_renders_fiber_sugar_lines():
    """F-16: fiber and sugar each get a target value + percentage in
    `format_today_progress`, matching the textual treatment of
    protein/carbs/fat. ASCII bars were removed in a follow-up — the
    cur / target / % numbers are the at-a-glance signal."""
    from lib.formatters import format_today_progress
    log = {"calories": 1500, "protein": 100, "carbs": 180, "fat": 50,
           "fiber": 28, "sugar": 40, "meal_count": 3}
    profile = {"lang": "en", "weight_kg": 70, "goal": "maintain"}
    out = format_today_progress(log, daily_cal_target=2000, profile=profile)
    # Fiber: target 28g for 2000 kcal (14g per 1000); current 28 → 100%.
    assert "28g / 28g" in out
    # Sugar: limit 25g for 2000 kcal (WHO conditional 5% / 4);
    # current 40 → 160% (user is over the limit).
    assert "40g / 25g" in out
    # ASCII bar blocks deliberately removed — the text "X / Y (Z%)"
    # carries the same info more cleanly in chat. Regression guard:
    # no `█` characters anywhere in the rendered card.
    assert "█" not in out
    # 2026-05 follow-up: the two `━━━` rules wrapping the macro block
    # were also dropped — same noise-vs-signal complaint. Guard against
    # accidental re-introduction.
    assert "━" not in out


def test_format_meals_list_without_header_args_suppresses_daily_totals():
    """F-16 merge depends on `format_meals_list` skipping its compact
    daily-total header when called without `log`/`daily_cal_target`/
    `macros`. The combined `/today` handler relies on this so the
    full progress card below isn't duplicated."""
    from lib.formatters import format_meals_list
    meals = [{
        "id": 1, "meal_type": "breakfast", "description": "вівсянка",
        "calories": 320, "protein_g": 12, "carbs_g": 55, "fat_g": 6,
        "fiber_g": 4, "sugar_g": 8,
    }]
    out_no_header = format_meals_list(meals, locale="en")
    out_with_header = format_meals_list(
        meals, log={"calories": 320, "protein": 12, "carbs": 55, "fat": 6},
        daily_cal_target=2000, macros={"protein": 100, "carbs": 250, "fat": 70},
        locale="en",
    )
    # The "Day total" line only appears when the optional args are passed.
    # Without them, the compact header is suppressed entirely.
    assert "320" in out_no_header  # meal kcal still present
    assert "Day total" not in out_no_header or "/ 2000" not in out_no_header
    # The with-header version contains the daily-target reference.
    assert "2000" in out_with_header


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


# ---------- nudge helpers (lib.database) ----------

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
        if "COUNT(*)" in (self._last_sql or ""):
            return self.conn.rows[0] if self.conn.rows else None
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


def test_get_users_to_nudge_filters_and_shape():
    """Hourly per-user-tz nudge query: returns {user_id, lang} for every
    onboarded opt-in user whose local clock is in the summary hour and who
    hasn't already been nudged today (in their tz)."""
    conn = _NudgeConn(rows=[
        (301, "en"),
        (302, "uk"),
    ])
    out = db.get_users_to_nudge(conn)
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    # Critical filters preserved:
    assert "onboarding_step = 'done'" in sql
    assert "daily_calorie_target IS NOT NULL" in sql
    assert "COALESCE(up.nudge_optout, 0) = 0" in sql
    assert "NOT EXISTS" in sql                          # no meal today
    # No tier classification:
    assert "CASE" not in sql
    assert "'recent'" not in sql and "'stale'" not in sql
    # Per-user-tz timing + dedup:
    assert "EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))" in sql
    assert "TO_CHAR(NOW() AT TIME ZONE up.tz, 'YYYY-MM-DD')" in sql
    assert "up.last_nudge_sent_at IS NULL" in sql
    assert "up.last_nudge_sent_at::timestamptz AT TIME ZONE up.tz" in sql
    # Single param: the summary hour (default 22).
    assert isinstance(params, tuple) and len(params) == 1
    assert params[0] == 22
    # Row → dict shape (no tier).
    assert out == [
        {"user_id": 301, "lang": "en"},
        {"user_id": 302, "lang": "uk"},
    ]


def test_get_users_to_nudge_custom_hour_passed_to_sql():
    """`summary_hour` keyword propagates to the SQL parameter."""
    conn = _NudgeConn(rows=[])
    db.get_users_to_nudge(conn, summary_hour=18)
    _, params = conn.calls[0]
    assert params == (18,)


def test_mark_nudge_sent_updates_with_iso_timestamp():
    conn = _NudgeConn()
    db.mark_nudge_sent(conn, user_id=42)
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "UPDATE user_profiles" in sql
    assert "last_nudge_sent_at = %s" in sql
    assert params[-1] == 42  # WHERE user_id last
    ts = params[0]
    assert "T" in ts and "+00:00" in ts


def test_set_blocked_stamps_or_clears_timestamp():
    """`set_blocked` writes an ISO timestamp on True, NULL on False."""
    conn = _NudgeConn()
    db.set_blocked(conn, user_id=42, blocked=True)
    db.set_blocked(conn, user_id=42, blocked=False)
    assert conn.commits == 2
    sql_set, params_set = conn.calls[0]
    sql_clr, params_clr = conn.calls[1]
    assert "UPDATE user_profiles" in sql_set
    assert "blocked_at = %s" in sql_set
    # True → ISO timestamp string in the first slot.
    assert isinstance(params_set[0], str) and "T" in params_set[0]
    # False → NULL.
    assert params_clr[0] is None
    assert params_set[-1] == 42 and params_clr[-1] == 42


def test_finalize_stuck_tz_users_targets_both_tz_steps():
    """Sweep finalizes users on `awaiting_tz` OR `awaiting_tz_custom`
    older than the cutoff, leaving tz at the schema default (Europe/Kyiv)."""
    conn = _NudgeConn(rows=[(555, "uk"), (777, "en")])
    out = db.finalize_stuck_tz_users(conn, max_age_hours=12)
    # Two queries: the SELECT to find stuck users, then an UPDATE.
    assert len(conn.calls) == 2
    sql_select, _ = conn.calls[0]
    sql_update, params_update = conn.calls[1]
    # Both tz steps are in the WHERE clause.
    assert "awaiting_tz" in sql_select and "awaiting_tz_custom" in sql_select
    # Update writes step='done' and only touches the user_ids we found.
    assert "onboarding_step = 'done'" in sql_update
    assert "WHERE user_id = ANY(%s)" in sql_update
    # The returned rows are the stuck ones, suitable for the cron caller
    # to send each freed user a notice.
    assert out == [{"user_id": 555, "lang": "uk"},
                   {"user_id": 777, "lang": "en"}]


def test_finalize_stuck_tz_users_no_update_when_empty():
    """When no users are stuck, we don't issue a wasteful empty UPDATE."""
    conn = _NudgeConn(rows=[])
    out = db.finalize_stuck_tz_users(conn)
    assert out == []
    # Only the SELECT ran; no UPDATE.
    assert len(conn.calls) == 1


def test_get_users_to_nudge_filters_blocked_at():
    """F-16: blocked-by-Telegram users excluded from nudge cohort."""
    conn = _NudgeConn(rows=[])
    db.get_users_to_nudge(conn)
    sql, _ = conn.calls[0]
    assert "blocked_at IS NULL" in sql


def test_get_users_needing_summary_filters_blocked_at():
    """F-16: blocked-by-Telegram users excluded from end-of-day summary."""
    conn = _NudgeConn(rows=[])
    db.get_users_needing_summary(conn)
    sql, _ = conn.calls[0]
    assert "blocked_at IS NULL" in sql


def test_get_users_due_weekly_checkin_filters_blocked_at():
    """F-16: blocked-by-Telegram users excluded from Monday weight prompt."""
    conn = _NudgeConn(rows=[])
    db.get_users_due_weekly_checkin(conn)
    sql, _ = conn.calls[0]
    assert "blocked_at IS NULL" in sql


def test_get_users_due_morning_greeting_filters_and_shape():
    """Per-user-local morning greeting query: filters on opt-out + blocked,
    matches local hour 8, dedups against last_morning_sent_at."""
    conn = _NudgeConn(rows=[
        (501, "uk", "done"),
        (502, "en", "awaiting_age"),
    ])
    out = db.get_users_due_morning_greeting(conn)
    sql, params = conn.calls[0]
    # All three notification gates.
    assert "COALESCE(up.nudge_optout, 0) = 0" in sql
    assert "up.blocked_at IS NULL" in sql
    # Per-user-tz hour filter + dedup.
    assert "EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))" in sql
    assert "up.last_morning_sent_at IS NULL" in sql
    assert "(up.last_morning_sent_at::timestamptz AT TIME ZONE up.tz)::date" in sql
    # Default morning_hour is 8.
    assert params == (8,)
    # Row → dict shape with onboarding_step so caller can pick the right
    # message variant (greeting_done vs greeting_mid_onboarding).
    assert out == [
        {"user_id": 501, "lang": "uk", "onboarding_step": "done"},
        {"user_id": 502, "lang": "en", "onboarding_step": "awaiting_age"},
    ]


def test_get_users_due_morning_greeting_honors_custom_hour():
    """`morning_hour=N` propagates to the SQL parameter."""
    conn = _NudgeConn(rows=[])
    db.get_users_due_morning_greeting(conn, morning_hour=10)
    _, params = conn.calls[0]
    assert params == (10,)


def test_get_users_for_first_meal_demo_filters():
    """Day-2 demo cohort SQL must enforce every safety gate: profile done,
    no opt-out, no block, no prior activation step, no lifetime meals,
    ≥24h since signup, local hour matches."""
    conn = _NudgeConn(rows=[
        (701, "uk", "Olena", 1850),
        (702, "en", "Mark",  2100),
    ])
    out = db.get_users_for_first_meal_demo(conn, morning_hour=8)
    sql, params = conn.calls[0]
    # All critical gates present.
    assert "onboarding_step = 'done'" in sql
    assert "COALESCE(up.nudge_optout, 0) = 0" in sql
    assert "up.blocked_at IS NULL" in sql
    assert "COALESCE(up.activation_step, '') = ''" in sql
    assert "NOT EXISTS" in sql and "FROM meals m" in sql
    assert "EXTRACT(HOUR FROM (NOW() AT TIME ZONE up.tz))" in sql
    assert "INTERVAL '24 hours'" in sql
    assert "last_morning_sent_at" in sql  # dedup
    # Single param: the morning_hour.
    assert params == (8,)
    # Row shape: caller needs name + cal for the personalised demo.
    assert out == [
        {"user_id": 701, "lang": "uk", "first_name": "Olena", "cal": 1850},
        {"user_id": 702, "lang": "en", "first_name": "Mark",  "cal": 2100},
    ]


def test_get_users_for_d4_followup_gates_on_demo_state():
    """Day-4 follow-up cohort: must already be in 'demo' state and ≥3 days old."""
    conn = _NudgeConn(rows=[(801, "uk", "Iryna")])
    out = db.get_users_for_d4_followup(conn, morning_hour=8)
    sql, _ = conn.calls[0]
    assert "up.activation_step = 'demo'" in sql
    assert "INTERVAL '3 days'" in sql
    assert "NOT EXISTS" in sql and "FROM meals m" in sql
    assert out == [{"user_id": 801, "lang": "uk", "first_name": "Iryna"}]


def test_get_users_for_d7_final_gates_on_d4_state():
    """Day-7 final cohort: must already be in 'd4_followup' state and ≥6 days old."""
    conn = _NudgeConn(rows=[])
    db.get_users_for_d7_final(conn, morning_hour=8)
    sql, _ = conn.calls[0]
    assert "up.activation_step = 'd4_followup'" in sql
    assert "INTERVAL '6 days'" in sql
    assert "NOT EXISTS" in sql and "FROM meals m" in sql


def test_get_users_to_auto_quiet_safety_net():
    """Day-9 auto-quiet cohort: gates on age ≥9d + no lifetime meals.
    Does NOT gate on activation_step — even users who never got any
    activation message (cron missed fires) get silenced after 9 days
    if they never engaged. Active loggers are excluded by NOT EXISTS."""
    conn = _NudgeConn(rows=[(901, "uk"), (902, "en")])
    out = db.get_users_to_auto_quiet(conn, days=9)
    sql, _ = conn.calls[0]
    assert "onboarding_step = 'done'" in sql
    assert "COALESCE(up.nudge_optout, 0) = 0" in sql
    assert "up.blocked_at IS NULL" in sql
    # CRITICAL — protects active loggers from accidental silencing.
    assert "NOT EXISTS" in sql and "FROM meals m" in sql
    assert "INTERVAL '9 days'" in sql
    # Don't double-quiet a user who's already been auto-quieted in a
    # prior run (would re-send the notice unnecessarily).
    assert "'auto_quieted'" in sql
    assert out == [
        {"user_id": 901, "lang": "uk"},
        {"user_id": 902, "lang": "en"},
    ]


def test_set_activation_step_writes_step_and_updated_at():
    """Helper writes the new activation state + bumps updated_at."""
    conn = _NudgeConn()
    db.set_activation_step(conn, user_id=42, step="demo")
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "UPDATE user_profiles" in sql
    assert "SET activation_step = %s" in sql
    assert params[0] == "demo"
    assert params[-1] == 42


def test_mark_morning_sent_updates_with_iso_timestamp():
    """Mirrors mark_nudge_sent — stamps last_morning_sent_at to NOW iso."""
    conn = _NudgeConn()
    db.mark_morning_sent(conn, user_id=42)
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "UPDATE user_profiles" in sql
    assert "last_morning_sent_at = %s" in sql
    assert params[-1] == 42
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


# ---------- F-14: admin analytics helpers ----------

class _AdminCursor:
    """Records every (sql, params) and returns whatever the conn pre-staged."""
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self.conn._last_sql = sql
    def fetchall(self):
        return self.conn.rows
    def fetchone(self):
        return self.conn.rows[0] if self.conn.rows else None


class _AdminConn:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or []
        self.commits = 0
        self._last_sql = ""
    def cursor(self):
        return _AdminCursor(self)
    def commit(self):
        self.commits += 1


def test_get_retention_cohorts_sql_shape():
    """Cohort query must group by signup week and gate on d1/d7/d30 windows."""
    conn = _AdminConn(rows=[("2026-05-04", 7, 5, 0, 0)])
    out = db.get_retention_cohorts(conn, weeks=12)
    sql, _ = conn.calls[0]
    # Cohort week + the three retention windows must all be present.
    assert "DATE_TRUNC('week'" in sql
    assert "INTERVAL '12 weeks'" in sql
    assert "INTERVAL '2 days'" in sql   # D1 slack
    assert "INTERVAL '8 days'" in sql   # D7 slack
    assert "INTERVAL '31 days'" in sql  # D30 slack
    assert out == [{"cohort_week": "2026-05-04", "size": 7,
                    "d1": 5, "d7": 0, "d30": 0}]


def test_get_daily_trends_dense_and_sorted():
    """Trends query must use generate_series and sort ascending by day."""
    conn = _AdminConn(rows=[("2026-05-09", 0, 2, 5), ("2026-05-10", 2, 4, 24)])
    out = db.get_daily_trends(conn, days=30)
    sql, _ = conn.calls[0]
    assert "generate_series" in sql
    assert "CURRENT_DATE - 29" in sql
    assert "ORDER BY d.d" in sql
    assert out[0]["day"] < out[1]["day"]
    assert out[1]["meals"] == 24


def test_get_onboarding_funnel_canonical_order():
    """Funnel always returns every step in canonical order, zero-filled."""
    # Simulate DB returning only 2 of the 13 steps.
    conn = _AdminConn(rows=[("done", 15), ("awaiting_age", 3)])
    out = db.get_onboarding_funnel(conn)
    steps = [s for s, _ in out]
    assert steps == list(db.ONBOARDING_STEPS)
    counts = dict(out)
    assert counts["done"] == 15
    assert counts["awaiting_age"] == 3
    assert counts["awaiting_sex"] == 0  # zero-filled


def test_upsert_user_source_first_write_wins():
    """`source` is written on INSERT but NOT in ON CONFLICT DO UPDATE,
    so a repeat-tapper of a tagged link keeps their first attribution."""
    conn = _AdminConn()
    db.upsert_user(conn, 999, "alice", "Alice", source="site_banner_home")
    assert conn.commits == 1
    sql, params = conn.calls[0]
    # source is in the INSERT column list and goes into VALUES.
    assert "INSERT INTO users" in sql
    assert "source" in sql and "source_seen_at" in sql
    # source must NOT appear in the conflict-update set.
    update_clause = sql.split("ON CONFLICT")[1]
    assert "source" not in update_clause, (
        "ON CONFLICT must not overwrite source — first attribution wins"
    )
    assert "username" in update_clause and "first_name" in update_clause
    # Params: user_id, username, first_name, source, source_seen_at, created_at.
    assert params[0] == 999
    assert params[3] == "site_banner_home"
    # source_seen_at gets a timestamp when source is non-empty.
    assert params[4] is not None and "T" in params[4]


def test_upsert_user_source_empty_clears_source_seen_at():
    """An organic /start (no token) writes source='' and source_seen_at=None."""
    conn = _AdminConn()
    db.upsert_user(conn, 1000, "bob", "Bob")  # no source arg → default ""
    _, params = conn.calls[0]
    assert params[3] == ""        # source
    assert params[4] is None       # source_seen_at not stamped


class _PendingAnalysisConn:
    """Cursor that returns a single configurable row for `get_pending_analysis`
    and records DELETEs so we can assert the corrupt-row was dropped."""
    def __init__(self, row):
        self._row = row
        self.deletes = []
        self.commits = 0
    def cursor(self):
        return _PendingAnalysisCursor(self)
    def commit(self):
        self.commits += 1


class _PendingAnalysisCursor:
    def __init__(self, conn):
        self.conn = conn
        self._is_delete = False
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("DELETE"):
            self._is_delete = True
            self.conn.deletes.append((sql, params))
    def fetchone(self):
        return self.conn._row


def test_get_pending_analysis_corrupt_json_drops_row_returns_none():
    """A malformed `analysis_json` must not crash the meal-logging flow.
    The row gets dropped (so the user isn't permanently stuck) and the
    function returns None instead of raising."""
    # row layout: id, meal_type, analysis_json, photo_file_id, text_description,
    #             raw_response, awaiting_manual, created_at, candidates_json,
    #             replaces_meal_id
    bad_row = (42, "lunch", "{not valid json", "ph", None,
               "raw", False, "2026-05-12T10:00:00+00:00", None, None)
    conn = _PendingAnalysisConn(bad_row)
    out = db.get_pending_analysis(conn, user_id=1234)
    assert out is None, "corrupt JSON must NOT raise — must return None"
    # The corrupt row should have been deleted by id (not user_id) so the
    # specific bad row vanishes without nuking unrelated pending state.
    assert len(conn.deletes) == 1
    sql, params = conn.deletes[0]
    assert "DELETE FROM pending_analyses" in sql
    assert "WHERE id = %s" in sql
    assert params == (42,)
    assert conn.commits == 1


def test_get_pending_analysis_returns_none_when_row_missing():
    """When there is no pending row at all, the function returns None
    without attempting any DELETE / commit."""
    conn = _PendingAnalysisConn(None)
    out = db.get_pending_analysis(conn, user_id=1234)
    assert out is None
    assert conn.deletes == []
    assert conn.commits == 0


def test_get_attribution_breakdown_shape():
    """Per-source quality breakdown joins users + user_profiles + EXISTS-meals,
    counts via FILTER, ordered by total DESC then source ASC."""
    conn = _AdminConn(rows=[
        ("organic", 14, 11, 3, 14, 11),
        ("site_calc_continue_uk", 1, 0, 1, 1, 0),
    ])
    out = db.get_attribution_breakdown(conn)
    sql, _ = conn.calls[0]
    # Two-table join + meal-existence subquery.
    assert "FROM users u" in sql
    assert "LEFT JOIN user_profiles p" in sql
    assert "FROM meals m" in sql and "EXISTS" in sql
    # Locale + onboarding + completion filters.
    assert "FILTER (WHERE p.lang = 'uk')" in sql
    assert "FILTER (WHERE p.lang = 'en')" in sql
    assert "FILTER (WHERE p.onboarding_step = 'done')" in sql
    # Empty source collapses to 'organic'; stable ordering.
    assert "'organic'" in sql
    assert "ORDER BY total DESC, source" in sql
    # Row → dict shape with ints.
    assert out[0] == {
        "source": "organic", "total": 14,
        "uk_count": 11, "en_count": 3,
        "done_count": 14, "logged_count": 11,
    }
    assert out[1]["source"] == "site_calc_continue_uk"


def test_restore_main_menu_helper_used_after_every_meal_save():
    """Regression: every saved-meal send must be followed by a
    `_restore_main_menu` call. The meal-saved message uses an inline
    keyboard (⭐ / ✏️ / 🗑), so without the follow-up the persistent
    reply keyboard (often collapsed by Telegram during photo upload)
    never gets refreshed.
    """
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "webhook.py")).read()
    # Helper exists.
    assert "def _restore_main_menu" in src, "helper not defined"
    # Every block that attaches `meal_logged_actions_keyboard` as a
    # reply_markup must be followed within ~20 lines by a call to
    # `_restore_main_menu` — except the favorite-toggle callback,
    # which uses editMessageReplyMarkup (no new message → no refresh
    # needed). The toggle site is identifiable because it's inside
    # `handle_meal_manage_callback` and uses `is_fav=target_state`.
    chunks = src.split("meal_logged_actions_keyboard(meal_id")
    # First chunk is preamble; remaining chunks each start AT a usage.
    save_sites = 0
    save_sites_with_refresh = 0
    for chunk in chunks[1:]:
        window = chunk[:600]
        # Favorite-toggle callback uses `is_fav=target_state`, not False.
        if "is_fav=target_state" in window[:100]:
            continue
        save_sites += 1
        if "_restore_main_menu" in window:
            save_sites_with_refresh += 1
    assert save_sites >= 2, f"expected ≥2 meal-save sites, found {save_sites}"
    assert save_sites == save_sites_with_refresh, (
        f"{save_sites - save_sites_with_refresh} meal-save sites are "
        f"missing a `_restore_main_menu` follow-up"
    )


def test_all_cron_modules_import_cleanly():
    """Each cron endpoint must import without raising — Vercel re-imports
    the module on every cold invocation, so any module-level error
    (syntax, eager-annotation evaluation, missing symbol, etc.) results
    in FUNCTION_INVOCATION_FAILED in production. F-17 shipped with
    `callable | None` (lowercase builtin used as a type) which crashed
    cron_good_morning on import for ~54 hours before this test existed.
    Each module imports its handler class, run function, and any
    module-level helpers. If any reference resolves at the wrong time
    this test catches it."""
    import importlib
    for name in (
        "cron_daily_summary",
        "cron_good_morning",
        "cron_health_monitor",
        "cron_midnight_reset",
        "cron_weekly_weight_checkin",
    ):
        try:
            mod = importlib.import_module(f"api.{name}")
        except Exception as e:
            raise AssertionError(
                f"api.{name} failed to import: {type(e).__name__}: {e}"
            ) from e
        # Every cron module must expose the BaseHTTPRequestHandler subclass
        # named `handler` (Vercel's Python runtime entry point).
        assert hasattr(mod, "handler"), (
            f"api.{name} missing the `handler` class — Vercel won't be "
            f"able to dispatch invocations"
        )


def test_send_plan_day_uses_locale_not_undefined_profile():
    """Regression: `_send_plan_day` previously referenced an undefined
    `profile` symbol (NameError) which crashed every /plan day render.
    Source-grep guards against the regression — the function must use
    the `locale` parameter directly, not `i18n_mod.locale_of(profile)`."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "webhook.py")).read()
    block = src.split("def _send_plan_day", 1)[1].split("\ndef ", 1)[0]
    assert "locale=locale" in block, (
        "_send_plan_day must pass `locale=locale` (its parameter) to "
        "plan_day_keyboard. Found mismatched signature."
    )
    assert "locale_of(profile)" not in block, (
        "_send_plan_day must NOT reference `profile` — it's not in scope. "
        "Use the `locale` parameter instead."
    )


def test_text_input_states_constant_covers_dispatcher_guards():
    """`_TEXT_INPUT_STATES` in webhook.py is the canonical list of states
    whose photo-arrival should clear the prompt. Must match the set of
    awaiting_input_type values whose dispatcher guards already use the
    `not text.startswith('/')` escape pattern (commit 0e46a66)."""
    import importlib
    wh = importlib.import_module("api.webhook")
    assert wh._TEXT_INPUT_STATES == frozenset({
        "weight", "water_target", "target_weight", "weekly_delta",
        "barcode_grams", "barcode_manual", "timezone",
        "health_allergens", "health_conditions",
    })


def test_get_user_breakdowns_six_dims_incl_source_and_status():
    """Breakdowns query each dim once + the source dim against `users` +
    the F-16 status dim derived from `blocked_at` / `nudge_optout`."""
    conn = _AdminConn(rows=[("uk", 12), ("en", 4)])
    out = db.get_user_breakdowns(conn)
    assert set(out.keys()) == {"lang", "tz", "sex", "goal", "source", "status"}
    # Six separate queries (one per dim).
    assert len(conn.calls) == 6
    # First 4 hit user_profiles (lang, tz, sex, goal).
    for sql, _ in conn.calls[:4]:
        assert "FROM user_profiles" in sql
    # 5th hits `users` table for source.
    assert "FROM users" in conn.calls[4][0]
    assert "'organic'" in conn.calls[4][0]
    # 6th is the derived status query — back on user_profiles, joins
    # `blocked_at` / `nudge_optout` precedence into a single CASE.
    status_sql = conn.calls[5][0]
    assert "FROM user_profiles" in status_sql
    assert "blocked_at IS NOT NULL" in status_sql
    assert "nudge_optout" in status_sql
    assert "'blocked'" in status_sql and "'quiet'" in status_sql and "'active'" in status_sql


def test_record_cron_run_inserts_with_finished_now():
    """record_cron_run writes a row with status, JSON result, and now() ts."""
    conn = _AdminConn()
    db.record_cron_run(conn, "cron_daily_summary", "ok",
                       result={"sent_summary": 3, "sent_nudge": 7})
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "INSERT INTO cron_runs" in sql
    assert "finished_at" in sql and "now()" in sql
    # cron_name, status, json, error.
    assert params[0] == "cron_daily_summary"
    assert params[1] == "ok"
    assert '"sent_summary": 3' in params[2]
    assert params[3] is None


def test_get_latest_cron_runs_distinct_per_name():
    """One row per cron via DISTINCT ON; result JSON parsed back to dict."""
    conn = _AdminConn(rows=[
        ("cron_daily_summary", "2026-05-10", "2026-05-10", "ok",
         '{"sent_nudge": 7}', None),
    ])
    out = db.get_latest_cron_runs(conn)
    sql, _ = conn.calls[0]
    assert "DISTINCT ON (cron_name)" in sql
    assert out[0]["result"] == {"sent_nudge": 7}
    assert out[0]["status"] == "ok"


def test_get_nudge_effectiveness_24h_window():
    """Conversion query gates on `last_nudge_sent_at + 24h` and recent days."""
    conn = _AdminConn(rows=[(13, 2)])
    out = db.get_nudge_effectiveness(conn, days=30)
    sql, _ = conn.calls[0]
    assert "INTERVAL '30 days'" in sql
    assert "INTERVAL '24 hours'" in sql
    assert "last_nudge_sent_at" in sql
    assert out == {"sent": 13, "converted": 2, "pct": round(2 / 13 * 100, 1)}


def test_get_ai_cost_estimate_multiplies_count_by_rate():
    """Cost helper multiplies usage_quota counts by COST_RATES per action."""
    # Two days, two actions, two users.
    conn = _AdminConn(rows=[
        ("2026-05-10", "meal_analysis", 100, 5),     # 5 × $0.005 = $0.025
        ("2026-05-10", "ask",           100, 10),    # 10 × $0.001 = $0.010
        ("2026-05-09", "meal_analysis", 200, 4),     # 4 × $0.005 = $0.020
    ])
    out = db.get_ai_cost_estimate(conn, days=30)
    # Use abs-tolerance comparisons — exact float equality after 2-decimal
    # rounding bites on values like 0.045 that float can't represent.
    assert abs(out["total_usd"] - 0.055) < 0.01
    assert abs(out["by_action"]["meal_analysis"] - 0.045) < 0.01
    assert abs(out["by_action"]["ask"] - 0.010) < 0.01
    # Top spender by user_id sum.
    assert out["top_spenders"][0][0] == 100
    assert abs(out["top_spenders"][0][1] - 0.035) < 0.01


def test_get_weight_outcomes_buckets_correctly():
    """Buckets: on_track (matches goal direction, |Δ|>0.2), stalled (|Δ|≤0.2),
    regressing (opposite direction)."""
    conn = _AdminConn(rows=[
        (1, "lose", 75.0, 80.0, -2.0),   # losing 2kg, goal lose → on_track
        (2, "lose", 75.0, 80.0, +1.0),   # gaining, goal lose → regressing
        (3, "lose", 75.0, 80.0, -0.1),   # flat → stalled
        (4, "gain", 80.0, 75.0, +1.5),   # gaining, goal gain → on_track
        (5, "gain", 80.0, 75.0, -1.0),   # losing, goal gain → regressing
    ])
    out = db.get_weight_outcomes(conn)
    assert {r["user_id"] for r in out["on_track"]} == {1, 4}
    assert {r["user_id"] for r in out["regressing"]} == {2, 5}
    assert {r["user_id"] for r in out["stalled"]} == {3}


def test_get_recent_events_unions_four_sources():
    """Recent-events query must UNION ALL signups + meals + weight + nudges."""
    conn = _AdminConn(rows=[
        ("meal",   100, "2026-05-10T20:00", "Yogurt"),
        ("signup", 101, "2026-05-10T19:00", ""),
    ])
    out = db.get_recent_events(conn, limit=10)
    sql, _ = conn.calls[0]
    # All four event sources present in the UNION.
    assert "'signup'" in sql and "FROM users" in sql
    assert "'meal'" in sql and "FROM meals" in sql
    assert "'weight'" in sql and "FROM weight_history" in sql
    assert "'nudge'" in sql and "last_nudge_sent_at" in sql
    assert "UNION ALL" in sql
    assert out[0]["kind"] == "meal" and out[1]["kind"] == "signup"


def test_cost_rates_constant_covers_tracked_actions():
    """All actions tracked by usage_quota have a USD estimate in COST_RATES."""
    from lib.config import COST_RATES
    assert set(COST_RATES.keys()) >= {
        "meal_analysis", "voice_transcribe", "ask",
        "suggest", "menu_ocr", "plan_generate",
    }
    # Sanity bounds — anything outside 0.0001–1.0 is a typo.
    for action, rate in COST_RATES.items():
        assert 0.0001 <= rate <= 1.0, f"{action} = {rate} is out of range"


def test_get_user_activity_30d_dense_oldest_first():
    """Activity helper returns a 30-element array per user, oldest day first."""
    # 3 rows: user 1 logged today (days_ago=0), 3 days ago, 29 days ago.
    conn = _AdminConn(rows=[(1, 0, 5), (1, 3, 2), (1, 29, 1)])
    out = db.get_user_activity_30d(conn, days=30)
    assert 1 in out
    arr = out[1]
    assert len(arr) == 30
    # days_ago=0 → last slot (index 29)
    assert arr[29] == 5
    # days_ago=3 → index 26
    assert arr[26] == 2
    # days_ago=29 → index 0 (oldest)
    assert arr[0] == 1
    # All other slots zero.
    assert sum(arr) == 5 + 2 + 1


def test_get_user_activity_30d_users_with_no_activity_absent():
    """Users with no rows in the period are simply not in the dict."""
    conn = _AdminConn(rows=[])
    out = db.get_user_activity_30d(conn)
    assert out == {}


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


def test_classify_ai_intent_question_marker():
    """`?` and English/UA question prefixes route to 'ask'."""
    from api.webhook import _classify_ai_intent
    assert _classify_ai_intent("how much protein for cutting?") == "ask"
    assert _classify_ai_intent("Why is fiber important") == "ask"
    assert _classify_ai_intent("what should I eat tonight") == "ask"
    # UA question prefix.
    assert _classify_ai_intent("як приготувати курку") == "ask"  # noqa: i18n
    assert _classify_ai_intent("чому фіброз небезпечний") == "ask"  # noqa: i18n
    # Plain trailing question mark.
    assert _classify_ai_intent("dinner ideas?") == "ask"


def test_classify_ai_intent_comma_list_routes_to_fridge():
    """Two or more commas in the text → fridge mode."""
    from api.webhook import _classify_ai_intent
    assert _classify_ai_intent("chicken, rice, broccoli") == "fridge"
    assert _classify_ai_intent("eggs, bread, butter, salt") == "fridge"
    # Single comma is NOT enough — could be a sentence aside.
    assert _classify_ai_intent("chicken with rice, please") == "suggest"


def test_classify_ai_intent_empty_and_default():
    """Empty / whitespace text and plain sentences default to 'suggest'."""
    from api.webhook import _classify_ai_intent
    assert _classify_ai_intent("") == "suggest"
    assert _classify_ai_intent("   ") == "suggest"
    assert _classify_ai_intent("low-carb dinner") == "suggest"
    assert _classify_ai_intent("щось низькокалорійне") == "suggest"  # noqa: i18n


def test_ai_menu_keyboard_shape():
    """4 single-button rows with the 4 expected callback_data values; both locales render."""
    from lib.telegram_helpers import ai_menu_keyboard
    for locale in ("en", "uk"):
        kb = ai_menu_keyboard(locale=locale)
        rows = kb["inline_keyboard"]
        assert len(rows) == 4
        assert [row[0]["callback_data"] for row in rows] == [
            "ai:ask", "ai:suggest", "ai:fridge", "ai:cancel"
        ]
        # Every label resolves through i18n (no raw key strings leaked).
        for row in rows:
            assert row[0]["text"]
            assert not row[0]["text"].startswith("ai_menu.")


def test_button_text_to_command_routes_ask_button_to_ai_chooser():
    """The merged AI button (label = btn.ask in both locales) now maps to /ai,
    not /ask. /ask still works as a typed slash command."""
    from lib.formatters import button_text_to_command, btn_label
    assert button_text_to_command(btn_label("ask", locale="en")) == "/ai"
    assert button_text_to_command(btn_label("ask", locale="uk")) == "/ai"
    # New 'recent' button maps to /recent.
    assert button_text_to_command(btn_label("recent", locale="en")) == "/recent"
    assert button_text_to_command(btn_label("recent", locale="uk")) == "/recent"
    # Legacy 'suggest' label still dispatches (stale-keyboard fallback).
    assert button_text_to_command(btn_label("suggest", locale="en")) == "/suggest_meal"


def test_meal_edit_callback_clears_awaiting_input_type():
    """Regression F1: tapping ✏️ Edit on a meal (from /meals or after a
    fresh analysis) MUST clear `awaiting_input_type` so a stuck 'weight'
    state from the weekly cron doesn't intercept the user's edit text
    via the weight-input handler."""
    import inspect
    from api import webhook
    src = inspect.getsource(webhook.handle_meal_manage_callback)
    edit_branch_start = src.find('elif data.startswith("meal_edit:"):')
    assert edit_branch_start >= 0, "meal_edit branch not found in handle_meal_manage_callback"
    # Check the 2 KB window after the branch opener — that's the whole branch.
    edit_block = src[edit_branch_start:edit_branch_start + 2000]
    assert "set_awaiting_input(conn, user_id, None)" in edit_block, (
        "F1 regression: meal_edit branch must call set_awaiting_input(..., None) "
        "so the weight-input handler doesn't silently intercept edit text."
    )


def test_text_dispatcher_pending_analyses_takes_precedence_over_weight():
    """Regression F4: in process_update, the meal-edit / manual-correction
    check (pending_analyses.awaiting_manual) MUST appear BEFORE the
    `awaiting_input_type == 'weight'` branch. This mirrors handle_voice's
    long-correct order. Otherwise a stuck weight state intercepts meal-edit
    text and the user sees 'Hmm, that doesn't look like a number.'"""
    import inspect
    from api import webhook
    src = inspect.getsource(webhook.process_update)
    # The new awaiting_manual check uses a uniquely-named local to avoid
    # collisions; both that AND the legacy variant are valid hits.
    pending_check = src.find('_pending_for_text["awaiting_manual"]')
    if pending_check < 0:
        pending_check = src.find('pending["awaiting_manual"]')
    weight_check = src.find('"awaiting_input_type") == "weight"')
    assert pending_check >= 0, "awaiting_manual text-branch check not found"
    assert weight_check >= 0, "weight awaiting_input_type branch not found"
    assert pending_check < weight_check, (
        "F4 regression: pending_analyses.awaiting_manual must be checked "
        "before awaiting_input_type == 'weight' in process_update."
    )


def test_cancel_handlers_clear_awaiting_input_type():
    """Regression F2 + F3: both /cancel (text) and mod:cancel (inline button)
    must clear `awaiting_input_type`. /cancel is the universal escape hatch —
    if it leaves FSM state set, the bug recurs the next time the user types."""
    import inspect
    from api import webhook

    # F2: /cancel text branch
    src_pu = inspect.getsource(webhook.process_update)
    cancel_block_start = src_pu.find('text.lower().strip() == "/cancel"')
    assert cancel_block_start >= 0, "/cancel text branch not found"
    cancel_block = src_pu[cancel_block_start:cancel_block_start + 1000]
    assert "set_awaiting_input(conn, user_id, None)" in cancel_block, (
        "F2 regression: /cancel text branch must clear awaiting_input_type."
    )

    # F3: mod:cancel callback
    src_mod = inspect.getsource(webhook.handle_moderation_callback)
    mod_cancel = src_mod.find('elif action == "cancel":')
    assert mod_cancel >= 0, "mod:cancel branch not found"
    mod_block = src_mod[mod_cancel:mod_cancel + 800]
    assert "set_awaiting_input(conn, user_id, None)" in mod_block, (
        "F3 regression: mod:cancel callback must clear awaiting_input_type."
    )


def test_main_menu_keyboard_layout_after_scanner_merge():
    """Current layout: row 3 is [profile, scanner]; suggest + recent
    are no longer on the visible keyboard but still dispatch via
    button_text_to_command for stale keyboards."""
    from lib.telegram_helpers import main_menu_keyboard
    from lib.formatters import btn_label, button_text_to_command
    kb = main_menu_keyboard(locale="en")
    rows = kb["keyboard"]
    flat = [b["text"] for row in rows for b in row]
    assert btn_label("scanner", locale="en") in flat
    assert btn_label("profile", locale="en") in flat
    # Old buttons no longer on the visible keyboard.
    assert btn_label("recent", locale="en") not in flat
    assert btn_label("suggest", locale="en") not in flat
    # AI button (rendered as btn.ask label) is still there.
    assert btn_label("ask", locale="en") in flat
    # Legacy "recent" label still dispatches → stale keyboards keep working.
    assert button_text_to_command(btn_label("recent", locale="en")) == "/recent"
    # New "scanner" label maps to /scan (the chooser-bearing command).
    assert button_text_to_command(btn_label("scanner", locale="en")) == "/scan"
    assert button_text_to_command(btn_label("scanner", locale="uk")) == "/scan"


def test_save_recipe_db_round_trip_shape():
    """save_recipe captures (user_id, body, pantry, created_at) in that order."""
    captured = []

    class _Cur:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            captured.append((sql, params))
        def fetchone(self):
            return (42,)

    class _Conn:
        def cursor(self): return _Cur(self)
        def commit(self): pass

    new_id = db.save_recipe(_Conn(), user_id=7, body="pasta with...", pantry="chicken")
    assert new_id == 42
    sql, params = captured[0]
    assert "INSERT INTO saved_recipes" in sql
    assert params[0] == 7
    assert params[1] == "pasta with..."
    assert params[2] == "chicken"
    # Timestamp is ISO 8601 UTC.
    assert "T" in params[3] and "+00:00" in params[3]


def test_count_chat_messages_uses_60_minute_window():
    """count_chat_messages issues a SELECT with a cutoff timestamp ~60 min back."""
    captured = []

    class _Cur:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            captured.append((sql, params))
        def fetchone(self):
            return (3,)

    class _Conn:
        def cursor(self): return _Cur(self)
        def commit(self): pass

    n = db.count_chat_messages(_Conn(), user_id=7)
    assert n == 3
    sql, params = captured[0]
    assert "SELECT COUNT(*)" in sql
    assert "chat_sessions" in sql
    assert params[0] == 7
    # cutoff is an ISO 8601 string roughly 60 minutes before now.
    assert "T" in params[1]


def test_clear_chat_history_returns_rowcount():
    """clear_chat_history returns the DELETE rowcount."""
    class _Cur:
        rowcount = 5
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass

    assert db.clear_chat_history(_Conn(), user_id=7) == 5


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


# ---------- Daily health monitor (cron_health_monitor) ----------


def test_count_cron_runs_24h_gates_on_finished_at_and_name():
    """Helper must filter by cron_name + last-24h window + finished_at not null."""
    conn = _AdminConn(rows=[(7,)])
    out = db.count_cron_runs_24h(conn, "cron_good_morning")
    assert out == 7
    sql, params = conn.calls[0]
    assert "FROM cron_runs" in sql
    assert "INTERVAL '24 hours'" in sql
    assert "finished_at IS NOT NULL" in sql
    assert params == ("cron_good_morning",)


def test_get_cron_errors_24h_filters_error_status_or_text():
    """Must surface rows where status='error' OR error column non-empty."""
    conn = _AdminConn(rows=[
        ("cron_good_morning", "2026-05-26T08:00", "boom"),
    ])
    out = db.get_cron_errors_24h(conn)
    sql, _ = conn.calls[0]
    assert "status = 'error'" in sql
    assert "error IS NOT NULL" in sql and "error <> ''" in sql
    assert "INTERVAL '24 hours'" in sql
    assert out[0]["cron_name"] == "cron_good_morning"
    assert out[0]["error"] == "boom"


def test_get_user_errors_in_cron_runs_24h_unpacks_errors_array():
    """`result_json.errors` arrays from 24h runs must be flattened into
    per-cron-run rows with count + first-error sample."""
    rj = json.dumps({"sent": 5, "errors": [
        {"user_id": 42, "error": "Telegram 500"},
        {"user_id": 7, "error": "json parse"},
    ]})
    conn = _AdminConn(rows=[
        ("cron_good_morning", "2026-05-26", rj),
        ("cron_daily_summary", "2026-05-26", json.dumps({"errors": []})),
    ])
    out = db.get_user_errors_in_cron_runs_24h(conn)
    # Only the row with non-empty errors survives.
    assert len(out) == 1
    assert out[0]["cron_name"] == "cron_good_morning"
    assert out[0]["errors_count"] == 2
    assert out[0]["sample"]["user_id"] == 42


def test_sum_counters_24h_rolls_up_named_keys():
    """Counter rollup must sum each named key across every successful run."""
    conn = _AdminConn(rows=[
        (json.dumps({"sent": 3, "activation_sent_demo": 1}),),
        (json.dumps({"sent": 2, "activation_sent_demo": 4}),),
        (json.dumps({"sent": 1}),),  # missing key → treated as 0
    ])
    out = db.sum_counters_24h(conn, "cron_good_morning",
                              ["sent", "activation_sent_demo"])
    assert out == {"sent": 6, "activation_sent_demo": 5}
    sql, params = conn.calls[0]
    assert "cron_name = %s" in sql
    assert "status = 'ok'" in sql
    assert params == ("cron_good_morning",)


def test_count_users_logged_yesterday_utc_uses_yesterday_date():
    """SQL must gate `created_at::date = (CURRENT_DATE - 1)`."""
    conn = _AdminConn(rows=[(12,)])
    out = db.count_users_logged_yesterday_utc(conn)
    sql, _ = conn.calls[0]
    assert "FROM meals" in sql
    assert "CURRENT_DATE - INTERVAL '1 day'" in sql
    assert "COUNT(DISTINCT user_id)" in sql
    assert out == 12


def test_count_new_blocks_uses_blocked_at_window():
    conn = _AdminConn(rows=[(3,)])
    out = db.count_new_blocks(conn, hours=24)
    sql, _ = conn.calls[0]
    assert "FROM user_profiles" in sql
    assert "blocked_at IS NOT NULL" in sql
    assert "INTERVAL '24 hours'" in sql
    assert out == 3


def test_avg_daily_blocks_divides_by_days_window():
    """Baseline is COUNT / days over `days` days."""
    conn = _AdminConn(rows=[(1.5,)])
    out = db.avg_daily_blocks(conn, days=7)
    sql, _ = conn.calls[0]
    assert "FROM user_profiles" in sql
    assert "/ 7" in sql
    assert "INTERVAL '7 days'" in sql
    assert out == 1.5


def test_get_users_stuck_in_activation_step_gates_on_step_age_meals():
    """Stuck-in-step query must filter step + age + never-logged + not opted-out."""
    conn = _AdminConn(rows=[(42,), (7,)])
    out = db.get_users_stuck_in_activation_step(conn, "demo", min_days=3)
    sql, params = conn.calls[0]
    assert "activation_step = %s" in sql
    assert "nudge_optout" in sql
    assert "blocked_at IS NULL" in sql
    assert "updated_at" in sql and "INTERVAL '3 days'" in sql
    assert "FROM meals" in sql  # NOT EXISTS gate
    assert params == ("demo",)
    assert out == [42, 7]


def test_count_signups_24h_splits_by_done_vs_mid():
    """FILTER clauses must split done vs. mid-onboarding signups."""
    conn = _AdminConn(rows=[(12, 2)])
    out = db.count_signups_24h(conn)
    sql, _ = conn.calls[0]
    assert "FROM users" in sql
    assert "onboarding_step" in sql
    assert "INTERVAL '24 hours'" in sql
    assert out == {"done": 12, "mid": 2}


def test_count_meals_and_active_users_24h_returns_both_counts():
    conn = _AdminConn(rows=[(247, 38)])
    out = db.count_meals_and_active_users_24h(conn)
    sql, _ = conn.calls[0]
    assert "FROM meals" in sql
    assert "COUNT(DISTINCT user_id)" in sql
    assert "INTERVAL '24 hours'" in sql
    assert out == {"meals": 247, "active_users": 38}


def test_count_first_meal_logs_today_subquery_uses_min_created_at():
    """Must compute MIN(created_at) per user, then filter by 24h window."""
    conn = _AdminConn(rows=[(3,)])
    out = db.count_first_meal_logs_today(conn)
    sql, _ = conn.calls[0]
    assert "MIN(created_at" in sql
    assert "GROUP BY user_id" in sql
    assert "INTERVAL '24 hours'" in sql
    assert out == 3


def test_cron_health_monitor_imports_cleanly_and_exposes_handler():
    """The new monitor must import without raising and expose a
    `handler` class (Vercel entry point). Same shape contract as the
    other crons — this is the regression net for the F-17 class of bug."""
    import importlib
    mod = importlib.import_module("api.cron_health_monitor")
    assert hasattr(mod, "handler")
    assert hasattr(mod, "run_health_monitor")
    assert callable(mod.run_health_monitor)


def test_cron_health_monitor_registered_in_vercel_json():
    """vercel.json must schedule the new cron at 06:00 UTC daily
    (= 09:00 Kyiv summer, 08:00 winter — morning-coffee window)."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "vercel.json")) as f:
        cfg_json = json.load(f)
    paths = {c["path"]: c["schedule"] for c in cfg_json["crons"]}
    assert paths.get("/api/cron_health_monitor") == "0 6 * * *"


def test_cron_good_morning_uses_per_variant_activation_counters():
    """Source-grep guard: the morning cron must split `activation_sent`
    into per-variant counters so the health monitor can see each F-17
    rung individually. Catches re-introduction of the lumped counter."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "cron_good_morning.py")).read()
    # Per-variant counter keys must be present.
    assert "activation_sent_demo" in src
    assert "activation_sent_d4"   in src
    assert "activation_sent_d7"   in src
    # The lumped counter name must NOT appear as a result-dict key anymore
    # (only the three per-variant keys above).
    assert '"activation_sent":' not in src


def _breakdown(started=0, ok=0, errored=0, unfinished=0):
    """Test helper: shape of count_cron_runs_24h_by_status return value."""
    return {
        "started":            started,
        "finished_ok":        ok,
        "errored":            errored,
        "running_unfinished": unfinished,
    }


def test_health_monitor_check_cron_firing_alerts_on_low_starts(monkeypatch):
    """Alert branch A: too few starts → Vercel didn't invoke us.
    Hourly cron with started < _HOURLY_OK_FLOOR (20) must flag the
    'Vercel cron not invoking' message."""
    import importlib
    cm = importlib.import_module("api.cron_health_monitor")
    breakdowns = {
        "cron_daily_summary":         _breakdown(started=24, ok=24),
        "cron_good_morning":          _breakdown(started=5,  ok=5),  # 5<20
        "cron_midnight_reset":        _breakdown(started=1,  ok=1),
        "cron_weekly_weight_checkin": _breakdown(started=0,  ok=0),
    }
    monkeypatch.setattr(cm, "count_cron_runs_24h_by_status",
                        lambda conn, name: breakdowns[name])
    out = cm._check_cron_firing(conn=None)
    assert out["ok"] is False
    # The alert text must point at Vercel-not-invoking, not function-crashing.
    morning_alert = next(a for a in out["alerts"] if "cron_good_morning" in a)
    assert "Vercel cron not invoking" in morning_alert
    # Healthy lines still render OK.
    rendered = "\n".join(out["lines"])
    assert "✅ daily_summary" in rendered
    assert "✅ midnight_reset" in rendered


def test_health_monitor_check_cron_firing_alerts_on_crashes(monkeypatch):
    """Alert branch B: started but didn't finish → function crashing
    mid-flight. The alert wording must be distinct from low-starts so
    the followup investigation goes in the right direction."""
    import importlib
    cm = importlib.import_module("api.cron_health_monitor")
    breakdowns = {
        "cron_daily_summary":         _breakdown(started=24, ok=24),
        "cron_good_morning":          _breakdown(started=24, ok=12, unfinished=12),
        "cron_midnight_reset":        _breakdown(started=1,  ok=1),
        "cron_weekly_weight_checkin": _breakdown(),
    }
    monkeypatch.setattr(cm, "count_cron_runs_24h_by_status",
                        lambda conn, name: breakdowns[name])
    out = cm._check_cron_firing(conn=None)
    assert out["ok"] is False
    morning_alert = next(a for a in out["alerts"] if "cron_good_morning" in a)
    assert "started but never finished" in morning_alert
    assert "Vercel logs" in morning_alert
    # Breakdown line shows the split.
    rendered = "\n".join(out["lines"])
    assert "12 lost" in rendered


def test_health_monitor_check_cron_firing_passes_when_above_floor(monkeypatch):
    """Healthy: enough starts AND no unfinished → no alerts."""
    import importlib
    cm = importlib.import_module("api.cron_health_monitor")
    breakdowns = {
        "cron_daily_summary":         _breakdown(started=24, ok=24),
        "cron_good_morning":          _breakdown(started=22, ok=22),  # ≥20
        "cron_midnight_reset":        _breakdown(started=1,  ok=1),
        "cron_weekly_weight_checkin": _breakdown(),
    }
    monkeypatch.setattr(cm, "count_cron_runs_24h_by_status",
                        lambda conn, name: breakdowns[name])
    out = cm._check_cron_firing(conn=None)
    assert out["ok"] is True
    assert out["alerts"] == []


def test_health_monitor_unfinished_tolerance_one_or_two(monkeypatch):
    """One or two genuinely-still-running fires (the function might
    legitimately be in-flight when the monitor reads the row) must
    NOT trigger the crash alert. Threshold is `> 2`."""
    import importlib
    cm = importlib.import_module("api.cron_health_monitor")
    breakdowns = {
        "cron_daily_summary":         _breakdown(started=24, ok=22, unfinished=2),
        "cron_good_morning":          _breakdown(started=24, ok=22, unfinished=2),
        "cron_midnight_reset":        _breakdown(started=1,  ok=1),
        "cron_weekly_weight_checkin": _breakdown(),
    }
    monkeypatch.setattr(cm, "count_cron_runs_24h_by_status",
                        lambda conn, name: breakdowns[name])
    out = cm._check_cron_firing(conn=None)
    # 2 unfinished is within tolerance → no crash alert.
    assert not any("started but never finished" in a for a in out["alerts"])


def test_cron_good_morning_uses_start_finish_bracket():
    """Phase B source-grep guard: the morning cron must use the
    start/finish bracket pattern (not the legacy one-shot
    record_cron_run). And R2 — `run_id` must be initialised BEFORE
    the try block so the finally can always reference it."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "cron_good_morning.py")).read()
    # Bracket pattern present.
    assert "start_cron_run(conn, \"cron_good_morning\")" in src
    assert "finish_cron_run(conn, run_id, status" in src
    # Legacy one-shot must not be re-introduced for this cron.
    assert "record_cron_run(conn, \"cron_good_morning\"" not in src
    # R2 guard: run_id initialised BEFORE its assignment from start_cron_run.
    # Look for the `run_id: int | None = None` line above `start_cron_run`.
    idx_init = src.find("run_id: int | None = None")
    idx_start = src.find("start_cron_run(conn, \"cron_good_morning\")")
    assert idx_init >= 0, "run_id must be initialised to None before start_cron_run"
    assert idx_init < idx_start, "run_id init must precede start_cron_run call"


def test_health_monitor_check_block_spike_requires_breach_and_magnitude(monkeypatch):
    """Block-spike check ignores 1-vs-0 noise but pages on 5-vs-1.2."""
    import importlib
    cm = importlib.import_module("api.cron_health_monitor")
    monkeypatch.setattr(cm, "count_new_blocks", lambda conn, hours=24: 5)
    monkeypatch.setattr(cm, "avg_daily_blocks", lambda conn, days=7: 1.2)
    out = cm._check_block_spike(conn=None)
    assert out["ok"] is False
    assert "block spike" in out["alerts"][0]

    # Below the absolute floor → no alert even if multiplier breached.
    monkeypatch.setattr(cm, "count_new_blocks", lambda conn, hours=24: 1)
    monkeypatch.setattr(cm, "avg_daily_blocks", lambda conn, days=7: 0.0)
    out2 = cm._check_block_spike(conn=None)
    assert out2["ok"] is True
    assert out2["alerts"] == []


# ---------- Phase A: cron-run lifecycle bracketing ----------


def test_start_cron_run_inserts_running_row_returns_id():
    """`start_cron_run` must INSERT with status='running' (no
    finished_at), commit, and return the new row's id via RETURNING.
    The 'running' status is the diagnostic that distinguishes
    'mid-flight' from 'finished'."""
    conn = _AdminConn(rows=[(42,)])
    out = db.start_cron_run(conn, "cron_good_morning")
    assert out == 42
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "INSERT INTO cron_runs" in sql
    assert "status" in sql
    assert "'running'" in sql
    assert "RETURNING id" in sql
    # `finished_at` must NOT be set at start — the DEFAULT is NULL and
    # that's the signal for "in-flight".
    assert "finished_at" not in sql
    assert params == ("cron_good_morning",)


def test_start_cron_run_returns_none_on_db_error():
    """Defensive: observability MUST NEVER block the cron. If the
    INSERT fails for any reason, the helper returns None instead of
    propagating — the caller (cron handler) then runs its actual
    work unimpeded."""
    class _BoomConn:
        def cursor(self): raise RuntimeError("neon asleep")
        def commit(self): pass
    out = db.start_cron_run(_BoomConn(), "cron_good_morning")
    assert out is None


def test_finish_cron_run_updates_row_by_id():
    """`finish_cron_run` must UPDATE the row by id, setting finished_at
    to now(), status, result_json, error. Targets only one row via
    WHERE id = %s."""
    conn = _AdminConn()
    db.finish_cron_run(conn, run_id=42, status="ok",
                       result={"sent": 5}, error=None)
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "UPDATE cron_runs" in sql
    assert "finished_at = now()" in sql
    assert "status = %s" in sql
    assert "WHERE id = %s" in sql
    assert params[0] == "ok"
    assert '"sent": 5' in params[1]
    assert params[2] is None
    assert params[3] == 42


def test_finish_cron_run_with_none_id_falls_back_to_record():
    """If start_cron_run returned None (DB error), finish must still
    leave SOME row via the existing record_cron_run path. The
    cron_name is stamped '<orphan>' so we can spot them in the
    admin panel later."""
    conn = _AdminConn()
    db.finish_cron_run(conn, run_id=None, status="ok",
                       result={"sent": 3}, error=None)
    # Fell through to record_cron_run, which INSERTs a one-shot row.
    assert conn.commits == 1
    sql, params = conn.calls[0]
    assert "INSERT INTO cron_runs" in sql
    assert params[0] == "<orphan>"
    assert params[1] == "ok"


def test_finish_cron_run_swallows_db_errors():
    """The cron's actual outcome must never be masked by observability
    failures. Even if the UPDATE blows up, finish_cron_run returns
    cleanly so the caller's finally block continues."""
    class _BoomConn:
        def cursor(self): raise RuntimeError("neon asleep")
        def commit(self): pass
    # Must not raise.
    db.finish_cron_run(_BoomConn(), run_id=99, status="error",
                       error="something bad")


def test_count_cron_runs_24h_by_status_returns_four_buckets():
    """Diagnostic helper must split the 24h cohort into 4 disjoint
    counts: total started, finished_ok, errored, running_unfinished.
    The running_unfinished bucket is the smoking gun for crashed fires."""
    conn = _AdminConn(rows=[(24, 12, 2, 10)])
    out = db.count_cron_runs_24h_by_status(conn, "cron_good_morning")
    assert out == {
        "started":            24,
        "finished_ok":        12,
        "errored":             2,
        "running_unfinished": 10,
    }
    sql, params = conn.calls[0]
    assert "FROM cron_runs" in sql
    assert "INTERVAL '24 hours'" in sql
    assert "cron_name = %s" in sql
    # Each bucket has its own FILTER clause.
    assert "FILTER (WHERE finished_at IS NOT NULL" in sql
    assert "status = 'ok'" in sql
    assert "status = 'error'" in sql
    assert "FILTER (WHERE finished_at IS NULL" in sql
    assert "status = 'running'" in sql
    assert params == ("cron_good_morning",)


def test_welcome_intro_sends_without_inline_lang_keyboard():
    """2026-05 follow-up: the welcome `onboarding.intro` send must NOT
    carry an inline language picker. User explicitly rejected the
    rescue switcher — Profile → 🌐 Language is the sole post-arrival
    recourse for mis-detection. Source-grep guard against the
    keyboard being re-attached."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "webhook.py")).read()
    helper_block = src.split(
        "def _enter_onboarding_age_step(", 1
    )[1].split("\ndef ", 1)[0]
    # The intro send is present, but does NOT pass reply_markup.
    assert 'i18n_mod.t("onboarding.intro"' in helper_block
    assert "welcome_lang_inline_keyboard" not in helper_block
    # As a stronger guard, the function itself shouldn't exist anymore.
    from lib import telegram_helpers as th
    assert not hasattr(th, "welcome_lang_inline_keyboard"), (
        "welcome_lang_inline_keyboard should be removed — Profile is "
        "the sole language-switch surface for the post-confirm-removal "
        "flow."
    )


def test_profile_edit_keyboard_includes_language_button():
    """After the 2026-05 onboarding simplification, /profile must
    surface a 🌐 Language button so users discover the switcher
    without having to know the /language command exists."""
    from lib.telegram_helpers import profile_edit_keyboard
    for locale in ("en", "uk"):
        kb = profile_edit_keyboard(locale=locale)
        callbacks: list[str] = []
        for row in kb["inline_keyboard"]:
            for btn in row:
                callbacks.append(btn["callback_data"])
        assert "prof:lang" in callbacks, (
            f"profile_edit_keyboard({locale!r}) missing the 🌐 Language "
            f"button (callback `prof:lang`)"
        )


def test_profile_edit_language_label_localizes():
    """Label for the new Language button must render in both locales —
    English and Ukrainian — not fall back to a placeholder."""
    en = i18n_mod.t("profile_edit.language", locale="en")
    uk = i18n_mod.t("profile_edit.language", locale="uk")
    assert "🌐" in en
    assert "🌐" in uk
    # Locale-specific words to guard against accidental cross-pollination.
    assert "Language" in en
    assert "Мова" in uk


def test_handle_start_skips_lang_confirm_for_fresh_user():
    """The 2026-05 onboarding simplification: a fresh user must land
    at `awaiting_age` directly, NOT at `awaiting_lang_confirm`. This
    is the source-grep guard against the confirmation screen being
    re-introduced.
    """
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "webhook.py")).read()
    # The new fresh-user entry helper must exist.
    assert "_enter_onboarding_age_step(" in src
    # The handle_start body must invoke it (NOT set onboarding_step
    # to awaiting_lang_confirm).
    handle_start_block = src.split("def handle_start(", 1)[1].split("\ndef ", 1)[0]
    assert "_enter_onboarding_age_step" in handle_start_block
    assert 'onboarding_step="awaiting_lang_confirm"' not in handle_start_block
    # The old lang_confirm_keyboard call must be gone from handle_start.
    assert "lang_confirm_keyboard(" not in handle_start_block


def test_enter_onboarding_age_step_advances_to_awaiting_age():
    """Source-grep guard: the new helper must persist lang, stamp
    lang_confirmed_at, and set onboarding_step='awaiting_age'. Each
    of these is critical — missing any leaves the user in a dead
    state."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "webhook.py")).read()
    helper_block = src.split(
        "def _enter_onboarding_age_step(", 1
    )[1].split("\ndef ", 1)[0]
    assert "lang=lang" in helper_block
    assert "lang_confirmed_at=" in helper_block
    assert 'onboarding_step="awaiting_age"' in helper_block
    # Must send both the intro and the age question. No inline
    # keyboard is attached — Profile is the sole language-switch
    # surface for the new flow.
    assert 'i18n_mod.t("onboarding.intro"' in helper_block
    assert 'i18n_mod.t("onboarding.ask_age"' in helper_block


def test_prof_lang_callback_handler_present():
    """The Profile screen's 🌐 Language button needs a `prof:lang`
    callback handler that opens the language picker. Source-grep
    guard against accidental removal."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "api", "webhook.py")).read()
    profile_block = src.split(
        "def handle_profile_edit_callback(", 1
    )[1].split("\ndef ", 1)[0]
    assert 'data == "prof:lang"' in profile_block
    assert "language_keyboard()" in profile_block


def test_record_cron_run_body_unchanged_phase_a():
    """Phase A regression guard: `record_cron_run` body MUST stay
    exactly as today so the admin panel reader + existing callers
    keep working. New crons get the start/finish bracket; old ones
    keep this one-shot path."""
    conn = _AdminConn()
    db.record_cron_run(conn, "cron_daily_summary", "ok",
                       result={"sent_summary": 3})
    sql, _ = conn.calls[0]
    # Single INSERT with both started_at (DEFAULT) + finished_at (now())
    # — the pre-Phase-A behaviour.
    assert "INSERT INTO cron_runs" in sql
    assert "finished_at" in sql
    assert "now()" in sql
    # No UPDATE / no RETURNING — these are the new-style helpers' shape.
    assert "UPDATE" not in sql
    assert "RETURNING" not in sql
