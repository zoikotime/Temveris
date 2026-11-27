"""
Temveris — Break Governance Service
====================================
Single source of truth for the **Break Entitlement & Workday Provenance**
specification. The live endpoints (``/employees/start-break``,
``/employees/resume-work``, ``/break/*``), the overrun monitor
(``app.breaks.monitor``) and the unit tests all share the pure functions
below so the rules can never drift between surfaces.

Governed model
--------------
* Short Break  = 15 minutes   (BR-02)
* Lunch Break  = 60 minutes   (BR-02)
* Per-workday entitlements (workday = IST calendar day, matching the
  dashboard's "today" buckets):
    full_time   → 2 Short + 1 Lunch
    part_time   → 1 Short + 0 Lunch   (Lunch NOT allowed — BR-03)
    contractor  → workspace policy; default 1 Short + 0 Lunch
  All counts are overridable per (worker_type, break_type) through
  ``app_settings`` keys ``break.entitlement.<worker_type>.<break_type>``.

Enforcement (BR-01/BR-02/BR-03 + workday limits). A worker must NOT be able to
start: a second concurrent break, more than the entitled count of a type per
workday, a Lunch break they are not entitled to, or *any* break after Stop
Working (no open clock session).

Provenance
----------
Every governed action emits a versioned ``WorkEvent`` **and** a hash-chained
``WorkEvidenceChain`` entry via :mod:`app.wec.service` — append-only, never
overwritten. Each record captures: tenantId, workerId, workerType,
policyVersionId, scheduleVersionId, timestamp, deviceId, sessionId,
eligibilityResult and denialReason.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta, date as date_type
from typing import Optional, Dict, Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from database import models
from app.wec import service as wec_service


# ── Break types ───────────────────────────────────────────────────────────────
SHORT_BREAK_TYPE = "break"
LUNCH_BREAK_TYPE = "lunch"

# ── Allowances (BR-02). Configurable via app_settings; defaults match spec. ────
DEFAULT_SHORT_MINUTES = 15
DEFAULT_LUNCH_MINUTES = 60
SHORT_THRESHOLD_KEY = "break.short_minutes"   # shared with /break/thresholds
LUNCH_THRESHOLD_KEY = "break.lunch_minutes"

# ── Grace period (BR-02 escalation). Configurable PER BREAK TYPE via
# app_settings; defaults to 10 minutes each. The FIRST overrun alert fires
# only after a break EXCEEDS its effective limit:
#       effective_limit = allowance + grace
# (Short 15+10 = 25 min, Lunch 60+10 = 70 min). Reminders then follow every
# OVERRUN_REMINDER_MINUTES while the break stays active. See grace_minutes().
DEFAULT_SHORT_GRACE_MINUTES = 10
DEFAULT_LUNCH_GRACE_MINUTES = 10
# Continuous work without a break before the client nudges the employee. It
# was a literal 4 hours in Dashboard.jsx — a break-policy rule living in the
# browser, next to break thresholds that were already server-driven.
DEEP_WORK_KEY = "break.deep_work_minutes"
DEFAULT_DEEP_WORK_MINUTES = 240

SHORT_GRACE_KEY = "break.short_break_grace_minutes"
LUNCH_GRACE_KEY = "break.lunch_break_grace_minutes"

# Back-compat alias: the original flat delay constant equals the default grace.
# Prefer grace_minutes()/effective_limit_minutes() — they are configurable and
# per-break-type; this name remains only so older imports keep resolving.
OVERRUN_FIRST_NOTIFICATION_DELAY_MINUTES = DEFAULT_SHORT_GRACE_MINUTES

# Reminder cadence after the first alert.
OVERRUN_REMINDER_MINUTES = 30

# YELLOW indicator fires once a break passes this fraction of its allowance.
NEAR_LIMIT_FRACTION = 0.8

# ── Per-workday entitlements by normalized worker type ─────────────────────────
DEFAULT_ENTITLEMENTS: Dict[str, Dict[str, int]] = {
    "full_time":  {SHORT_BREAK_TYPE: 2, LUNCH_BREAK_TYPE: 1},
    "part_time":  {SHORT_BREAK_TYPE: 1, LUNCH_BREAK_TYPE: 0},
    "contractor": {SHORT_BREAK_TYPE: 1, LUNCH_BREAK_TYPE: 0},
}

# ── Denial codes (spec-defined where the spec names them) ──────────────────────
CODE_NOT_CLOCKED_IN           = "NOT_CLOCKED_IN"
CODE_ACTIVE_BREAK_IN_PROGRESS = "ACTIVE_BREAK_IN_PROGRESS"
CODE_LUNCH_NOT_ALLOWED        = "LUNCH_BREAK_NOT_ALLOWED"
CODE_DAILY_SHORT_USED         = "DAILY_SHORT_BREAK_ALREADY_USED"
CODE_DAILY_LUNCH_USED         = "DAILY_LUNCH_BREAK_ALREADY_USED"

# Map spec event_type → WEC action_type (WEC column is VARCHAR(50)).
_WEC_ACTION: Dict[str, str] = {
    "BREAK_START_REQUESTED":            "break_start_requested",
    "BREAK_STARTED":                    "break_started",
    "BREAK_DENIED":                     "break_denied",
    "BREAK_ALLOWANCE_REACHED":          "break_allowance_reached",
    "BREAK_OVERRUN":                    "break_overrun",
    "BREAK_OVERRUN_NOTIFICATION_SENT":  "break_overrun_notification_sent",
    "BREAK_ENDED":                      "break_ended",
}

# The workday resets at 00:00 in the ORGANIZATION's configured timezone,
# matching the admin dashboard's "today" buckets. This was a fixed +05:30, so a
# tenant outside India counted "breaks taken today" against India's midnight —
# an employee in New York had their break allowance reset mid-afternoon.
# app._tz resolves it from OrgSettings.default_timezone.


# ══════════════════════════════════════════════════════════════════════════════
# WORKER TYPE
# ══════════════════════════════════════════════════════════════════════════════
def normalize_worker_type(user_or_value) -> str:
    """Map any stored employment_type ("Full Time", "PART-TIME", "contract", …)
    to one of: full_time | part_time | contractor. Unknown → full_time (the
    safest default — it grants the *most* governed entitlements, never fewer)."""
    raw = getattr(user_or_value, "employment_type", user_or_value)
    norm = re.sub(r"[\s\-]+", "_", (raw or "full_time").strip().lower())
    if norm in ("part_time", "parttime", "pt", "part"):
        return "part_time"
    if norm in ("contractor", "contract", "freelance", "freelancer", "consultant"):
        return "contractor"
    return "full_time"


# ══════════════════════════════════════════════════════════════════════════════
# WORKDAY (IST calendar day)
# ══════════════════════════════════════════════════════════════════════════════
def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def workday_date(db: Session, org_id, now: Optional[datetime] = None) -> date_type:
    """The organization-local calendar date ``now`` falls on."""
    from app import _tz as _tzmod
    now = _as_utc(now) or datetime.now(timezone.utc)
    return now.astimezone(_tzmod.org_timezone(db, org_id)).date()


def workday_bounds_utc(db: Session, org_id,
                       now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """``[start, end)`` UTC instants of the organization-local calendar day that
    contains ``now`` — used to count "breaks already taken today"."""
    from app import _tz as _tzmod
    return _tzmod.local_day_bounds(db, org_id, workday_date(db, org_id, now))


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG READS (app_settings)
# ══════════════════════════════════════════════════════════════════════════════
# `app_settings` is a tiny, rarely-mutated config table, yet it is read many
# times per break action (policy_version_id alone reads it 3×) and once PER
# active break on every 60s monitor tick. Each read was a separate single-row
# SELECT — pure round-trip latency on Supabase. Cache the raw value for a few
# seconds: within a request/tick all reads of the same key are folded into one,
# while a ≤10s TTL keeps admin setting changes effectively live. Purely additive
# — any miss / error falls straight through to the DB.
_SETTINGS_TTL_SEC = 10.0
# (org_id, key) -> (raw_value_or_None, expires_at_monotonic). The org id is part
# of the cache key: keying on `key` alone let one tenant's cached value be served
# to every other tenant for the whole TTL.
_settings_cache: dict = {}


def _cached_setting_value(db: Session, key: str, org_id=None):
    # Bypass the cache on SQLite (the unit-test DB). The cache exists purely to
    # amortize network round-trips to remote Postgres/Supabase; SQLite is
    # in-process so there's nothing to save, and a process-global TTL cache
    # would otherwise leak app_settings values across tests that set-then-read.
    try:
        use_cache = db.bind.dialect.name != "sqlite"
    except Exception:
        use_cache = True

    now = time.monotonic()
    cache_key = (org_id, key)
    if use_cache:
        hit = _settings_cache.get(cache_key)
        if hit is not None and hit[1] > now:
            return hit[0]
    try:
        # Scoped to this org AND to the org tier (team_key/user_id NULL). Without
        # the org filter a row from another tenant could be returned; without the
        # tier filter a team/employee override could be read as the org value.
        row = db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.key == key,
            models.AppSettings.team_key.is_(None),
            models.AppSettings.user_id.is_(None),
        ).first()
        val = row.value if row is not None else None
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None   # never cache a transient DB error
    if use_cache:
        _settings_cache[cache_key] = (val, now + _SETTINGS_TTL_SEC)
    return val


def invalidate_settings_cache() -> None:
    """Clear the settings cache. Call after writing any ``break.*`` / ``tenant.id``
    app_setting so the change is reflected immediately rather than after the TTL."""
    _settings_cache.clear()


def _setting_int(db: Session, key: str, default: int, org_id=None) -> int:
    raw = _cached_setting_value(db, key, org_id)
    if raw is not None:
        try:
            return max(0, int(str(raw).strip()))
        except (TypeError, ValueError):
            return default
    return default


def allowance_minutes(db: Session, break_type: str, org_id=None) -> int:
    """BR-02 allowance for a break type (minutes). Never below 1."""
    if break_type == LUNCH_BREAK_TYPE:
        return max(1, _setting_int(db, LUNCH_THRESHOLD_KEY, DEFAULT_LUNCH_MINUTES, org_id))
    return max(1, _setting_int(db, SHORT_THRESHOLD_KEY, DEFAULT_SHORT_MINUTES, org_id))


def grace_minutes(db: Session, break_type: str, org_id=None) -> int:
    """Grace window (minutes) added to the allowance before the FIRST overrun
    alert fires. Configurable per break type via app_settings
    (``break.short_break_grace_minutes`` / ``break.lunch_break_grace_minutes``);
    defaults to 10 each. Never negative — a 0 grace restores alert-at-allowance."""
    if break_type == LUNCH_BREAK_TYPE:
        return max(0, _setting_int(db, LUNCH_GRACE_KEY, DEFAULT_LUNCH_GRACE_MINUTES, org_id))
    return max(0, _setting_int(db, SHORT_GRACE_KEY, DEFAULT_SHORT_GRACE_MINUTES, org_id))


def effective_limit_minutes(db: Session, break_type: str, org_id=None) -> int:
    """Allowance + grace — a break must EXCEED this many minutes before the first
    overrun alert is sent (Short 15+5=20, Lunch 60+5=65 by default)."""
    return allowance_minutes(db, break_type, org_id) + grace_minutes(db, break_type, org_id)


def entitlement_for(db: Session, worker_type, break_type: str, org_id=None) -> int:
    """How many of ``break_type`` this worker type may take per workday."""
    wt = normalize_worker_type(worker_type)
    base = DEFAULT_ENTITLEMENTS.get(wt, DEFAULT_ENTITLEMENTS["full_time"]).get(break_type, 0)
    return _setting_int(db, f"break.entitlement.{wt}.{break_type}", base, org_id)


# ══════════════════════════════════════════════════════════════════════════════
# PROVENANCE CONTEXT HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def tenant_id(db: Session, org_id=None) -> str:
    """Single-tenant deployments still record a stable tenant id for the audit
    stream — read from app_settings ``tenant.id`` (default "default")."""
    raw = _cached_setting_value(db, "tenant.id", org_id)
    if raw:
        return str(raw).strip()
    return "default"


def policy_version_id(db: Session, org_id=None) -> str:
    """A version stamp that changes whenever the governed allowance changes, so
    every audit record is attributable to the policy that produced it."""
    base = _setting_str(db, "break.policy_version", "1.0", org_id)
    short = allowance_minutes(db, SHORT_BREAK_TYPE, org_id)
    lunch = allowance_minutes(db, LUNCH_BREAK_TYPE, org_id)
    return f"{base}:{short}m/{lunch}m"


def schedule_version_id(user) -> str:
    """Derived from the worker's clock-in schedule config — provenance for which
    schedule the eligibility decision was made against."""
    ci = getattr(user, "default_clock_in_time", None) or "default"
    grace = getattr(user, "default_clock_in_grace_min", None)
    return f"{ci}+{grace if grace is not None else 'def'}"


def _setting_str(db: Session, key: str, default: str, org_id=None) -> str:
    raw = _cached_setting_value(db, key, org_id)
    if raw is not None:
        return str(raw).strip()
    return default


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVE STATE LOOKUPS
# ══════════════════════════════════════════════════════════════════════════════
def get_active_session(db: Session, user_id: int) -> Optional[models.ClockSession]:
    return (
        db.query(models.ClockSession)
        .filter(and_(
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_out_time == None,  # noqa: E711
        ))
        .first()
    )


def get_active_break(db: Session, user_id: int) -> Optional[models.BreakSession]:
    return (
        db.query(models.BreakSession)
        .filter(and_(
            models.BreakSession.user_id == user_id,
            models.BreakSession.break_end == None,  # noqa: E711
        ))
        .order_by(models.BreakSession.break_start.desc())
        .first()
    )


def breaks_used_today(db: Session, user_id: int, break_type: str,
                      now: Optional[datetime] = None, org_id=None) -> int:
    if org_id is None:
        org_id = db.query(models.User.organization_id).filter(
            models.User.id == user_id).scalar()
    start_utc, end_utc = workday_bounds_utc(db, org_id, now)
    return int(
        db.query(func.count(models.BreakSession.id))
        .filter(and_(
            models.BreakSession.user_id == user_id,
            models.BreakSession.break_type == break_type,
            models.BreakSession.break_start >= start_utc,
            models.BreakSession.break_start < end_utc,
        ))
        .scalar() or 0
    )


# ══════════════════════════════════════════════════════════════════════════════
# ELIGIBILITY EVALUATION
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class EligibilityResult:
    allowed: bool
    code: Optional[str]
    message: str
    worker_type: str
    break_type: str
    allowed_minutes: int
    entitlement: int
    used_today: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_eligibility(
    db: Session,
    user,
    break_type: str,
    *,
    active_session: Optional[models.ClockSession] = None,
    active_break: Optional[models.BreakSession] = None,
    now: Optional[datetime] = None,
    _looked_up: bool = False,
) -> EligibilityResult:
    """Decide whether ``user`` may start ``break_type`` right now. Pure given
    the (session, active break, counts) it reads — returns a structured result;
    callers translate it into HTTP/audit output.

    Pass ``active_session`` / ``active_break`` when the caller already has them
    (avoids re-querying); otherwise they are looked up here.
    """
    break_type = LUNCH_BREAK_TYPE if break_type == LUNCH_BREAK_TYPE else SHORT_BREAK_TYPE
    wt = normalize_worker_type(user)
    _org = getattr(user, "organization_id", None)
    allowed_min = allowance_minutes(db, break_type, _org)
    entitlement = entitlement_for(db, wt, break_type, _org)

    if active_session is None and not _looked_up:
        active_session = get_active_session(db, user.id)
    if active_break is None and not _looked_up:
        active_break = get_active_break(db, user.id)

    used = breaks_used_today(db, user.id, break_type, now=now,
                             org_id=getattr(user, "organization_id", None))

    def deny(code: str, msg: str) -> EligibilityResult:
        return EligibilityResult(False, code, msg, wt, break_type, allowed_min, entitlement, used)

    # Start any break after Stop Working has occurred → blocked (no open session).
    if active_session is None:
        return deny(CODE_NOT_CLOCKED_IN, "You must be clocked in to start a break.")

    # BR-01 — only one active break at a time.
    if active_break is not None:
        return deny(
            CODE_ACTIVE_BREAK_IN_PROGRESS,
            "You already have an active break running. Please resume work first.",
        )

    # BR-03 — Lunch break entitlement (part-time = none; contractor per policy).
    if break_type == LUNCH_BREAK_TYPE and entitlement <= 0:
        if wt == "part_time":
            msg = "Lunch break is not available for part-time employees."
        else:
            msg = "Lunch break is not permitted under your workspace policy."
        return deny(CODE_LUNCH_NOT_ALLOWED, msg)

    # Per-workday count — at most `entitlement` of this type per workday. The
    # message pluralizes off the effective entitlement so it stays accurate for
    # types granted more than one per day (full_time = 2 Short) and for any
    # admin override via break.entitlement.<worker_type>.<break_type>.
    if used >= max(0, entitlement):
        label = break_label(break_type)
        if entitlement > 1:
            msg = f"You have already used all {entitlement} of your {label}s for today."
        else:
            msg = f"You have already used your {label} for today."
        return deny(
            CODE_DAILY_LUNCH_USED if break_type == LUNCH_BREAK_TYPE else CODE_DAILY_SHORT_USED,
            msg,
        )

    return EligibilityResult(True, None, "Break allowed.", wt, break_type, allowed_min, entitlement, used)


# ══════════════════════════════════════════════════════════════════════════════
# UI PRESENTATION HELPERS (returned to clients so copy stays consistent)
# ══════════════════════════════════════════════════════════════════════════════
def break_label(break_type: str) -> str:
    return "Lunch Break" if break_type == LUNCH_BREAK_TYPE else "Short Break"


def status_chip(break_type: str, exceeded: bool) -> str:
    base = f"On {break_label(break_type)}"
    return f"{base} (Exceeded)" if exceeded else base


def overrun_banner(break_type: str) -> str:
    return f"Your {break_label(break_type)} allowance has ended. Please resume work."


def governance_state(elapsed_seconds: int, allowed_minutes: int) -> str:
    """Dashboard indicator: GREEN within policy, YELLOW near limit, RED exceeded."""
    allowed_s = max(1, allowed_minutes) * 60
    if elapsed_seconds >= allowed_s:
        return "red"
    if elapsed_seconds >= allowed_s * NEAR_LIMIT_FRACTION:
        return "yellow"
    return "green"


# ══════════════════════════════════════════════════════════════════════════════
# IMMUTABLE PROVENANCE (append-only WorkEvent + hash-chained WEC entry)
# ══════════════════════════════════════════════════════════════════════════════
def record_break_event(
    db: Session,
    *,
    user,
    event_type: str,
    session_id: Optional[int] = None,
    break_id: Optional[int] = None,
    worker_type: Optional[str] = None,
    break_type: Optional[str] = None,
    allowed_minutes: Optional[int] = None,
    eligibility_result: Optional[str] = None,
    denial_reason: Optional[str] = None,
    device_info: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    commit: bool = False,
) -> Optional[models.WorkEvent]:
    """Append one immutable provenance record (WorkEvent + WEC chain entry).

    Never overwrites: when ``idempotency_key`` is supplied and already present,
    the existing event is returned and no new WEC entry is created (so the
    overrun monitor can run every minute without duplicating the chain).
    """
    if idempotency_key:
        existing = (
            db.query(models.WorkEvent)
            .filter(models.WorkEvent.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing

    wt = worker_type or normalize_worker_type(user)
    now = datetime.now(timezone.utc)
    device_info = device_info or {}
    payload: Dict[str, Any] = {
        "tenantId":          tenant_id(db, getattr(user, "organization_id", None)),
        "workerId":          getattr(user, "id", None),
        "workerName":        getattr(user, "name", None),
        "workerType":        wt,
        "policyVersionId":   policy_version_id(db, getattr(user, "organization_id", None)),
        "scheduleVersionId": schedule_version_id(user),
        "timestamp":         now.isoformat(),
        "deviceId":          device_info.get("deviceId") or device_info.get("device_id"),
        "sessionId":         session_id,
        "breakId":           break_id,
        "breakType":         break_type,
        "allowedMinutes":    allowed_minutes,
        "eligibilityResult": eligibility_result,
        "denialReason":      denial_reason,
    }
    if extra:
        payload.update(extra)

    action = _WEC_ACTION.get(event_type, event_type.lower())
    wec_entry = wec_service.create_wec_entry(
        db,
        user_id=getattr(user, "id", None),
        action_type=action,
        session_id=session_id,
        device_info=device_info,
        payload=payload,
    )
    event = wec_service.emit_work_event(
        db,
        event_type=event_type,
        user_id=getattr(user, "id", None),
        session_id=session_id,
        payload=payload,
        idempotency_key=idempotency_key,
        wec_id=wec_entry.work_record_id if wec_entry else None,
    )

    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    return event
