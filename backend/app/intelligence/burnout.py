"""
Burnout & context-switch overload detection.

Computes risk signals from clock sessions, break sessions, and browser
activity over a configurable rolling window. Output drives:

    • BurnoutMeter on the cinematic admin view (per employee)
    • Team-pulse aggregate ("3 of 12 engineers at elevated risk")
    • The `burnout_risk` field on DailyIntelligenceSummary

Risk is intentionally explainable — each signal carries a reason and a
contribution weight so an admin can drill in and see *why* someone is
flagged, not just "the AI said so". No LLM call here; this is pure data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, time, timezone

# Analytics read in ORG-LOCAL time so they align with employees' clock hours.
# The tracker stores UTC; app._tz resolves each organization's own zone from
# OrgSettings.default_timezone. There is no module-level offset any more.
from typing import List, Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from database import models

# Thresholds — tuned for typical knowledge-work day. Each is the point
# at which the signal contributes its full weight; values below scale
# linearly from 0 → weight.
# EXPECTED_DAILY_HOURS was removed: it was a fifth copy of "how long is a work
# day". Burnout now reads the employee's effective expected hours through the
# canonical resolver, so a 6-hour contract is not scored as under-working.
#
# The rest are BURNOUT MODEL constants, not workforce policy: they describe when
# a wellbeing signal starts contributing, not what any organization requires of
# its staff. They stay module-level and are documented as such. The night window
# is superseded per employee by their own shift when one is configured — a night
# worker is not "burning the midnight oil" for working their own shift.
LONG_DAY_HOURS               = 10        # above this → "overwork" signal
NO_BREAK_HOURS               = 4         # uninterrupted clocked time
LATE_NIGHT_HOUR_LOCAL        = 22        # fallback night window start
EARLY_MORNING_HOUR_LOCAL     = 6         # fallback night window end
SWITCH_OVERLOAD_PER_HOUR     = 30        # tab switches/hr above this = overload
WEEKEND_WORK_HOURS_THRESHOLD = 2         # non-working-day hours above this signal

# Weight contributions (sum ~1.0). Risk score is the weighted sum,
# bucketed into low / moderate / high.
W_LONG_DAYS        = 0.30
W_NO_BREAKS        = 0.20
W_LATE_NIGHT       = 0.20
W_SWITCH_OVERLOAD  = 0.15
W_WEEKEND          = 0.15


@dataclass
class Signal:
    kind: str
    severity: float            # 0..1
    reason: str
    weight: float
    contribution: float = 0.0

    def as_dict(self) -> dict:
        return {
            "kind":         self.kind,
            "severity":     round(self.severity, 3),
            "reason":       self.reason,
            "weight":       self.weight,
            "contribution": round(self.contribution, 3),
        }


@dataclass
class BurnoutReport:
    user_id: int
    period_days: int
    risk_score: float            # 0..1
    risk_band: str               # 'low' | 'moderate' | 'high'
    signals: List[Signal] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "user_id":     self.user_id,
            "period_days": self.period_days,
            "risk_score":  round(self.risk_score, 3),
            "risk_band":   self.risk_band,
            "signals":     [s.as_dict() for s in self.signals],
            "metrics":     self.metrics,
        }


def compute_burnout(
    db: Session,
    user_id: int,
    period_days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> BurnoutReport:
    now = now or datetime.now(timezone.utc)
    period_start = now - timedelta(days=period_days)

    # ── Schedule source ──────────────────────────────────────────────────────
    # Engine ON: "weekend" means "not a working day for THIS employee" and the
    # night window comes from their shift, so a 22:00-06:00 night worker is no
    # longer scored as burnt out for working their own shift. Engine OFF: the
    # module constants, unchanged.
    # Resolved ONCE per call, not per session: working days and the shift window
    # are employee-level, and this loops over a week of sessions.
    _sched = None
    _worker = None
    try:
        _worker = db.query(models.User).filter(models.User.id == user_id).first()
        if _worker is not None:
            from app.schedule_policy.resolver import resolve_work_schedule
            _sched = resolve_work_schedule(db, _worker, now.date())
    except Exception:
        _sched = None

    _org_id = getattr(_worker, "organization_id", None)

    def _local(dt):
        """A stored UTC instant on the employee's own wall clock."""
        if dt is None:
            return None
        try:
            from app import _tz as _tzmod
            return _tzmod.to_local(db, _org_id, dt)
        except Exception:
            return dt

    def _is_non_working(day) -> bool:
        """This employee's own working-day pattern — never a Sat/Sun guess.

        `day` must already be org-local: it used to be handed the raw UTC
        clock_in_time, so a Monday 02:00 shift in Asia read as Sunday and was
        scored as weekend work.
        """
        if _sched is not None:
            from app.schedule_policy import keys as _K
            return _K.WEEKDAY_NAMES[day.weekday()] not in _sched.working_days
        return False

    def _is_night_hour(hour: int) -> bool:
        """Engine ON with a shift configured: hours OUTSIDE the shift window count
        as late-night, so working your own night shift is not a burnout signal.
        Otherwise the fixed 22:00-06:00 window."""
        if _sched is not None and _sched.start_local and _sched.end_local:
            sh, eh = _sched.start_local.hour, _sched.end_local.hour
            inside = (sh <= hour < eh) if sh < eh else (hour >= sh or hour < eh)
            return not inside
        return hour >= LATE_NIGHT_HOUR_LOCAL or hour < EARLY_MORNING_HOUR_LOCAL

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_in_time >= period_start,
        )
        .order_by(models.ClockSession.clock_in_time.asc())
        .all()
    )

    breaks = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.user_id == user_id,
            models.BreakSession.break_start >= period_start,
        )
        .all()
    )

    tab_events = (
        db.query(models.BrowserActivity.started_at, models.BrowserActivity.duration_sec)
        .filter(
            models.BrowserActivity.user_id == user_id,
            models.BrowserActivity.started_at >= period_start,
        )
        .all()
    )

    # ─── derive daily totals ──────────────────────────────────────────
    daily_hours: dict = {}
    weekend_hours: float = 0.0
    late_night_minutes: int = 0
    long_days = 0
    no_break_hits = 0

    breaks_by_session = {}
    for b in breaks:
        if not b.break_end:
            continue
        bs = breaks_by_session.setdefault(b.session_id, [])
        bs.append((b.break_start, b.break_end))

    for s in sessions:
        if not s.clock_out_time:
            continue
        # Wall-clock analysis (which day, which hour) happens in the
        # organization's zone; durations are zone-independent.
        start = _local(s.clock_in_time)
        end = _local(s.clock_out_time)
        total_sec = max(0, int((end - start).total_seconds()))
        break_sec = sum(int((be - bs).total_seconds()) for bs, be in breaks_by_session.get(s.id, []))
        worked_sec = max(0, total_sec - break_sec)
        hours = worked_sec / 3600.0
        day_key = start.date().isoformat()
        daily_hours[day_key] = daily_hours.get(day_key, 0.0) + hours

        # Non-working-day hours (Sat/Sun when the engine is off).
        if _is_non_working(start):
            weekend_hours += hours

        # Late-night / early-morning portion (rough — count the wall hours
        # the session was active during the late-night bucket)
        cursor = start
        while cursor < end:
            h = cursor.hour
            if _is_night_hour(h):
                late_night_minutes += 1
            cursor += timedelta(minutes=1)

        # No-break stretch — was there a single span > NO_BREAK_HOURS?
        if not breaks_by_session.get(s.id):
            if total_sec >= NO_BREAK_HOURS * 3600:
                no_break_hits += 1
        else:
            # Find the longest span without a break inside the session.
            spans = []
            prev_end = start
            for bs_, be_ in sorted(breaks_by_session[s.id]):
                spans.append((bs_ - prev_end).total_seconds())
                prev_end = be_
            spans.append((end - prev_end).total_seconds())
            if max(spans) >= NO_BREAK_HOURS * 3600:
                no_break_hits += 1

    for hours in daily_hours.values():
        if hours >= LONG_DAY_HOURS:
            long_days += 1

    # ─── switch overload ─────────────────────────────────────────────
    # Tab switches/hour averaged across days with activity.
    total_switches = max(0, len(tab_events) - 1)
    total_active_sec = sum(int(d or 0) for _, d in tab_events)
    switch_rate_per_hr = (total_switches / total_active_sec * 3600) if total_active_sec else 0

    # ─── build signals ───────────────────────────────────────────────
    signals: List[Signal] = []

    if long_days > 0:
        sev = min(1.0, long_days / max(1, period_days * 0.5))
        signals.append(Signal(
            kind="long_days",
            severity=sev,
            reason=f"{long_days} day(s) over {LONG_DAY_HOURS}h in the last {period_days}d",
            weight=W_LONG_DAYS,
            contribution=sev * W_LONG_DAYS,
        ))

    if no_break_hits > 0:
        sev = min(1.0, no_break_hits / max(1, period_days * 0.5))
        signals.append(Signal(
            kind="no_breaks",
            severity=sev,
            reason=f"{no_break_hits} session(s) ran >{NO_BREAK_HOURS}h without a break",
            weight=W_NO_BREAKS,
            contribution=sev * W_NO_BREAKS,
        ))

    if late_night_minutes >= 60:
        # 1h of late-night work in the window → severity 1.0
        sev = min(1.0, late_night_minutes / (period_days * 60))
        signals.append(Signal(
            kind="late_night",
            severity=sev,
            reason=f"{late_night_minutes // 60}h {late_night_minutes % 60}m of work outside 06:00–22:00",
            weight=W_LATE_NIGHT,
            contribution=sev * W_LATE_NIGHT,
        ))

    if switch_rate_per_hr >= SWITCH_OVERLOAD_PER_HOUR:
        sev = min(1.0, (switch_rate_per_hr - SWITCH_OVERLOAD_PER_HOUR) / SWITCH_OVERLOAD_PER_HOUR + 1.0)
        signals.append(Signal(
            kind="context_switch_overload",
            severity=sev,
            reason=f"{int(switch_rate_per_hr)} tab switches/hr — high multitasking",
            weight=W_SWITCH_OVERLOAD,
            contribution=sev * W_SWITCH_OVERLOAD,
        ))

    if weekend_hours >= WEEKEND_WORK_HOURS_THRESHOLD:
        sev = min(1.0, weekend_hours / (WEEKEND_WORK_HOURS_THRESHOLD * 3))
        signals.append(Signal(
            kind="weekend_work",
            severity=sev,
            reason=f"{weekend_hours:.1f}h of weekend work",
            weight=W_WEEKEND,
            contribution=sev * W_WEEKEND,
        ))

    risk_score = sum(s.contribution for s in signals)
    risk_score = max(0.0, min(1.0, risk_score))
    if risk_score >= 0.6:
        band = "high"
    elif risk_score >= 0.3:
        band = "moderate"
    else:
        band = "low"

    return BurnoutReport(
        user_id=user_id,
        period_days=period_days,
        risk_score=risk_score,
        risk_band=band,
        signals=signals,
        metrics={
            "long_days":            long_days,
            "no_break_hits":        no_break_hits,
            "late_night_minutes":   late_night_minutes,
            "weekend_hours":        round(weekend_hours, 1),
            "switch_rate_per_hr":   round(switch_rate_per_hr, 1),
            "total_clocked_hours":  round(sum(daily_hours.values()), 1),
            "days_active":          len(daily_hours),
        },
    )


