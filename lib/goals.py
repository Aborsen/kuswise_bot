"""Goals dashboard helpers (F-5).

Pure functions for projecting weeks-to-goal from current weight, target
weight, and a weekly weight-change rate. Also classifies actual weekly
progress against the user's target rate as ahead / on-track / behind.

Weekly-delta sign convention:
    - "lose" goal → weekly_delta_kg should be negative, e.g. -0.5
    - "gain" goal → weekly_delta_kg should be positive, e.g. +0.3
    - "maintain"  → weekly_delta_kg should be ~0; projection is undefined
                    (they're already there).

The math is intentionally simple: linear projection ``weeks = (current −
target) / |delta|``, with edge-case guards for zero/wrong-direction
deltas. We don't account for acceleration / plateaus — the goal is a
fair projection, not a forecast.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


# Defensive bounds — outside this range we refuse to compute a projection
# rather than giving the user a nonsense answer.
_MIN_WEEKLY_DELTA = 0.05  # kg/week — slower than this is treated as zero
_MAX_WEEKLY_DELTA = 2.0   # kg/week — faster is medically dubious; cap


def default_weekly_delta(goal: Optional[str]) -> float:
    """Fallback weekly_delta_kg when the user hasn't set one.
    Conservative defaults that won't mislead anyone medically."""
    if goal == "lose":
        return -0.5
    if goal == "gain":
        return 0.3
    return 0.0


def effective_weekly_delta(profile: Optional[dict]) -> float:
    """Return the user's weekly_delta_kg, falling back to a goal-based default.

    Empty/None/zero overrides → default. NULL DB values arrive as None.
    """
    if not profile:
        return 0.0
    raw = profile.get("weekly_delta_kg")
    if raw is None or raw == "":
        return default_weekly_delta(profile.get("goal"))
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default_weekly_delta(profile.get("goal"))
    if abs(v) < _MIN_WEEKLY_DELTA:
        return 0.0
    # Clamp implausible values without silently lying to the user.
    if v > _MAX_WEEKLY_DELTA:
        return _MAX_WEEKLY_DELTA
    if v < -_MAX_WEEKLY_DELTA:
        return -_MAX_WEEKLY_DELTA
    return v


@dataclass
class Projection:
    """Result of compute_projection.

    ``weeks_to_goal`` and ``projected_date`` are None when projection isn't
    meaningful (no target set, already past target, delta is zero, or delta
    points the wrong way). ``reason`` carries a machine-readable explainer
    so callers can localize the message at the surface layer.
    """
    current_kg:        Optional[float]
    target_kg:         Optional[float]
    weekly_delta_kg:   float
    weeks_to_goal:     Optional[float]
    projected_date:    Optional[date]
    reason:            str  # one of: ok, no_current, no_target, at_target,
                            # zero_delta, wrong_direction


def compute_projection(
    current_kg:    Optional[float],
    target_kg:     Optional[float],
    weekly_delta_kg: float,
    today:         Optional[date] = None,
) -> Projection:
    """Linear weeks-to-goal projection.

    Returns a :class:`Projection`. ``weeks_to_goal`` rounds to one decimal;
    callers can render as "16.0 weeks", "≈ 4 months", etc.
    """
    if today is None:
        today = date.today()

    if current_kg is None:
        return Projection(current_kg, target_kg, weekly_delta_kg, None, None, "no_current")
    if target_kg is None:
        return Projection(current_kg, target_kg, weekly_delta_kg, None, None, "no_target")

    diff = float(target_kg) - float(current_kg)  # positive = need to gain

    if abs(diff) < 0.1:  # within 100g — treat as at-target
        return Projection(current_kg, target_kg, weekly_delta_kg, 0.0, today, "at_target")

    if abs(weekly_delta_kg) < _MIN_WEEKLY_DELTA:
        return Projection(current_kg, target_kg, weekly_delta_kg, None, None, "zero_delta")

    # Wrong-direction guard: e.g. user wants to lose but set +0.5/wk.
    if (diff > 0 and weekly_delta_kg < 0) or (diff < 0 and weekly_delta_kg > 0):
        return Projection(current_kg, target_kg, weekly_delta_kg, None, None, "wrong_direction")

    weeks = abs(diff) / abs(weekly_delta_kg)
    projected = today + timedelta(days=int(round(weeks * 7)))
    return Projection(
        current_kg=current_kg,
        target_kg=target_kg,
        weekly_delta_kg=weekly_delta_kg,
        weeks_to_goal=round(weeks, 1),
        projected_date=projected,
        reason="ok",
    )


