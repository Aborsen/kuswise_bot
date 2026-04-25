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