def context_switch_pulse(
    db: Session,
    user_id: int,
    day_start: datetime,
    day_end: datetime,
) -> dict:
    """Hourly tab-switch counts for the FocusTimeline pulse overlay.

    Buckets are in the ORGANIZATION's local hours so the graph lines up with
    the employee's actual clock times, not UTC. The incoming day_start marks
    the target date; we read that full local day.
    """
    from app import _tz as _tzmod
    _org_id = db.query(models.User.organization_id).filter(
        models.User.id == user_id).scalar()
    _zone = _tzmod.org_timezone(db, _org_id)
    win_start, win_end = _tzmod.local_day_bounds(db, _org_id, day_start.date())
    rows = (
        db.query(models.BrowserActivity.started_at)
        .filter(
            models.BrowserActivity.user_id == user_id,
            models.BrowserActivity.started_at >= win_start,
            models.BrowserActivity.started_at < win_end,
        )
        .order_by(models.BrowserActivity.started_at.asc())
        .all()
    )
    hourly = [0] * 24
    for (ts,) in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        hourly[ts.astimezone(_zone).hour] += 1
    total = sum(hourly)
    peak = max(hourly) if hourly else 0
    peak_hour = hourly.index(peak) if peak else None
    return {
        "hourly":     hourly,
        "total":      total,
        "peak_count": peak,
        "peak_hour":  peak_hour,
        "overload":   total > SWITCH_OVERLOAD_PER_HOUR * 8,
    }