def projection_for_profile(profile: Optional[dict], today: Optional[date] = None) -> Projection:
    """Convenience: pull current/target/delta from a profile dict."""
    if not profile:
        return Projection(None, None, 0.0, None, None, "no_current")
    return compute_projection(
        current_kg=profile.get("weight_kg"),
        target_kg=profile.get("target_weight_kg"),
        weekly_delta_kg=effective_weekly_delta(profile),
        today=today,
    )


# ---------- Adherence classification ----------

def classify_actual_vs_target(
    actual_weekly_delta: float,
    target_weekly_delta: float,
    tolerance: float = 0.25,
) -> str:
    """Compare actual weekly weight change against the target rate.

    Returns ``"ahead"``, ``"on_track"``, or ``"behind"``.

    *Ahead* means actual progress is faster than target *toward the goal*
    (more negative for losing, more positive for gaining).
    *Behind* means actual progress is slower (or wrong-direction) versus target.
    *On-track* means within ``tolerance`` (kg/week) of target.

    For ``target_weekly_delta == 0`` (maintain) we just check absolute drift:
    drift > tolerance → behind, else on_track. There's no concept of "ahead"
    when maintaining.
    """
    diff = actual_weekly_delta - target_weekly_delta

    if abs(target_weekly_delta) < _MIN_WEEKLY_DELTA:
        # Maintenance: any sizeable drift is "behind" (the target).
        if abs(actual_weekly_delta) <= tolerance:
            return "on_track"
        return "behind"

    if abs(diff) <= tolerance:
        return "on_track"

    # Goal direction: target_weekly_delta sign tells us which way is "toward".
    if target_weekly_delta < 0:
        # Losing: actual more negative than target = ahead, less negative = behind.
        return "ahead" if actual_weekly_delta < target_weekly_delta else "behind"
    # Gaining: actual more positive than target = ahead.
    return "ahead" if actual_weekly_delta > target_weekly_delta else "behind"


def actual_weekly_delta(weight_history: list[dict], window_weeks: int = 4) -> Optional[float]:
    """Compute actual weekly weight change from recent ``weight_history`` rows.

    ``weight_history`` is the list produced by ``lib.database.get_weight_history``
    (most recent first). We take the oldest and newest weights within the
    ``window_weeks`` window and divide the delta by the elapsed weeks.

    Returns None if there's < 2 data points or the time span is too short.
    """
    if not weight_history or len(weight_history) < 2:
        return None

    # `recorded_at` is a TIMESTAMPTZ — psycopg returns datetimes; we expect
    # something with .date() or that's already a date.
    def _as_date(row) -> Optional[date]:
        ts = row.get("recorded_at") or row.get("created_at")
        if ts is None:
            return None
        if hasattr(ts, "date"):
            try:
                return ts.date()
            except Exception:
                return None
        if isinstance(ts, date):
            return ts
        if isinstance(ts, str):
            try:
                # Tolerate both "YYYY-MM-DD" and ISO timestamps.
                return date.fromisoformat(ts[:10])
            except ValueError:
                return None
        return None

    rows = [r for r in weight_history if _as_date(r) is not None and r.get("weight_kg") is not None]
    if len(rows) < 2:
        return None

    # Sort newest-first, then oldest-first within the window.
    rows.sort(key=lambda r: _as_date(r), reverse=True)
    newest = rows[0]
    cutoff = _as_date(newest) - timedelta(weeks=window_weeks)
    in_window = [r for r in rows if _as_date(r) >= cutoff]
    if len(in_window) < 2:
        in_window = rows[:2]  # fall back to whatever 2 data points we have

    oldest = in_window[-1]
    span_days = (_as_date(newest) - _as_date(oldest)).days
    if span_days < 5:  # need at least ~half a week of data
        return None

    delta_kg = float(newest["weight_kg"]) - float(oldest["weight_kg"])
    return delta_kg / (span_days / 7.0)
