"""
The ECO v2 shadow replay harness — §44.1's P0 exit criterion.
=============================================================

    P0 exit: "All golden vectors pass; replay harness clean for 14 days on
              shadow data."

The golden vectors pass. This module is the other half: a deterministic runner
that replays a complete 14-day observation window and reports, per date and per
Worker, whether the authoritative artifacts came out identical.

WHAT THIS IS NOT
----------------
It is NOT the §44.2 shadow period. Running it proves the harness can DETECT
divergence in shadow data; it does not observe two real workweeks, and its
verdict is deliberately spelled ``HARNESS_CLEAN`` rather than anything that
could be mistaken for the operational gate. ``SHADOW_GATE_SATISFIED`` is not a
value this module can produce.

GV-25 IS NOT THIS EITHER
------------------------
``test_gv25_execution.py`` replays ONE day's three operations and proves the
identity mechanisms hold. That is a vector. This is a WINDOW: fourteen dated
days across two complete workweeks, several Workers, working and non-working
days, shortfall and no shortfall, a crossing week and a non-crossing week, a
deferred day and an unresolved requirement — replayed whole and diffed.

ARCHITECTURE: TEST-ONLY, DRIVER-DRIVEN
--------------------------------------
No new production subsystem, and none is needed. ``run_eco_calculation_pass``
is already the authorized invocation of the chain, and "replay" is simply
invoking it again over unchanged input. So the harness:

* invokes the DRIVER — it re-derives nothing and calculates nothing;
* SNAPSHOTS the authoritative artifacts through the identities those artifacts
  already carry (``content_hash``, the §21.2 threshold natural key, the
  six-field notification identity, the weekly provenance block);
* DIFFS two snapshots and classifies any difference.

It invents no second source of truth. Every value it compares was produced by
the component that owns it.

WHY THE INPUTS ARE SNAPSHOTTED TOO
-----------------------------------
A downstream difference only means "non-deterministic" if the inputs did NOT
change. So the input state is captured alongside, and a difference there is
reported as ``INPUT_DIVERGENCE`` — a defect in the harness or the fixture,
never in the engine. Without it a drifting fixture would read as an engine
fault, which is the most expensive kind of false positive.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import sqlalchemy as sa

#: Two complete Monday-start workweeks. The producer refuses a day whose
#: organization-local day has not ended (D18-F3 gate 1), so the window is
#: deliberately in the past; a window ending today would refuse its own last
#: day and the harness would be measuring the gate, not determinism.
WINDOW_START = date(2026, 9, 7)      # Monday
WINDOW_DAYS = 14                     # ...through Sunday 2026-09-20
WORKWEEK_KEYS = ("2026-09-07", "2026-09-14")


def window_dates(start: date = WINDOW_START,
                 days: int = WINDOW_DAYS) -> Tuple[date, ...]:
    """The fourteen dated days, in order. One calendar, no gaps."""
    return tuple(start + timedelta(days=offset) for offset in range(days))


# ═════════════════════════════════════════════════════════════════════════════
# Divergence taxonomy
# ═════════════════════════════════════════════════════════════════════════════
class DivergenceKind(str, enum.Enum):
    """WHY two replays differ. Never a bare inequality.

    The kind is what makes a failure actionable: a content difference on stable
    identity is an engine determinism fault, while the same difference beside a
    changed ledger version is a correction that was supposed to happen.
    """

    IDENTITY_DIVERGENCE = "IDENTITY_DIVERGENCE"
    """A version, generation or key moved when the input did not."""

    CONTENT_DIVERGENCE = "CONTENT_DIVERGENCE"
    """``content_hash`` differs — the computed result is not reproducible."""

    STATE_DIVERGENCE = "STATE_DIVERGENCE"
    """A day or week resolved to a different state."""

    PROVENANCE_DIVERGENCE = "PROVENANCE_DIVERGENCE"
    """A policy, config, leave or classification reference moved."""

    DUPLICATE_ARTIFACT = "DUPLICATE_ARTIFACT"
    """Replay created a second artifact under one identity."""

    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    """An artifact present in the first pass is absent from the second."""

    UNEXPECTED_ARTIFACT = "UNEXPECTED_ARTIFACT"
    """An artifact appeared in the second pass that the first did not have."""

    THRESHOLD_DIVERGENCE = "THRESHOLD_DIVERGENCE"
    """A week crossed, withdrew or re-crossed differently."""

    NOTIFICATION_DIVERGENCE = "NOTIFICATION_DIVERGENCE"
    """A durable notification record differs, or a second was written."""

    INPUT_DIVERGENCE = "INPUT_DIVERGENCE"
    """The SOURCE state changed between passes. Not an engine fault — the
    replay was not a replay, and every downstream difference is explained."""


@dataclass(frozen=True)
class Divergence:
    """One classified difference. PII-free: identifiers and values only."""

    kind: DivergenceKind
    artifact: str
    key: str
    field_name: Optional[str] = None
    first: Optional[Any] = None
    second: Optional[Any] = None

    def as_row(self) -> Mapping[str, Any]:
        return {
            "kind": self.kind.value, "artifact": self.artifact,
            "key": self.key, "field": self.field_name,
            "first": self.first, "second": self.second,
        }


# ═════════════════════════════════════════════════════════════════════════════
# What a snapshot is
# ═════════════════════════════════════════════════════════════════════════════
#: The day fields the replay contract compares. Split into the three groups the
#: taxonomy distinguishes, so a difference classifies itself.
DAY_IDENTITY_FIELDS = ("ledger_version", "evaluation_generation")
DAY_PROVENANCE_FIELDS = (
    "policy_version", "config_version", "leave_input_snapshot_key",
    "classification_snapshot_key", "break_treatment_profile_ref",
    "requirement_outcome",
)
DAY_MEASUREMENT_FIELDS = (
    "required_minutes", "qualifying_minutes", "gross_shortfall_minutes",
    "exclusion_minutes", "attributable_minutes", "schedule_variance_minutes",
    "contributes_to_accumulator_minutes",
)

WEEK_FIELDS = (
    "state", "weekly_attributable_shortfall_minutes", "canonical_dates",
    "observed_states", "blocking_dates", "day_count",
    "ledger_versions", "policy_versions", "config_versions",
)


@dataclass(frozen=True)
class Snapshot:
    """Every authoritative artifact in the window, keyed by its own identity."""

    inputs: Mapping[str, Any]
    days: Mapping[str, Mapping[str, Any]]
    weeks: Mapping[str, Mapping[str, Any]]
    events: Mapping[str, Mapping[str, Any]]
    notifications: Mapping[str, Mapping[str, Any]]
    incidents: Mapping[str, Mapping[str, Any]]
    day_alerts: Mapping[str, Mapping[str, Any]]


def _key(*parts: Any) -> str:
    return "|".join("" if p is None else str(p) for p in parts)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def capture(db, *, organization_id: int,
            dates: Sequence[date]) -> Snapshot:
    """Read every authoritative artifact for one tenant across the window.

    READS ONLY. The one call that is not a plain query is ``evaluate_workweek``,
    which is a deterministic fold over already-persisted rows and is the
    authoritative weekly answer — asking it is how the harness learns the week
    result without forming an opinion of its own.
    """
    from app.alert.models import EcoNotification
    from app.eco_day_alert import EcoDayAdministratorAlert
    from app.eco_ledger_incident import EcoLedgerIncident
    from app.eval_day.models import EcoDayRecord
    from app.eval_event.models import EcoThresholdEvent
    from app.eval_week import evaluate_workweek
    from app.work_ledger.models import WorkDayLedger
    import database.models as dbm

    first, last = dates[0], dates[-1]

    # ── The INPUTS, so a fixture that drifts is not read as an engine fault ──
    sessions = db.execute(
        sa.select(dbm.ClockSession.id, dbm.ClockSession.user_id,
                  dbm.ClockSession.clock_in_time, dbm.ClockSession.clock_out_time)
        .where(dbm.ClockSession.organization_id == organization_id)
        .order_by(dbm.ClockSession.id)).all()
    boundaries = db.execute(sa.text(
        "SELECT id, session_id, boundary, boundary_at, provenance FROM "
        "clock_session_boundary_event ORDER BY id")).all()
    config = db.execute(sa.text(
        "SELECT version_key, effective_from, content_hash FROM config_version "
        "WHERE organization_id = :o ORDER BY effective_from"),
        {"o": organization_id}).all()
    inputs = {
        "clock_sessions": [tuple(_iso(v) for v in r) for r in sessions],
        "boundary_events": [tuple(_iso(v) for v in r) for r in boundaries],
        "config_versions": [tuple(_iso(v) for v in r) for r in config],
    }

    # ── Days ────────────────────────────────────────────────────────────────
    days: Dict[str, Dict[str, Any]] = {}
    records = db.execute(
        sa.select(EcoDayRecord)
        .where(EcoDayRecord.organization_id == organization_id,
               EcoDayRecord.work_date >= first,
               EcoDayRecord.work_date <= last)
        .order_by(EcoDayRecord.user_id, EcoDayRecord.work_date,
                  EcoDayRecord.evaluation_generation)).scalars().all()
    for row in records:
        entry: Dict[str, Any] = {
            "state": row.state,
            "content_hash": row.content_hash,
            "superseded": row.superseded_by is not None,
        }
        for name in DAY_IDENTITY_FIELDS + DAY_PROVENANCE_FIELDS \
                + DAY_MEASUREMENT_FIELDS:
            entry[name] = _iso(getattr(row, name))
        days[_key(row.user_id, _iso(row.work_date),
                  row.evaluation_generation)] = entry

    workers = sorted({int(r.user_id) for r in records})

    # ── Ledger versions, so a silent re-seal is visible ─────────────────────
    ledgers = db.execute(
        sa.select(WorkDayLedger.user_id, WorkDayLedger.work_date,
                  WorkDayLedger.ledger_version, WorkDayLedger.state,
                  WorkDayLedger.workweek_key, WorkDayLedger.superseded_by)
        .where(WorkDayLedger.organization_id == organization_id)
        .order_by(WorkDayLedger.user_id, WorkDayLedger.work_date,
                  WorkDayLedger.ledger_version)).all()
    inputs["ledger_versions"] = [
        (r.user_id, _iso(r.work_date), r.ledger_version, r.state,
         r.workweek_key, r.superseded_by is not None) for r in ledgers]

    # ── Weeks ───────────────────────────────────────────────────────────────
    weeks: Dict[str, Dict[str, Any]] = {}
    for user_id in workers:
        for workweek_key in WORKWEEK_KEYS:
            week = evaluate_workweek(db, organization_id=organization_id,
                                     user_id=user_id, workweek_key=workweek_key)
            entry = {}
            for name in WEEK_FIELDS:
                value = getattr(week, name, None)
                if name == "state":
                    value = value.value
                elif isinstance(value, (list, tuple)):
                    value = [_iso(v) for v in value]
                entry[name] = _iso(value)
            weeks[_key(user_id, workweek_key)] = entry

    # ── Threshold events, by their §21.2 natural key ────────────────────────
    events: Dict[str, Dict[str, Any]] = {}
    for row in db.execute(
            sa.select(EcoThresholdEvent)
            .where(EcoThresholdEvent.organization_id == organization_id)
            .order_by(EcoThresholdEvent.id)).scalars().all():
        events[_key(row.user_id, row.workweek_key, row.tier, row.sequence,
                    row.mode, row.event_type)] = {
            "weekly_total_minutes": row.weekly_total_minutes,
            "threshold_minutes": row.threshold_minutes,
            "is_retroactive": bool(getattr(row, "is_retroactive", False)),
        }

    # ── Durable notifications, by the six-field episode identity ────────────
    notifications: Dict[str, Dict[str, Any]] = {}
    for row in db.execute(
            sa.select(EcoNotification)
            .where(EcoNotification.organization_id == organization_id)
            .order_by(EcoNotification.id)).scalars().all():
        notifications[_key(row.user_id, row.workweek_key, row.tier, row.mode,
                           row.sequence, row.notification_type,
                           row.audience)] = {
            "content_hash": getattr(row, "content_hash", None),
            "originating_event_id_present": row.originating_event_id is not None,
        }

    # ── §24.15 incidents and §24.3 day alerts ───────────────────────────────
    incidents = {
        _key(r.user_id, _iso(r.work_date), r.ledger_version): {
            "incident_type": r.incident_type, "ledger_state": r.ledger_state}
        for r in db.execute(
            sa.select(EcoLedgerIncident)
            .where(EcoLedgerIncident.organization_id == organization_id)
            .order_by(EcoLedgerIncident.id)).scalars().all()}

    day_alerts = {
        _key(r.user_id, _iso(r.work_date), r.evaluation_generation,
             r.alert_type, r.audience, r.mode): {
            "day_state": r.day_state, "recipient_outcome": r.recipient_outcome}
        for r in db.execute(
            sa.select(EcoDayAdministratorAlert)
            .where(EcoDayAdministratorAlert.organization_id == organization_id)
            .order_by(EcoDayAdministratorAlert.id)).scalars().all()}

    return Snapshot(inputs=inputs, days=days, weeks=weeks, events=events,
                    notifications=notifications, incidents=incidents,
                    day_alerts=day_alerts)


# ═════════════════════════════════════════════════════════════════════════════
# Comparison
# ═════════════════════════════════════════════════════════════════════════════
_FIELD_KIND = {
    **{f: DivergenceKind.IDENTITY_DIVERGENCE for f in DAY_IDENTITY_FIELDS},
    **{f: DivergenceKind.PROVENANCE_DIVERGENCE for f in DAY_PROVENANCE_FIELDS},
    **{f: DivergenceKind.CONTENT_DIVERGENCE for f in DAY_MEASUREMENT_FIELDS},
    "content_hash": DivergenceKind.CONTENT_DIVERGENCE,
    "state": DivergenceKind.STATE_DIVERGENCE,
    "superseded": DivergenceKind.IDENTITY_DIVERGENCE,
}


def _compare_mapping(first: Mapping[str, Mapping[str, Any]],
                     second: Mapping[str, Mapping[str, Any]],
                     *, artifact: str,
                     kind_for_field=None,
                     default_kind: DivergenceKind,
                     inputs_changed: bool = False) -> List[Divergence]:
    """Diff two identity-keyed artifact maps, classifying every difference.

    ``inputs_changed`` is what separates the two readings of a NEW key:

    * inputs UNCHANGED — a second artifact for a subject that already had one
      is the exact defect this harness hunts, so it is a DUPLICATE_ARTIFACT;
    * inputs CHANGED — a correction legitimately produces a new ledger version
      and a new evaluation generation. Those are new IDENTITIES, not duplicates,
      and calling them duplicates would report the supersession mechanism
      working as a fault. They are UNEXPECTED_ARTIFACT, and the accompanying
      INPUT_DIVERGENCE is what explains them.
    """
    out: List[Divergence] = []
    for key in sorted(set(first) - set(second)):
        out.append(Divergence(DivergenceKind.MISSING_ARTIFACT, artifact, key))
    for key in sorted(set(second) - set(first)):
        subject = key.rsplit("|", 1)[0]
        duplicate = (not inputs_changed
                     and any(k.rsplit("|", 1)[0] == subject for k in first))
        out.append(Divergence(
            DivergenceKind.DUPLICATE_ARTIFACT if duplicate
            else DivergenceKind.UNEXPECTED_ARTIFACT, artifact, key))
    for key in sorted(set(first) & set(second)):
        a, b = first[key], second[key]
        for name in sorted(set(a) | set(b)):
            if a.get(name) == b.get(name):
                continue
            kind = (kind_for_field or {}).get(name, default_kind)
            out.append(Divergence(kind, artifact, key, name,
                                  a.get(name), b.get(name)))
    return out


def compare(first: Snapshot, second: Snapshot) -> List[Divergence]:
    """Every classified difference between two passes. Empty means clean."""
    out: List[Divergence] = []

    # Inputs FIRST: a changed input explains everything downstream, and
    # reporting an engine fault on a drifted fixture would be a lie.
    for name in sorted(set(first.inputs) | set(second.inputs)):
        if first.inputs.get(name) != second.inputs.get(name):
            out.append(Divergence(DivergenceKind.INPUT_DIVERGENCE, "input",
                                  name))
    inputs_changed = bool(out)

    for kwargs in (
        dict(first=first.days, second=second.days, artifact="day",
             kind_for_field=_FIELD_KIND,
             default_kind=DivergenceKind.CONTENT_DIVERGENCE),
        dict(first=first.weeks, second=second.weeks, artifact="week",
             kind_for_field={"state": DivergenceKind.STATE_DIVERGENCE},
             default_kind=DivergenceKind.CONTENT_DIVERGENCE),
        dict(first=first.events, second=second.events, artifact="event",
             default_kind=DivergenceKind.THRESHOLD_DIVERGENCE),
        dict(first=first.notifications, second=second.notifications,
             artifact="notification",
             default_kind=DivergenceKind.NOTIFICATION_DIVERGENCE),
        dict(first=first.incidents, second=second.incidents,
             artifact="incident",
             default_kind=DivergenceKind.CONTENT_DIVERGENCE),
        dict(first=first.day_alerts, second=second.day_alerts,
             artifact="day_alert",
             default_kind=DivergenceKind.CONTENT_DIVERGENCE),
    ):
        out += _compare_mapping(inputs_changed=inputs_changed, **kwargs)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Evidence
# ═════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class DayEvidence:
    """One machine-checkable row per date per Worker, as §44.1 evidence."""

    work_date: str
    user_id: int
    ledger_version: Optional[int]
    evaluation_generation: Optional[int]
    evaluation_state: Optional[str]
    content_hash: Optional[str]
    workweek_key: str
    week_state: Optional[str]
    threshold_result: Optional[str]
    replay_clean: bool
    divergence: Optional[str] = None

    def as_row(self) -> Mapping[str, Any]:
        return {
            "date": self.work_date, "worker": self.user_id,
            "ledger_version": self.ledger_version,
            "evaluation_generation": self.evaluation_generation,
            "evaluation_state": self.evaluation_state,
            "content_hash": self.content_hash,
            "workweek_key": self.workweek_key,
            "week_state": self.week_state,
            "threshold_result": self.threshold_result,
            "replay": "CLEAN" if self.replay_clean else "DIVERGENT",
            "divergence_reason": self.divergence,
        }


@dataclass(frozen=True)
class ReplayReport:
    """The aggregate §44.1 result.

    ``verdict`` is ``HARNESS_CLEAN`` or ``HARNESS_NOT_CLEAN`` and NOTHING else.
    The operational gate — two complete workweeks actually observed in SHADOW
    (§44.2) — is a different claim that no test can make, and there is
    deliberately no value here that could be read as making it.
    """

    total_dates: int
    total_workers: int
    total_cases: int
    clean_cases: int
    divergent_cases: int
    duplicate_cases: int
    unexpected_cases: int
    missing_cases: int
    input_divergences: int
    failures: int
    verdict: str
    evidence: Sequence[DayEvidence] = field(default_factory=tuple)
    divergences: Sequence[Divergence] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        return self.verdict == "HARNESS_CLEAN"

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "total_dates": self.total_dates,
            "total_workers": self.total_workers,
            "total_cases": self.total_cases,
            "clean_cases": self.clean_cases,
            "divergent_cases": self.divergent_cases,
            "duplicate_cases": self.duplicate_cases,
            "unexpected_cases": self.unexpected_cases,
            "missing_cases": self.missing_cases,
            "input_divergences": self.input_divergences,
            "failures": self.failures,
            "verdict": self.verdict,
            "evidence": [e.as_row() for e in self.evidence],
            "divergences": [d.as_row() for d in self.divergences],
        }


def _week_key_for(work_date: date) -> str:
    """Which of the two workweeks a date falls in. Monday-start, by fixture."""
    return WORKWEEK_KEYS[0] if work_date < WINDOW_START + timedelta(days=7) \
        else WORKWEEK_KEYS[1]


def build_report(first: Snapshot, second: Snapshot, *,
                 dates: Sequence[date]) -> ReplayReport:
    """Turn two snapshots into per-date evidence and one aggregate verdict."""
    divergences = compare(first, second)

    touched = {d.key for d in divergences if d.artifact in ("day", "week")}
    reason_for: Dict[str, str] = {}
    for d in divergences:
        reason_for.setdefault(d.key, d.kind.value)

    workers = sorted({int(k.split("|")[0]) for k in first.days} or {0})
    evidence: List[DayEvidence] = []
    for work_date in dates:
        iso = work_date.isoformat()
        week_key = _week_key_for(work_date)
        for user_id in workers:
            day_keys = [k for k in first.days
                        if k.startswith(_key(user_id, iso) + "|")]
            latest = max(day_keys, default=None)
            day = first.days.get(latest) if latest else None
            week = first.weeks.get(_key(user_id, week_key), {})
            event = next((k for k in first.events
                          if k.startswith(f"{user_id}|{week_key}|")), None)
            keys = {latest, _key(user_id, week_key)} - {None}
            clean = not (keys & touched)
            evidence.append(DayEvidence(
                work_date=iso, user_id=user_id,
                ledger_version=(day or {}).get("ledger_version"),
                evaluation_generation=(day or {}).get("evaluation_generation"),
                evaluation_state=(day or {}).get("state"),
                content_hash=(day or {}).get("content_hash"),
                workweek_key=week_key,
                week_state=week.get("state"),
                threshold_result=event.split("|")[-1] if event else None,
                replay_clean=clean,
                divergence=next((reason_for[k] for k in sorted(keys)
                                 if k in reason_for), None),
            ))

    by_kind = {kind: sum(1 for d in divergences if d.kind is kind)
               for kind in DivergenceKind}
    divergent = sum(1 for e in evidence if not e.replay_clean)
    return ReplayReport(
        total_dates=len(dates),
        total_workers=len(workers),
        total_cases=len(evidence),
        clean_cases=len(evidence) - divergent,
        divergent_cases=divergent,
        duplicate_cases=by_kind[DivergenceKind.DUPLICATE_ARTIFACT],
        unexpected_cases=by_kind[DivergenceKind.UNEXPECTED_ARTIFACT],
        missing_cases=by_kind[DivergenceKind.MISSING_ARTIFACT],
        input_divergences=by_kind[DivergenceKind.INPUT_DIVERGENCE],
        failures=len(divergences),
        verdict="HARNESS_CLEAN" if not divergences else "HARNESS_NOT_CLEAN",
        evidence=tuple(evidence), divergences=tuple(divergences),
    )


def replay_window(db, *, organization_id: int,
                  dates: Sequence[date]) -> None:
    """Invoke the authorized driver once per date. Calculates nothing itself."""
    from app.eco_driver import run_eco_calculation_pass

    for work_date in dates:
        run_eco_calculation_pass(db, organization_id=organization_id,
                                 work_date=work_date)


def run_replay_harness(db, *, organization_id: int,
                       dates: Optional[Sequence[date]] = None) -> ReplayReport:
    """Pass 1, pass 2 over unchanged input, and the diff between them."""
    dates = tuple(dates or window_dates())
    replay_window(db, organization_id=organization_id, dates=dates)
    first = capture(db, organization_id=organization_id, dates=dates)
    replay_window(db, organization_id=organization_id, dates=dates)
    second = capture(db, organization_id=organization_id, dates=dates)
    return build_report(first, second, dates=dates)


__all__ = [
    "DAY_IDENTITY_FIELDS", "DAY_MEASUREMENT_FIELDS", "DAY_PROVENANCE_FIELDS",
    "WEEK_FIELDS", "WINDOW_DAYS", "WINDOW_START", "WORKWEEK_KEYS",
    "DayEvidence", "Divergence", "DivergenceKind", "ReplayReport", "Snapshot",
    "build_report", "capture", "compare", "replay_window",
    "run_replay_harness", "window_dates",
]
