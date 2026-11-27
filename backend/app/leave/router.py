"""
Temveris – Leave Management Router (Enterprise Edition v2)
Policy engine: eligibility, balance, notice period, documents, frequency, pattern detection
Approval workflow: Manager (under_review) → HR (approved)
Auto-LWP: missing docs, insufficient balance, late submission
AWOL: unapproved absence
Leave Types: 11 types based on Temveris Comprehensive Leave Policy
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import and_
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Optional, List
import os, shutil, uuid, logging, traceback, json

from database.database import get_db
from database import models
from app.auth.service import get_current_user, get_current_organization
from app.email_service import send_leave_request_to_admin, send_leave_status_to_employee, send_leave_submitted_to_employee
from app._time_utils import iso_z
from app.leave import policy as leave_policy
from app.leave import identity as leave_identity
from app import _tz

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Leave"])

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ATTACH_DIR = os.path.join(BASE_DIR, "..", "..", "..", "tracker-data", "leave-attachments")
os.makedirs(ATTACH_DIR, exist_ok=True)

# ── Supporting-document upload contract ───────────────────────────────────────
# One source of truth for what /leave/apply accepts. GET /leave/types publishes
# `doc_max_mb` / `doc_accept` / `doc_formats` from these constants so the Apply
# Leave dropzone advertises exactly what this endpoint enforces — the limit
# cannot drift between the two the way a hardcoded frontend copy would.
#
# The stored file's extension is derived from the VALIDATED content type, never
# from the client's filename: a request claiming `image/png` while uploading
# "payload.exe" would otherwise be written to disk as .exe and served back from
# the /leave-attachments path.
MIME_EXT = {
    "application/pdf": "pdf",
    "image/jpeg":      "jpg",
    "image/png":       "png",
    # WEBP is accepted but not advertised: it predates this contract and
    # rejecting it now would bounce a format employees have already been
    # filing with. New uploads are steered to the four formats below.
    "image/webp":      "webp",
}
ALLOWED_MIME = set(MIME_EXT)
MAX_FILE_MB  = 10
DOC_FORMATS  = ("PDF", "JPG", "JPEG", "PNG")
DOC_ACCEPT   = ".pdf,.jpg,.jpeg,.png"

# ── Leave type policy definitions (Temveris – India Mode default) ─────────────
# Flags:
#   shared_pool   – leave draws from the Annual Leave master pool when the
#                   "shared_pool_enabled" toggle is on (India default).
#   hard_notice   – notice_days is hard-enforced (block) regardless of docs.
#   evidence_gate – Submit must be blocked in the UI until a document is uploaded.
#
# BUG-010 fix: notice_days raised from 5 → 7 days for Annual / Casual /
# Child Care / Parent Care / Medical Escort to match the official Temveris
# Leave Summary. With the old 5-day window, requests the spec considered
# "rejected/blocked" (e.g. Annual Leave with 6 days notice) were being
# accepted at /leave/apply and counted toward used days — looking to the
# user like a rejected leave was still deducted. The hard_notice flag
# (set on Annual/Casual) guarantees those are 422'd at submission so no DB
# row is written, no balance is consumed. (Compensatory Leave was previously
# exempted from the notice gate, but per 2026-07 policy now requires 7 days
# advance notice, hard-blocked like Annual/Casual — see its entry below.)
LEAVE_POLICIES = {
    "Annual Leave":       {"limit": 15,  "notice_days": 7, "doc_required": False, "accrual": 1.25, "shared_pool": False, "hard_notice": True,  "evidence_gate": False},
    "Casual Leave":       {"limit": 15,  "notice_days": 7, "doc_required": False, "accrual": 0.0,  "shared_pool": True,  "hard_notice": True,  "evidence_gate": False},
    # `waiting_exempt` — available from day one, like Compensatory and Unpaid.
    # You do not schedule illness or an emergency around a tenure milestone, so
    # gating these behind the new-joiner paid-leave waiting period left a sick
    # new joiner with no lawful way to report an absence: the banner told them
    # "paid leave not yet available" and /leave/apply answered 403. Both types
    # keep every other rule (notice, annual cap, document gate).
    "Sick Leave":         {"limit": 15,  "notice_days": 0, "doc_required": True,  "accrual": 0.0,  "shared_pool": True,  "hard_notice": False, "evidence_gate": True, "waiting_exempt": True},
    # Compensatory Leave is claim-based: employees list the extra worked
    # dates that justify the comp-off. It has no fixed annual quota and needs
    # no separate upload — the worked-date list is the evidence HR reviews.
    # Per policy (2026-07) it now requires 7 days advance notice, hard-blocked
    # like Annual/Casual — comp-off must be planned ahead, not taken same-day.
    "Compensatory Leave": {"limit": 0,   "notice_days": 7, "doc_required": False, "accrual": 0.0,  "shared_pool": False, "hard_notice": True,  "evidence_gate": False, "earned": True, "no_annual_limit": True, "balance_exempt": True},
    "Maternity Leave":    {"limit": 182, "notice_days": 60, "doc_required": True, "accrual": 0.0,  "shared_pool": False, "hard_notice": False, "evidence_gate": True, "balance_exempt": True},
    "Paternity Leave":    {"limit": 15,  "notice_days": 14, "doc_required": True, "accrual": 0.0,  "shared_pool": False, "hard_notice": False, "evidence_gate": False, "balance_exempt": True},
    "Adoption Leave":     {"limit": 84,  "notice_days": 30, "doc_required": True, "accrual": 0.0,  "shared_pool": False, "hard_notice": False, "evidence_gate": False, "balance_exempt": True},
    "Bereavement Leave":  {"limit": 2,   "notice_days": 0, "doc_required": True,  "accrual": 0.0,  "shared_pool": False, "hard_notice": False, "evidence_gate": False, "balance_exempt": True},
    "Child Care Leave":   {"limit": 15,  "notice_days": 7, "doc_required": True,  "accrual": 0.0,  "shared_pool": True,  "hard_notice": False, "evidence_gate": False},
    "Parent Care Leave":  {"limit": 15,  "notice_days": 7, "doc_required": True,  "accrual": 0.0,  "shared_pool": True,  "hard_notice": False, "evidence_gate": False},
    "Medical Escort Leave":{"limit": 15, "notice_days": 7, "doc_required": True,  "accrual": 0.0,  "shared_pool": True,  "hard_notice": False, "evidence_gate": False},
    "Emergency Leave":    {"limit": 6,   "notice_days": 0, "doc_required": True,  "accrual": 0.0,  "shared_pool": True,  "hard_notice": False, "evidence_gate": True, "waiting_exempt": True},
    # Company Leave / Unpaid Leave are the only genuinely unlimited buckets:
    # the `unlimited: True` flag lets is_unlimited skip the legacy >=99 heuristic
    # so capped types like Maternity (182) don't get mis-labelled.
    "Company Leave":      {"limit": 0,   "notice_days": 0, "doc_required": False, "accrual": 0.0,  "shared_pool": False, "hard_notice": False, "evidence_gate": False, "unlimited": True, "balance_exempt": True},
    "Unpaid Leave":       {"limit": 0,   "notice_days": 7, "doc_required": False, "accrual": 0.0,  "shared_pool": False, "hard_notice": True,  "evidence_gate": False, "unlimited": True, "balance_exempt": True},
    # Half-day type. limit is denominated in DAYS (10 = up to 20 half-day
    # requests per year). The frontend forces is_half_day=true and a single
    # date when this type is picked, and the apply handler refuses any
    # request on this type that isn't tagged half-day.
    "Half Day Leave":     {"limit": 10,  "notice_days": 0, "doc_required": False, "accrual": 0.0,  "shared_pool": False, "hard_notice": False, "evidence_gate": False, "half_day_only": True},
}

# Types that draw from the Annual master pool when shared_pool_enabled is on.
SHARED_POOL_TYPES = {lt for lt, p in LEAVE_POLICIES.items() if p.get("shared_pool")}

# Types that always count as 0.5-day requests. The frontend uses this to
# auto-disable the "To Date" picker; the backend enforces it server-side.
HALF_DAY_ONLY_TYPES = {lt for lt, p in LEAVE_POLICIES.items() if p.get("half_day_only")}

# ── Full leave type details for employee reference ─────────────────────────────
LEAVE_TYPE_DETAILS = {
    "Annual Leave": {
        "icon": "🏖️",
        "description": "Paid time off for rest, recreation, and personal activities. Acts as the master Shared Pool in India Mode — Casual, Sick and Non-Medical Emergency leave deduct from this balance.",
        "eligibility": "All full-time employees. Accrues at 1.25 days per completed month of service.",
        "max_duration": "15 days per calendar year (1.25 days/month accrual)",
        "required_docs": "No documents required. Minimum 7 days advance notice is mandatory.",
        "terms": [
            "Hard-block: submissions with less than 7 days notice are rejected",
            "Master Shared Pool — Casual / Sick / Emergency draw from this balance when the Shared Pool toggle is ON",
            "Excess beyond the pool auto-converts to Unpaid Leave (LWP)",
            "Subject to manager approval and team availability",
        ],
    },
    "Casual Leave": {
        "icon": "🗓️",
        "description": "Short-term leave for personal errands or unforeseen events. Deducts from the Annual Leave Shared Pool in India Mode.",
        "eligibility": "All confirmed employees.",
        "max_duration": "Drawn from the Annual Leave pool (15 days shared)",
        "required_docs": "No documents required. Minimum 7 days advance notice is mandatory.",
        "terms": [
            "Hard-block: submissions with less than 7 days notice are rejected",
            "Deducts from the Annual Leave Shared Pool",
            "If the pool is exhausted, leave auto-converts to Unpaid Leave (LWP)",
        ],
    },
    "Sick Leave": {
        "icon": "🤒",
        "description": "Paid leave for illness, injury or medical treatment. Deducts from the Annual Leave Shared Pool in India Mode.",
        "eligibility": "All employees. No advance notice required — inform manager as early as possible.",
        "max_duration": "Drawn from the Annual Leave pool (15 days shared)",
        "required_docs": "Mandatory MBBS / registered-medical-practitioner certificate — Submit is blocked until uploaded.",
        "terms": [
            "Tier 0 Evidence Gate: Submit is disabled until a valid medical certificate is attached",
            "No minimum notice — can be applied retroactively",
            "Deducts from the Annual Leave Shared Pool",
            "If the pool is exhausted or docs are missing/invalid, excess auto-converts to Unpaid Leave (LWP)",
            "Frequent Sick Leave (≥2 times/month) or Monday/Friday patterns trigger HR review",
        ],
    },
    "Compensatory Leave": {
        "icon": "⚖️",
        "description": "Leave earned in lieu of approved overtime worked on weekends, public holidays or beyond regular working hours.",
        "eligibility": "Employees who worked extra time on overtime, weekends, public holidays or beyond regular working hours.",
        "max_duration": "No fixed annual cap; each request must list the dates already worked.",
        "required_docs": "No separate document required. Add the extra worked dates in the request.",
        "terms": [
            "Minimum 7 days advance notice required — same-day / short-notice comp-off is rejected",
            "At least one extra worked date is required",
            "HR verifies the listed worked dates before approval",
            "Not encashable",
        ],
    },
    "Maternity Leave": {
        "icon": "🤱",
        "description": "Paid leave for female employees before and after childbirth, per the Maternity Benefit Act.",
        "eligibility": "Female employees who have worked at least 80 days in the preceding 12 months.",
        "max_duration": "182 days (26 weeks)",
        "required_docs": "Medical certificate with EDD and hospital discharge proof — Submit is blocked until uploaded.",
        "terms": [
            "Tier 0 Evidence Gate: Submit is disabled until medical proof is attached",
            "Minimum 60 days advance notice required",
            "Job protection guaranteed during leave",
        ],
    },
    "Paternity Leave": {
        "icon": "👨‍👶",
        "description": "Paid leave for male employees upon the birth or adoption of a child.",
        "eligibility": "All confirmed male employees.",
        "max_duration": "15 days within 3 months of birth/adoption",
        "required_docs": "Birth certificate of the child.",
        "terms": [
            "Minimum 14 days advance notice required",
            "Birth certificate must be submitted with the request",
        ],
    },
    "Adoption Leave": {
        "icon": "🧡",
        "description": "Leave for employees legally adopting a child — for legal proceedings, bonding and transition.",
        "eligibility": "All confirmed employees with a valid adoption proceeding.",
        "max_duration": "84 days",
        "required_docs": "Legal adoption order / court documents.",
        "terms": [
            "Minimum 30 days advance notice required",
            "Valid legal proof must be attached with the request",
        ],
    },
    "Bereavement Leave": {
        "icon": "🕊️",
        "description": "Compassionate leave on the death of an immediate family member.",
        "eligibility": "All employees. Immediate family: spouse, child, parent, sibling, parent-in-law.",
        "max_duration": "2 days per bereavement event",
        "required_docs": "Death certificate required.",
        "terms": [
            "No minimum notice — inform manager as soon as possible",
            "Death certificate must be provided with or shortly after the request",
        ],
    },
    "Child Care Leave": {
        "icon": "👶",
        "description": "Leave to care for a sick or dependent child. Deducts from the Annual Leave Shared Pool; excess auto-converts to LWP.",
        "eligibility": "All confirmed employees with a dependent child.",
        "max_duration": "Drawn from Annual Leave pool; LWP beyond the balance",
        "required_docs": "Medical proof for the child required.",
        "terms": [
            "Minimum 7 days advance notice where possible",
            "Medical proof must be attached",
            "Excess days beyond the Shared Pool auto-convert to Unpaid Leave (LWP)",
        ],
    },
    "Parent Care Leave": {
        "icon": "👵",
        "description": "Leave to care for a parent in need of medical or dependency support. Deducts from the Annual Leave Shared Pool; excess auto-converts to LWP.",
        "eligibility": "All confirmed employees.",
        "max_duration": "Drawn from Annual Leave pool; LWP beyond the balance",
        "required_docs": "Medical proof for the parent required.",
        "terms": [
            "Minimum 7 days advance notice where possible",
            "Medical proof must be attached",
            "Excess days beyond the Shared Pool auto-convert to Unpaid Leave (LWP)",
        ],
    },
    "Medical Escort Leave": {
        "icon": "🚑",
        "description": "Leave to accompany an immediate family member for medical treatment. Deducts from the Annual Leave Shared Pool; excess auto-converts to LWP.",
        "eligibility": "All confirmed employees.",
        "max_duration": "Drawn from Annual Leave pool; LWP beyond the balance",
        "required_docs": "Medical proof / appointment letter required.",
        "terms": [
            "Minimum 7 days advance notice where possible",
            "Medical proof must be attached",
            "Excess days beyond the Shared Pool auto-convert to Unpaid Leave (LWP)",
        ],
    },
    "Emergency Leave": {
        "icon": "🚨",
        "description": "Leave for sudden emergencies (accidents, hospitalisation, natural disasters, legal/government summons).",
        "eligibility": "All employees. Can be applied retroactively within 2 business days.",
        "max_duration": "6 days per calendar year (Non-Medical emergencies deduct from the Annual pool)",
        "required_docs": "Legal or government-issued supporting document — Submit is blocked until uploaded.",
        "terms": [
            "Tier 0 Evidence Gate: Submit is disabled until supporting proof is attached",
            "No minimum notice — inform manager as soon as possible",
            "Non-Medical emergencies deduct from the Annual Leave Shared Pool",
            "If docs are missing/invalid, the request auto-converts to Unpaid Leave (LWP)",
        ],
    },
    "Company Leave": {
        "icon": "🏢",
        "description": "Mandatory or discretionary leave declared by the company for special occasions, off-site events, festive shutdowns, or operational requirements.",
        "eligibility": "All employees — attendance is not required during Company Leave periods.",
        "max_duration": "As declared by management (typically 1–5 days per event)",
        "required_docs": "No documents required. Company issues official notice.",
        "terms": [
            "Declared by Management/HR — employees have no discretion",
            "Does not deduct from personal leave balances",
            "Cannot be exchanged for salary or carry-forward leave",
            "Remote employees must check local holiday calendar",
            "Annual festive shutdown (typically year-end) is included in this category",
        ],
    },
    "Unpaid Leave": {
        "icon": "📋",
        "description": "Leave Without Pay (LWP) — taken when all paid leave balances are exhausted or as mutually agreed between employee and management for personal reasons.",
        "eligibility": "All employees. Requires manager and HR approval. No minimum service period required.",
        "max_duration": "No hard limit — subject to approval. Typically up to 30 days per year.",
        "required_docs": "No mandatory documents. HR may request justification for extended unpaid leave.",
        "terms": [
            "No pay for days on LWP — deducted from monthly salary",
            "Benefits (insurance, PF) continue during unpaid leave up to 30 days",
            "Extended LWP (>30 days) may affect annual increment, bonus eligibility",
            "Probationary employees require additional HR approval",
            "Automatically applied when Annual Leave balance is exhausted (auto-LWP)",
            "Must be approved before starting leave (except emergency conversions)",
        ],
    },
    "Half Day Leave": {
        "icon": "🌗",
        "description": "Half-day leave allows you to take time off for either the first or second half of the day. Each request consumes 0.5 days from your Half Day balance.",
        "eligibility": "All employees. Single calendar day only — pick the morning or afternoon half.",
        "max_duration": "10 days per year (equivalent to 20 half-day requests)",
        "required_docs": "No documents required.",
        "terms": [
            "Single calendar date only — the To Date is auto-locked to the From Date",
            "Pick Morning Half (first_half) or Evening Half (second_half) when applying",
            "Each approved request deducts 0.5 day from the Half Day Leave balance",
            "Subject to manager approval; counts toward attendance for the chosen half",
        ],
    },
}

# ── Valid state transitions ────────────────────────────────────────────────────
TRANSITIONS = {
    "submitted":          ["under_review", "approved", "rejected"],
    "under_review":       ["approved", "rejected"],
    "approved":           ["locked_for_payroll"],
    "rejected":           [],
    "locked_for_payroll": [],
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _log_audit(db, actor_id, action, entity, target_id=None, payload=None, org_id=None):
    db.add(models.AuditLog(
        actor_id=actor_id, action=action, entity=entity,
        target_id=target_id, payload=payload or {}, organization_id=org_id,
    ))

def _emit_event(db, event_type, aggregate_id, payload):
    db.add(models.OutboxEvent(
        event_type=event_type, aggregate_id=aggregate_id,
        payload=payload, status="pending"
    ))

def _alert_recipients(db: Session, user, purpose: str) -> list:
    """This employee's organization's own approval inbox. Never a platform
    address — see app/notifications/recipients.py."""
    try:
        from app.notifications.recipients import recipients_for_user
        return recipients_for_user(db, user, purpose=purpose)
    except Exception:
        return []


def _org_setting(db: Session, org_id: Optional[int], base_key: str) -> Optional[str]:
    """Read one leave-policy setting for exactly this organization.

    Two things this fixes, both cross-tenant:

    * **The SQL had no ``organization_id`` filter.** Isolation rested entirely
      on the ``:{org_id}`` suffix baked into the key string. Any row whose key
      matched — including one written by another tenant's code path, or a
      historical unsuffixed row — could be returned by ``.first()``.
    * **``org_id is None`` collapsed the key to a shared global bucket.** Every
      org-less user read and wrote the SAME ``leave_policies`` row, so one
      workspace's leave configuration became another's. There is no legitimate
      tenant-less leave policy, so that now returns ``None`` (system defaults).

    Both the legacy suffixed key and the plain key are accepted so existing
    production rows keep working; the suffixed row wins where both exist.
    """
    if org_id is None:
        return None
    try:
        rows = (
            db.query(models.AppSettings.key, models.AppSettings.value)
            .filter(
                models.AppSettings.organization_id == org_id,
                models.AppSettings.key.in_([f"{base_key}:{org_id}", base_key]),
                models.AppSettings.team_key.is_(None),   # org tier only
                models.AppSettings.user_id.is_(None),
            )
            .all()
        )
    except Exception:
        return None
    by_key = {k: v for k, v in rows}
    return by_key.get(f"{base_key}:{org_id}", by_key.get(base_key))


def _get_effective_policies(db: Session, org_id: Optional[int] = None) -> dict:
    """This organization's effective leave policy.

    ``LEAVE_POLICIES`` is the SYSTEM DEFAULT — a starting catalogue, not a
    product rule. Every field of every type is overridable per organization,
    including Compensatory Leave: that entry used to be force-overwritten AFTER
    the merge ("so an org override can't silently drop it"), which meant no
    tenant could configure comp-off at all. One customer's 2026-07 policy
    decision is not every customer's, so the override now wins like any other.
    Organizations that want the old behaviour simply keep the defaults.
    """
    policies = {lt: dict(p) for lt, p in LEAVE_POLICIES.items()}
    raw = _org_setting(db, org_id, "leave_policies")
    if raw:
        import json as _j
        try:
            overrides = _j.loads(raw)
            for lt, pol in overrides.items():
                if lt in policies:
                    policies[lt].update(pol)
                else:
                    policies[lt] = pol
        except Exception:
            pass
    return policies

def _disabled_leave_types(db: Session, org_id: Optional[int] = None) -> set:
    """Leave types this organization has switched off. Org-scoped — the two
    read paths used to match on the key string alone (see ``_org_setting``)."""
    raw = _org_setting(db, org_id, "disabled_leave_types")
    if not raw:
        return set()
    import json as _j
    try:
        data = _j.loads(raw)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _shared_pool_enabled(db: Session, org_id: Optional[int] = None) -> bool:
    """Does this organization pool Annual/Casual/Sick against one balance?

    A regional convention (it ships ON to match the seeded default), not a
    product rule — hence a per-organization row rather than a constant.
    """
    raw = _org_setting(db, org_id, "shared_pool_enabled")
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

def _shared_pool_used(user_id: int, policies: dict, db: Session,
                      exclude_leave_id: Optional[int] = None) -> float:
    """Sum days used across all leave types that draw from the Annual pool."""
    pool_types = ({lt for lt, p in policies.items() if p.get("shared_pool")}
                  | {leave_identity.pool_master_name(policies)})
    return sum(_compute_used_days(user_id, lt, db, exclude_leave_id=exclude_leave_id)
               for lt in pool_types)

def _request_day_cost(r) -> float:
    """How many days a single LeaveRequest row consumes from the balance.

    Half-day requests (is_half_day=True) always count as 0.5 regardless of
    the start/end date span — the apply handler already enforces from==to in
    that case. Full-day requests fall back to the stored leave_days column,
    or to a date-span computation for legacy rows that pre-date the column.
    Returns float so half-day rows surface correctly in the balance UI."""
    if getattr(r, "is_half_day", False):
        return 0.5
    stored = getattr(r, "leave_days", None)
    if stored is not None and stored > 0:
        return float(stored)
    return float(max(1, (r.to_date - r.from_date).days + 1))

def _compute_used_days(user_id: int, leave_type: str, db: Session,
                       exclude_leave_id: Optional[int] = None) -> float:
    """Sum leave days from non-rejected requests in the current LEAVE year.

    The leave year is the organization's (``leave.year_start_month``), not a
    hard-coded January-to-December calendar read off the UTC clock.

    Returns a float so half-day requests (0.5) surface correctly. Callers
    that previously expected ``int`` only used it in arithmetic and string
    formatting, both of which accept floats without change.

    ``exclude_leave_id`` leaves one request out — used when re-validating an
    edit, so the request being edited is not counted against itself."""
    _u = db.query(models.User).filter(models.User.id == user_id).first()
    year_start = leave_policy.leave_year_start(
        db, getattr(_u, "organization_id", None))
    q = db.query(models.LeaveRequest).filter(
        and_(
            models.LeaveRequest.user_id    == user_id,
            models.LeaveRequest.leave_type == leave_type,
            models.LeaveRequest.from_date  >= year_start,
            models.LeaveRequest.status.notin_(["rejected"]),
        )
    )
    if exclude_leave_id is not None:
        q = q.filter(models.LeaveRequest.id != exclude_leave_id)
    return sum(_request_day_cost(r) for r in q.all())


# ── Monthly limit (per leave type, organization-configured) ─────────────────
def _monthly_limit(policy: dict) -> Optional[float]:
    """The type's per-calendar-month day cap, or None when it has none."""
    raw = (policy or {}).get("monthly_limit")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _month_span(y: int, m: int):
    from datetime import date, timedelta
    start = date(y, m, 1)
    end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return start, end


def _days_by_month(db: Session, user, start, end, is_half: bool) -> dict:
    """{(year, month): working days} for a span — the same day cost the balance
    uses (weekends / holidays / off-days excluded), split at month boundaries."""
    if is_half:
        return {(start.year, start.month): 0.5}
    out, y, m = {}, start.year, start.month
    while (y, m) <= (end.year, end.month):
        ms, me = _month_span(y, m)
        out[(y, m)] = leave_policy.leave_day_cost(db, user, max(start, ms), min(end, me))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_usage(db: Session, user, leave_type: str, y: int, m: int,
                 exclude_leave_id: Optional[int] = None) -> float:
    """Days of `leave_type` this employee already has (non-rejected) inside
    month (y, m) — only the part of each request that falls in that month."""
    ms, me = _month_span(y, m)
    q = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id    == user.id,
        models.LeaveRequest.leave_type == leave_type,
        models.LeaveRequest.status.notin_(["rejected"]),
        models.LeaveRequest.from_date  <= datetime(me.year, me.month, me.day, 23, 59, 59),
        models.LeaveRequest.to_date    >= datetime(ms.year, ms.month, ms.day),
    )
    if exclude_leave_id is not None:
        q = q.filter(models.LeaveRequest.id != exclude_leave_id)
    total = 0.0
    for r in q.all():
        f, t = r.from_date.date(), r.to_date.date()
        if getattr(r, "is_half_day", False):
            total += 0.5 if ms <= f <= me else 0.0
        else:
            total += _days_by_month(db, user, f, t, False).get((y, m), 0.0)
    return total

def _accrued_days(leave_type: str, policy: dict = None, db: Session = None,
                  org_id=None) -> float:
    """Days accrued so far this year based on months elapsed.

    Returns the running accrual; the caller decides whether to expose it as
    `accrued` (informational) or treat it as a real cap. The annual entitlement
    is `policy["limit"]` — accrual just paces how much of it is usable now.
    Unlimited / earned-balance types short-circuit to the policy limit because
    they don't have a monthly accrual schedule."""
    if policy is None:
        policy = LEAVE_POLICIES.get(leave_type, {})
    if policy.get("unlimited") or policy.get("earned"):
        return float(policy.get("limit", 0))
    limit   = policy.get("limit", 0)
    accrual = policy.get("accrual", 0)
    if accrual == 0:
        return float(limit)
    # Months into THIS organization's leave year. Using the calendar month
    # number assumed a January start, so an April-March tenant credited a full
    # year's accrual in December and reset to one month's worth in January.
    months_elapsed = (
        leave_policy.months_into_leave_year(db, org_id)
        if db is not None else datetime.now(timezone.utc).month
    )
    return min(float(limit), round(accrual * months_elapsed, 2))


# ──────────────────────────────────────────────────────────────────────────────
# PER-EMPLOYEE ALLOCATION OVERRIDES (Leave Balance Management module)
# HR can override the `allocated` side of `remaining = allocated - used` for one
# (employee, leave_type). The override row in EmployeeLeaveBalance is the single
# source of truth; `used` always stays derived from LeaveRequest, so a grant can
# never erase days already taken. These two helpers are consumed by BOTH
# get_balance() and _run_validation() so the displayed balance and the apply-time
# gate can never diverge.
# ──────────────────────────────────────────────────────────────────────────────
def _get_balance_overrides(user_id: int, db: Session, org_id=None) -> dict:
    """Batch-load this user's allocation overrides → {leave_type: allocated_days}.

    One SELECT, bucketed in Python — mirrors the N+1-avoiding `_used_by_type`
    pattern in get_balance() so callers can resolve every type without a query
    per type. org_id filters when provided so an override can't bleed across
    tenants (the unique key is (user_id, leave_type), but a user's org can
    change, so we still scope reads)."""
    q = db.query(models.EmployeeLeaveBalance).filter(
        models.EmployeeLeaveBalance.user_id == user_id
    )
    if org_id is not None:
        q = q.filter(models.EmployeeLeaveBalance.organization_id == org_id)
    return {row.leave_type: float(row.allocated_days) for row in q.all()}


def _annual_base_entitlement(user, policy, db, org_id=None) -> float:
    """The un-overridden base entitlement for one employee and leave type.

    For an ACCRUAL-BASED type (Annual Leave — ``accrual > 0``) whose holder has
    a recorded joining date, this is the joining-date-prorated entitlement for
    the organization's current leave year (``leave_policy.prorated_annual_
    entitlement``), capped at ``policy["limit"]``. For every other case it is the
    flat ``policy["limit"]`` exactly as before:

      • a non-accrual type (Maternity, Bereavement, Half Day, …) — those are
        per-event or fixed caps, not annual accrual, so they are never prorated;
      • a null ``official_start_date`` — pre-feature / unknown-joining-date
        workers are grandfathered to the full entitlement, the same rule the
        leave-eligibility gate already applies;
      • no user object available — the safe fallback preserves prior behaviour.

    ``is_active`` is deliberately NOT consulted here: the only endpoint that
    enumerates employees (``list_employee_balances``) already filters
    ``is_active == True``, so proration inherits that scope without introducing a
    second inactive-user rule.
    """
    limit = float(policy.get("limit", 0) or 0)
    rate  = float(policy.get("accrual", 0) or 0)
    start = getattr(user, "official_start_date", None) if user is not None else None
    if rate <= 0 or start is None:
        return limit
    year_start = leave_policy.leave_year_start(db, org_id)
    return leave_policy.prorated_annual_entitlement(
        start=start, leave_year_start_dt=year_start,
        monthly_rate=rate, annual_cap=limit,
    )


def _effective_allocation(user_id, leave_type, policy, db, overrides_map=None,
                          org_id=None, user=None):
    """Return (allocated_days, is_override) — the `allocated` side of the balance.

      • override present → (override value, True)
      • no override      → (base entitlement from _annual_base_entitlement, False)

    The no-override base is the employee's joining-date-prorated Annual
    entitlement (or the flat rulebook cap for fixed types / unknown joining
    dates) — so HR sees and adjusts a "prorated / used / remaining" that matches
    the spec, and a mid-year joiner is no longer shown the full 15. Pass the
    ``user`` object so the joining date can be resolved; when it is omitted the
    base falls back to the flat limit (prior behaviour, never over-grants).

    Pass `overrides_map` (from _get_balance_overrides) to avoid a per-type query
    inside loops; omit it for a one-off single-type lookup and it self-loads."""
    if overrides_map is None:
        overrides_map = _get_balance_overrides(user_id, db, org_id)
    if leave_type in overrides_map:
        return float(overrides_map[leave_type]), True
    return _annual_base_entitlement(user, policy, db, org_id), False


def _get_blackout_dates(db: Session, from_dt, to_dt, org_id=None) -> list:
    """Return blackout date strings (YYYY-MM-DD) that fall in [from_dt, to_dt].

    Configured via AppSettings key 'blackout_dates' — JSON list of ISO dates.
    Scoped to org_id so one tenant's blackout calendar cannot block another's.
    """
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == "blackout_dates",
    ).first()
    if not row or not row.value:
        return []
    import json as _j
    try:
        dates = _j.loads(row.value) or []
    except Exception:
        return []
    hits = []
    for d in dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            if from_dt <= dt <= to_dt:
                hits.append(d)
        except Exception:
            continue
    return hits


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE ELIGIBILITY — Start Date + Tenant-configurable Waiting Period
# Workers can apply for paid leave only after `official_start_date + N months`.
# N defaults to 6 months (Temveris business rule) and is tenant-configurable
# via the AppSettings key `leave_waiting_period_months`.
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_LEAVE_WAITING_PERIOD_MONTHS = 6
_LEAVE_WAITING_KEY = "leave_waiting_period_months"

# Leave types NOT gated by the new-joiner paid-leave waiting period. The waiting
# period only restricts PAID, ACCRUED leave (Annual/Sick/etc.). Compensatory
# Leave is EARNED by working extra (weekends/holidays/overtime) — not tenure-
# based — and Unpaid Leave (LWP) involves no paid balance, so both must be
# available from day one, even inside the waiting window.
_WAITING_PERIOD_EXEMPT_TYPES = {"Compensatory Leave", "Unpaid Leave"}


def _is_waiting_exempt(leave_type: str, policies: dict) -> bool:
    """Is this type available before the new-joiner waiting period is served?

    ONE definition, used by both readers. GET /leave/types already honoured the
    per-type `waiting_exempt` policy flag while POST /leave/apply did not, so an
    organization that set the flag got a form that let the employee fill it in
    and an endpoint that answered 403 — the worst of both. The frontend banner
    and Submit gate both key off what /leave/types reports, so the two answers
    have to come from the same place.

    Exempt when the type is:
      • this org's unpaid bucket — no paid balance is involved at all;
      • `earned` (comp-off) — earned by working, not by tenure;
      • flagged `waiting_exempt` — Sick and Emergency by default, and editable
        per type per organization;
      • one of the legacy hardcoded names, kept so a tenant that has neither
        role flags nor policy overrides behaves exactly as before.
    """
    pol = (policies or {}).get(leave_type) or {}
    return bool(
        leave_identity.is_unpaid(leave_type, pol, policies)
        or pol.get("earned")
        or pol.get("waiting_exempt")
        or leave_type in _WAITING_PERIOD_EXEMPT_TYPES
    )


def _get_leave_waiting_period_months(db: Session, org_id: Optional[int] = None) -> int:
    """Read the tenant's configured waiting period from AppSettings.

    Falls back to DEFAULT_LEAVE_WAITING_PERIOD_MONTHS if the row is missing,
    blank, or unparseable so a fresh tenant gets the documented business rule
    automatically without having to seed the table.
    """
    raw = _org_setting(db, org_id, _LEAVE_WAITING_KEY)
    if raw is None:
        return DEFAULT_LEAVE_WAITING_PERIOD_MONTHS
    try:
        n = int(str(raw).strip())
        return max(0, min(36, n))
    except (TypeError, ValueError):
        return DEFAULT_LEAVE_WAITING_PERIOD_MONTHS


def _add_months(d: datetime, months: int) -> datetime:
    """Add `months` calendar months to a datetime, clamping end-of-month.

    e.g. Jan 31 + 1 month → Feb 28 (or 29 in leap year). No dateutil dep.
    """
    import calendar
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    day = min(d.day, last_day)
    return d.replace(year=y, month=m, day=day)


def _compute_leave_eligibility(user: "models.User", db: Session) -> dict:
    """Return the worker's leave eligibility status as a structured dict.

    Grandfathering rule (per product): if `official_start_date` is null we
    assume the worker pre-dates this feature and treat them as eligible —
    otherwise the rule would brick the entire org's leave flow on day one.
    """
    waiting_months = _get_leave_waiting_period_months(db, getattr(user, "organization_id", None))
    start = getattr(user, "official_start_date", None)
    # This decides an HTTP 403 at apply time, so it is a business gate and runs
    # on the organization's calendar day.
    _today_d = _tz.local_date(db, getattr(user, "organization_id", None))
    today = datetime(_today_d.year, _today_d.month, _today_d.day, tzinfo=timezone.utc)

    if start is None:
        return {
            "eligible":             True,
            "start_date":           None,
            "waiting_period_months": waiting_months,
            "eligible_from":        None,
            "days_remaining":       0,
            "grandfathered":        True,
        }

    # Coerce to UTC-aware so we can compare against `today` without
    # the "naive vs aware" TypeError the surrounding code already hits.
    start_aware = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
    eligible_from = _add_months(start_aware, waiting_months)
    eligible = today >= eligible_from
    return {
        "eligible":             eligible,
        "start_date":           start_aware.date().isoformat(),
        "waiting_period_months": waiting_months,
        "eligible_from":        eligible_from.date().isoformat(),
        "days_remaining":       max(0, (eligible_from - today).days),
        "grandfathered":        False,
    }


def _assess_team_coverage(db: Session, user: "models.User", from_dt, to_dt) -> str:
    """Return a human-readable sentence on team coverage during the leave window."""
    if not getattr(user, "department", None):
        return "Team coverage: department not set — please review staffing in Temveris."
    teammates = db.query(models.User).filter(
        models.User.organization_id == user.organization_id,
        models.User.department == user.department,
        models.User.id != user.id,
    ).all()
    total = len(teammates)
    if total == 0:
        return "The employee has no teammates in the same department — coverage must be verified manually."
    ids = [t.id for t in teammates]
    overlapping = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id.in_(ids),
        models.LeaveRequest.status.in_(["approved", "under_review", "submitted"]),
        models.LeaveRequest.from_date <= to_dt,
        models.LeaveRequest.to_date   >= from_dt,
    ).count()
    available = total - overlapping
    if available <= 0:
        return f"⚠️ <strong>Coverage risk:</strong> all {total} teammate(s) are also on leave — work may not be covered."
    if available < max(1, total // 2):
        return f"⚠️ Limited coverage: only {available} of {total} teammate(s) available during this period."
    return f"The team has enough staff available ({available} of {total} teammates on duty) — work will be covered."


def _parse_worked_dates(raw):
    """Decode the stored worked_dates JSON column into a list of date strings.

    Returns [] for NULL / blank / malformed values so serializers never crash
    on a legacy or hand-edited row.
    """
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(d) for d in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _run_validation(user_id, leave_type, from_dt, to_dt, has_attachment, db, org_id=None,
                    is_half_day=False, exclude_leave_id=None):
    """
    Returns (errors, flags, auto_lwp).
    errors  – list of blocking messages shown to employee
    flags   – list of non-blocking flags: HR_REVIEW, DOC_REQUIRED, PATTERN_DETECTED
    auto_lwp – True if leave should be auto-converted to LWP
    is_half_day – per-request half-day intent (any single-day request the user
                  tagged as half-day), so the balance cost here matches what the
                  apply handler actually stores (0.5).
    exclude_leave_id – when re-validating an EDIT, the request being edited, so
                  its own days are not counted against its new values.
    """
    errors   = []
    flags    = []
    auto_lwp = False

    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=timezone.utc)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=timezone.utc)

    # Advance-notice is measured from the organization's today. Measuring it
    # from UTC midnight moved the boundary by up to a day for tenants far from
    # UTC, and this gate hard-blocks with HTTP 422.
    _today_d   = _tz.local_date(db, org_id)
    today      = datetime(_today_d.year, _today_d.month, _today_d.day, tzinfo=timezone.utc)
    notice_days = (from_dt - today).days
    policies    = _get_effective_policies(db, org_id)
    policy      = policies.get(leave_type, {})
    pool_on     = _shared_pool_enabled(db, org_id)
    # Loaded here rather than at the sick-leave check further down because the
    # day-cost calculation below needs the employee's resolved schedule.
    _requester  = db.query(models.User).filter(models.User.id == user_id).first()
    # Half-day requests cost 0.5 — both the dedicated "Half Day Leave" type
    # (half_day_only policy) AND any single-day request the caller tagged as
    # is_half_day. Using 1 here would falsely trip INSUFFICIENT_BALANCE for an
    # employee with exactly 0.5 days remaining (and misreport "Requested 1").
    #
    # Full-day spans are costed by leave_policy.leave_day_cost, which charges
    # only the days this employee would actually have worked — public holidays,
    # weekends and roster off-days inside the span are not deducted. The apply
    # handler calls the same function, so the balance checked here is the
    # balance charged.
    _is_half = bool(policy.get("half_day_only") or is_half_day)
    leave_days  = (
        0.5 if _is_half
        else (
            leave_policy.leave_day_cost(db, _requester, from_dt.date(), to_dt.date())
            if _requester is not None
            else float(max(1, (to_dt - from_dt).days + 1))
        )
    )

    # 0. A full-day request whose span contains no day this employee works
    #    (weekend, public holiday, roster off-day) costs 0, which slipped past
    #    every balance check below — including an HR allocation of 0 — and was
    #    then counted as one calendar day once stored. No leave is needed for
    #    such dates, so the request is refused.
    if not _is_half and leave_days <= 0:
        errors.append(
            "The selected dates are all non-working days (weekend, public holiday "
            "or scheduled day off). No leave is needed for these dates."
        )
        flags.append("NO_WORKING_DAYS")

    # 1. Notice period — hard-block for ANY leave type that defines a notice
    # window. Previously only `hard_notice=True` types (Annual/Casual/
    # Compensatory/Unpaid) blocked, so Maternity/Paternity/Adoption/Child
    # Care/Parent Care/Medical Escort would silently let employees submit
    # inside the policy window. Per product: notice means notice — if a
    # type has notice_days > 0 the request must be that many days out.
    # Types that legitimately allow zero notice (Sick / Bereavement /
    # Emergency / Half Day / Company) keep notice_days=0 in the policy
    # table and are unaffected by this guard.
    min_notice = policy.get("notice_days", 0)
    if min_notice > 0 and notice_days < min_notice:
        errors.append(
            f"{leave_type} requires at least {min_notice} days advance notice. "
            f"Submissions inside the notice window are blocked — please apply "
            f"at least {min_notice} days before the start date."
        )
        flags.append("NOTICE_BLOCKED")

    # 2. Evidence gate — ENFORCED (Phase 5 decision).
    #
    # The product has always documented two different promises, and the
    # difference is the decision this phase makes explicit:
    #
    #   doc_required  — INFORMATIONAL. Drives the "📎 Document Required" badge
    #                   and the `doc_status` the reviewer sees. Never blocks;
    #                   HR can accept a request and chase the certificate.
    #   evidence_gate — BLOCKING. LEAVE_POLICIES's own comment says "Submit
    #                   must be blocked ... until a document is uploaded".
    #
    # Both were dead: the condition read `leave_type == "Medical Leave"`, a
    # type that exists in no catalogue, so it could never be true. The gate is
    # now what its own definition says it is. Organizations that do not want a
    # blocking gate clear `evidence_gate` on that type — it is editable per
    # type in Workspace Settings, so this is a control, not an imposition.
    if policy.get("evidence_gate") and not has_attachment:
        errors.append("Supporting document is required for this leave type.")
        flags.append("EVIDENCE_GATE")
        flags.append("DOC_REQUIRED")
    elif policy.get("doc_required") and not has_attachment:
        # Informational only — the reviewer is told, the employee is not stopped.
        flags.append("DOC_REQUIRED")

    # 3. Balance check
    # Which types skip the balance gate is a POLICY property, not a set of
    # English names. It was a literal `exempt_types` set, so a tenant that
    # renamed "Unpaid Leave" silently started balance-gating it, and a tenant
    # that added its own uncapped type could not exempt it at all.
    # `balance_exempt` carries exactly the membership that set had, so today's
    # behaviour is unchanged and it is now configurable per organization.
    uncapped      = bool(policy.get("unlimited") or policy.get("no_annual_limit")
                         or policy.get("earned"))
    exempt        = uncapped or bool(policy.get("balance_exempt"))
    overrides_map = _get_balance_overrides(user_id, db, org_id)
    has_override  = leave_type in overrides_map

    if has_override and not uncapped:
        # Explicit per-employee allocation set by HR → HARD BLOCK on shortfall
        # (no auto-LWP). remaining = allocated - used; used stays derived.
        allocated = float(overrides_map[leave_type])
        used      = _compute_used_days(user_id, leave_type, db, exclude_leave_id=exclude_leave_id)
        remaining = max(0.0, allocated - used)
        if leave_days > remaining:
            errors.append(
                f"Insufficient leave balance. Available {remaining:g}, Requested {leave_days:g}"
            )
            flags.append("INSUFFICIENT_BALANCE")
    elif not exempt:
        if pool_on and (leave_identity.is_pool_master(leave_type, policy, policies)
                        or policy.get("shared_pool")):
            # Shared pool: pool types draw from the Annual master balance (sized
            # by the FULL Annual entitlement or this employee's override), but
            # each type is ALSO capped at its own entitlement — mirroring the
            # min() in _compute_user_balances so the enforced limit matches the
            # displayed per-type remaining (otherwise a type could be applied
            # beyond its own cap, up to the whole pool).
            _master        = leave_identity.pool_master_name(policies)
            annual_policy  = policies.get(_master, {})
            pool_total     = _effective_allocation(
                user_id, _master, annual_policy, db,
                overrides_map=overrides_map, org_id=org_id, user=_requester)[0]
            used_pool      = _shared_pool_used(user_id, policies, db, exclude_leave_id=exclude_leave_id)
            pool_remaining = max(0.0, pool_total - used_pool)
            type_used      = _compute_used_days(user_id, leave_type, db, exclude_leave_id=exclude_leave_id)
            type_remaining = max(0.0, float(policy.get("limit", 0)) - type_used)
            remaining      = min(type_remaining, pool_remaining)
            if leave_days > remaining:
                auto_lwp = True
                flags.append("INSUFFICIENT_BALANCE")
                if leave_days > pool_remaining:
                    flags.append("SHARED_POOL_EXHAUSTED")
        else:
            # No override → allocation is the joining-date-prorated annual
            # entitlement for accrual types (flat cap for everything else).
            allocated = _annual_base_entitlement(_requester, policy, db, org_id)
            used      = _compute_used_days(user_id, leave_type, db, exclude_leave_id=exclude_leave_id)
            remaining = max(0.0, allocated - used)
            if leave_days > remaining:
                auto_lwp = True
                flags.append("INSUFFICIENT_BALANCE")

    # 3b. Monthly limit — an organization-configured cap on how many days of
    #     this type may be taken in one calendar month (e.g. Compensatory Leave:
    #     2). Counted per month the leave days fall in, half days as 0.5, and
    #     applied to every type that sets it — including uncapped / earned types,
    #     which the balance check above deliberately skips.
    monthly_limit = _monthly_limit(policy)
    if monthly_limit is not None and _requester is not None:
        for (y, m), new_days in _days_by_month(
                db, _requester, from_dt.date(), to_dt.date(), _is_half).items():
            if new_days <= 0:
                continue
            already = _month_usage(db, _requester, leave_type, y, m, exclude_leave_id)
            if already + new_days > monthly_limit + 1e-9:
                errors.append(
                    f"{leave_type} is limited to {monthly_limit:g} day(s) per month. "
                    f"{datetime(y, m, 1):%B %Y}: {already:g} day(s) already used or "
                    f"pending, this request adds {new_days:g}."
                )
                flags.append("MONTHLY_LIMIT")
                break

    # 4. Sick Leave frequency check → HR review flag. The threshold is the
    #    organization's, not a fixed 2. `_requester` is loaded at the top of the
    #    function, where the day-cost calculation first needs it.
    sick_threshold = leave_policy.sick_review_threshold(db, _requester) if _requester else 0
    if leave_identity.is_sick(leave_type, policy, policies) and sick_threshold > 0:
        month_start = from_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        sl_q = db.query(models.LeaveRequest).filter(
            and_(
                models.LeaveRequest.user_id    == user_id,
                models.LeaveRequest.leave_type == leave_type,
                models.LeaveRequest.from_date  >= month_start,
                models.LeaveRequest.status.notin_(["rejected"]),
            )
        )
        if exclude_leave_id is not None:
            sl_q = sl_q.filter(models.LeaveRequest.id != exclude_leave_id)
        sl_count = sl_q.count()
        if sl_count >= sick_threshold:
            flags.append("HR_REVIEW")

    # 5. Long-weekend pattern → HR review flag. This was `weekday() == 0 or
    #    weekday() == 4`, which only means anything for a Mon-Fri week; it is
    #    now "the leave abuts a day this employee doesn't work", resolved from
    #    their configured working days and holiday calendar.
    if _requester is not None and leave_policy.flags_adjacent_leave(db, _requester):
        if leave_policy.abuts_non_working_day(db, _requester, from_dt.date(), to_dt.date()):
            flags.append("PATTERN_DETECTED")
            flags.append("HR_REVIEW")

    return errors, list(set(flags)), auto_lwp


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE TYPES  GET /leave/types
# Returns all leave types with full descriptions, eligibility, docs, T&C
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/types")
def get_leave_types(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Return all active leave types with full policy details."""
    org_id   = user.organization_id
    policies = _get_effective_policies(db, org_id)

    # Check for disabled types
    disabled_types = _disabled_leave_types(db, org_id)

    pool_on = _shared_pool_enabled(db, org_id)
    result = []
    for lt, pol in policies.items():
        if lt in disabled_types:
            continue
        details = LEAVE_TYPE_DETAILS.get(lt, {})
        result.append({
            "leave_type":      lt,
            "icon":            details.get("icon", "📋"),
            "description":     details.get("description", ""),
            "eligibility":     details.get("eligibility", "All confirmed employees"),
            "max_duration":    details.get("max_duration", f"{pol.get('limit', 0)} days per year"),
            "required_docs":   details.get("required_docs", "None" if not pol.get("doc_required") else "Supporting document required"),
            "terms":           details.get("terms", []),
            "annual_limit":    pol.get("limit", 0),
            "notice_days":     pol.get("notice_days", 0),
            "doc_required":    pol.get("doc_required", False),
            # Upload contract for the Apply Leave dropzone. Published per type
            # (rather than from a separate config endpoint) so the form can read
            # it off `selectedTypeInfo` it already has, and so the advertised
            # limit is always the limit /leave/apply actually enforces.
            "doc_max_mb":      MAX_FILE_MB,
            "doc_accept":      DOC_ACCEPT,
            "doc_formats":     list(DOC_FORMATS),
            "monthly_accrual": pol.get("accrual", 0.0),
            # Per-calendar-month day cap (None = no monthly limit).
            "monthly_limit":   _monthly_limit(pol),
            "shared_pool":     bool(pol.get("shared_pool")) and pool_on,
            "hard_notice":     bool(pol.get("hard_notice")),
            "evidence_gate":   bool(pol.get("evidence_gate")),
            # `is_unlimited` and `is_earned` drive the cap display so the
            # frontend never has to second-guess whether a numeric limit
            # actually represents "no cap" or "earned from overtime".
            "is_unlimited":    bool(pol.get("unlimited")),
            "is_earned":       bool(pol.get("earned")),
            "no_annual_limit":  bool(pol.get("no_annual_limit")),
            # Frontend reads this to switch the apply form into half-day mode
            # (auto-disable To Date, force the period selector). Driving this
            # off the policy avoids hardcoding the "Half Day Leave" string in
            # JSX, so renaming the type or adding a second half-day variant
            # later won't require a frontend change.
            "half_day_only":   bool(pol.get("half_day_only")),
            # Driven by the policy, not by a type NAME. `is_paid` compared
            # against the literal "Unpaid Leave", so a tenant that renamed its
            # unpaid bucket had it reported as paid.
            # Paid unless this organization's own unpaid type — resolved by
            # leave.identity, so a tenant that calls it "LWP" is answered
            # correctly. The legacy name still applies only while that tenant
            # has assigned no unpaid role (the migration bridge).
            "is_paid":         not leave_identity.is_unpaid(lt, pol, policies),
            # Exempt from the new-joiner waiting period. The rule is "earned or
            # unpaid leave is available from day one" — so it follows the
            # RESOLVED unpaid type, not the literal name. Before this, a tenant
            # whose unpaid bucket was "LWP" had its new joiners blocked from
            # leave the backend was otherwise happy to grant.
            "waiting_exempt":  _is_waiting_exempt(lt, policies),
        })
    return result


# ──────────────────────────────────────────────────────────────────────────────
# ADMIN: MANAGE LEAVE TYPES  POST/PUT/DELETE /leave/admin/types
# ──────────────────────────────────────────────────────────────────────────────
import json as _json
from app.commercial.enforcement import shadow_capability as _shadow

# Role flags an admin may set on a leave type. Kept in one tuple so the two
# write paths, the validator and the migration script cannot drift apart.
ROLE_FLAGS = ("pool_master", "is_unpaid", "is_sick")
WRITABLE_FLAGS = ROLE_FLAGS


def _assert_roles_unambiguous(policies: dict, disabled: set) -> None:
    """At most ONE enabled type may claim each role.

    Two pool masters is not a preference the system can honour — every balance
    read would have to pick one, and picking silently is exactly what Phase 5
    exists to stop. Disabled types are ignored: a tenant that switched off the
    shipped "Annual Leave" and flagged "PTO" instead is unambiguous, not in
    conflict.
    """
    for flag in ROLE_FLAGS:
        claimants = sorted(
            lt for lt, pol in (policies or {}).items()
            if pol.get(flag) and lt not in (disabled or set())
        )
        if len(claimants) > 1:
            raise HTTPException(
                400,
                f"Only one active leave type may be marked '{flag}'. "
                f"Currently claimed by: {', '.join(claimants)}. "
                f"Clear the flag on the others first.",
            )


def _apply_flags(target: dict, body, *, fields=WRITABLE_FLAGS) -> dict:
    """Copy only the flags the caller actually SENT.

    ``None`` means "not specified" and leaves the stored value alone — so a
    client that predates these fields cannot wipe a configured role simply by
    saving an unrelated change.
    """
    for f in fields:
        v = getattr(body, f, None)
        if v is not None:
            target[f] = bool(v)
    return target


class LeaveTypeCreate(BaseModel):
    leave_type:      str
    annual_limit:    int
    notice_days:     int = 0
    doc_required:    bool = False
    monthly_accrual: float = 0.0
    # Max days of this type per calendar month; None leaves any stored value
    # untouched, 0 removes the limit.
    monthly_limit:   Optional[float] = None
    shared_pool:     bool = False
    hard_notice:     bool = False
    evidence_gate:   bool = False
    # ROLE FLAGS (Phase 5). Which of this organization's types plays each
    # structural role — the question app/leave/identity.py isolates. They are
    # explicit rather than inferred: a tenant that calls its vacation bucket
    # "PTO" says so here, instead of the code guessing from the name, the
    # largest entitlement or catalogue order.
    pool_master:     Optional[bool] = None
    is_unpaid:       Optional[bool] = None
    is_sick:         Optional[bool] = None
    # NOTE: there is no separate "block" flag — `evidence_gate` is it. Two
    # flags meaning the same thing is how the first one came to be ignored.
    description:     Optional[str] = ""
    eligibility:     Optional[str] = ""
    max_duration:    Optional[str] = ""
    required_docs:   Optional[str] = ""
    terms:           Optional[List[str]] = []

@router.post("/admin/types")
def create_leave_type(
    body:  LeaveTypeCreate,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can manage leave types")

    org_id      = admin.organization_id
    policy_key  = f"leave_policies:{org_id}"  if org_id is not None else "leave_policies"
    details_key = f"leave_type_details:{org_id}" if org_id is not None else "leave_type_details"

    # Save policy config
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == policy_key,
    ).first()
    policies = {}
    if row and row.value:
        try: policies = _json.loads(row.value)
        except: pass

    # Preserve anything already stored for this type that the request does not
    # carry (role flags set earlier, fields a future client adds).
    existing = dict(policies.get(body.leave_type) or {})
    existing.update({
        "limit":         body.annual_limit,
        "notice_days":   body.notice_days,
        "doc_required":  body.doc_required,
        "accrual":       body.monthly_accrual,
        "shared_pool":   body.shared_pool,
        "hard_notice":   body.hard_notice,
        "evidence_gate": body.evidence_gate,
    })
    if body.monthly_limit is not None:
        if body.monthly_limit < 0 or body.monthly_limit > 31:
            raise HTTPException(400, "monthly_limit must be between 0 and 31 days")
        if body.monthly_limit > 0:
            existing["monthly_limit"] = body.monthly_limit
        else:
            existing.pop("monthly_limit", None)
    policies[body.leave_type] = _apply_flags(existing, body)
    # A role must be unambiguous across the org's EFFECTIVE catalogue, not just
    # the override blob — a shipped type can hold the flag too.
    _effective_for_check = _get_effective_policies(db, org_id)
    _effective_for_check[body.leave_type] = policies[body.leave_type]
    _assert_roles_unambiguous(_effective_for_check, _disabled_leave_types(db, org_id))
    if row:
        row.value           = _json.dumps(policies)
        row.organization_id = org_id
        row.updated_by      = admin.id
        row.updated_at      = datetime.now(timezone.utc)
    else:
        db.add(models.AppSettings(
            organization_id = org_id,
            key             = policy_key,
            value           = _json.dumps(policies),
            description     = "Leave policy overrides (JSON)",
            updated_by      = admin.id,
        ))

    # Save details if provided
    if body.description or body.eligibility or body.terms:
        details_row = db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.key             == details_key,
        ).first()
        details = {}
        if details_row and details_row.value:
            try: details = _json.loads(details_row.value)
            except: pass
        details[body.leave_type] = {
            "description": body.description, "eligibility": body.eligibility,
            "max_duration": body.max_duration, "required_docs": body.required_docs,
            "terms": body.terms or [],
        }
        if details_row:
            details_row.value           = _json.dumps(details)
            details_row.organization_id = org_id
        else:
            db.add(models.AppSettings(
                organization_id = org_id,
                key             = details_key,
                value           = _json.dumps(details),
                description     = "Custom leave type details",
                updated_by      = admin.id,
            ))

    db.commit()
    return {"success": True, "leave_type": body.leave_type}


@router.delete("/admin/types/{leave_type_name}")
def disable_leave_type(
    leave_type_name: str,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can manage leave types")

    org_id  = admin.organization_id
    dis_key = f"disabled_leave_types:{org_id}" if org_id is not None else "disabled_leave_types"
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == dis_key,
    ).first()
    disabled = []
    if row and row.value:
        try: disabled = _json.loads(row.value)
        except: pass

    if leave_type_name not in disabled:
        disabled.append(leave_type_name)

    if row:
        row.value           = _json.dumps(disabled)
        row.organization_id = org_id
    else:
        db.add(models.AppSettings(
            organization_id = org_id,
            key             = dis_key,
            value           = _json.dumps(disabled),
            description     = "Disabled leave types",
            updated_by      = admin.id,
        ))
    db.commit()
    return {"success": True}


class MonthlyLimitBody(BaseModel):
    # Days per calendar month; null or 0 removes the limit.
    monthly_limit: Optional[float] = None


@router.put("/admin/types/{leave_type_name}/monthly-limit")
def set_leave_type_monthly_limit(
    leave_type_name: str,
    body:  MonthlyLimitBody,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """Set or clear ONE field — the per-month cap — on this organization's
    leave type, leaving every other rule of the type exactly as it was (the
    create form above rewrites limit/notice/accrual, so it is not used here)."""
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can manage leave types")
    org_id = admin.organization_id
    if leave_type_name not in _get_effective_policies(db, org_id):
        raise HTTPException(404, f"Unknown leave type '{leave_type_name}'")
    value = body.monthly_limit
    if value is not None and (value < 0 or value > 31):
        raise HTTPException(400, "monthly_limit must be between 0 and 31 days")

    policy_key = f"leave_policies:{org_id}" if org_id is not None else "leave_policies"
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == policy_key,
    ).first()
    policies = {}
    if row and row.value:
        try: policies = _json.loads(row.value)
        except Exception: policies = {}
    entry = dict(policies.get(leave_type_name) or {})
    previous = _monthly_limit(entry)
    if value:
        entry["monthly_limit"] = value
    else:
        entry.pop("monthly_limit", None)
    policies[leave_type_name] = entry
    if row:
        row.value      = _json.dumps(policies)
        row.updated_by = admin.id
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(models.AppSettings(
            organization_id = org_id, key = policy_key, value = _json.dumps(policies),
            description = "Leave policy overrides (JSON)", updated_by = admin.id,
        ))
    _log_audit(db, admin.id, "LeaveTypeMonthlyLimitSet", "LeavePolicy", None,
               {"leave_type": leave_type_name, "previous": previous,
                "new": value or None}, org_id=org_id)
    db.commit()
    return {"leave_type": leave_type_name, "monthly_limit": value or None}


@router.post("/admin/types/{leave_type_name}/enable")
def enable_leave_type(
    leave_type_name: str,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can manage leave types")

    org_id  = admin.organization_id
    dis_key = f"disabled_leave_types:{org_id}" if org_id is not None else "disabled_leave_types"
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == dis_key,
    ).first()
    disabled = []
    if row and row.value:
        try: disabled = _json.loads(row.value)
        except: pass

    if leave_type_name in disabled:
        disabled.remove(leave_type_name)
        if row:
            row.value           = _json.dumps(disabled)
            row.organization_id = org_id
    db.commit()
    return {"success": True}


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE BALANCE  GET /leave/balance
# ──────────────────────────────────────────────────────────────────────────────
_PENDING_STATES = ("submitted", "under_review")


def _balance_is_uncapped(row: dict) -> bool:
    """True for a balance row of a type with no annual limit (Unpaid, Company,
    Compensatory) — its allowance-minus-used is not a real remaining balance.
    Mirrors `uncapped` in _run_validation, which ignores HR overrides on these
    types, so an override does not make them capped here either."""
    row = row or {}
    return bool(row.get("is_unlimited") or row.get("no_annual_limit") or row.get("is_earned"))


def _compute_user_balances(user: models.User, db: Session) -> dict:
    """Per-leave-type balance dict for one user — same shape as GET /leave/balance.

    Extracted from the endpoint so the employee's own balance view and the HR
    Leave Balance Management module compute identical numbers from one code
    path (allocation override + derived `used`), and can never drift apart."""
    org_id   = user.organization_id
    policies = _get_effective_policies(db, org_id)

    # Get disabled types
    disabled_types = _disabled_leave_types(db, org_id)

    pool_on      = _shared_pool_enabled(db, org_id)
    _master      = leave_identity.pool_master_name(policies)
    annual_pol   = policies.get(_master, {})
    # Per-employee allocation overrides (Leave Balance Management). Load once;
    # _effective_allocation reads this map so we never issue a query per type.
    _overrides   = _get_balance_overrides(user.id, db, org_id)
    # The shared pool is sized by the FULL Annual entitlement (or this
    # employee's Annual override), not the month-paced accrual.
    pool_total   = _effective_allocation(user.id, _master, annual_pol, db,
                                         overrides_map=_overrides, org_id=org_id, user=user)[0]
    pool_used    = _shared_pool_used(user.id, policies, db)
    pool_remain  = max(0.0, pool_total - pool_used)

    # N+1 fix: previously the loop below called _compute_used_days() once PER
    # leave type, issuing ~8–12 SELECTs that each re-scan this user's leave rows.
    # Pull the year's non-rejected requests ONCE and bucket by type in Python —
    # identical filter to _compute_used_days(), so semantics are unchanged.
    _year_start = leave_policy.leave_year_start(db, org_id)
    _used_by_type: dict = {}
    # Awaiting a decision. Already INCLUDED in `used` (and so already deducted
    # from `remaining`); reported separately so a reviewer can tell decided
    # days from pending ones. Never changes `used` or `remaining`.
    _pending_by_type: dict = {}
    for _r in (
        db.query(models.LeaveRequest)
        .filter(
            models.LeaveRequest.user_id   == user.id,
            models.LeaveRequest.from_date >= _year_start,
            models.LeaveRequest.status.notin_(["rejected"]),
        )
        .all()
    ):
        _cost = _request_day_cost(_r)
        _used_by_type[_r.leave_type] = _used_by_type.get(_r.leave_type, 0.0) + _cost
        if _r.status in _PENDING_STATES:
            _pending_by_type[_r.leave_type] = _pending_by_type.get(_r.leave_type, 0.0) + _cost

    result = {}
    for lt, policy in policies.items():
        if lt in disabled_types:
            continue
        is_unlimited  = bool(policy.get("unlimited"))
        is_earned     = bool(policy.get("earned"))
        # Entitlement = the base this employee is granted for the leave year:
        # Annual Leave (accrual > 0) is prorated from the joining date so a
        # mid-year joiner gets < 15; fixed / per-event types (Emergency 6,
        # Maternity 182, …) and workers with no recorded joining date keep the
        # full rulebook cap, exactly as before. Basis for "X / total days" and
        # for `remaining`.
        entitlement   = _annual_base_entitlement(user, policy, db, org_id)
        type_used     = _used_by_type.get(lt, 0.0)   # was _compute_used_days() — pre-bucketed above (N+1 fix)

        # allocated = the per-employee override (if HR set one) else the full
        # annual entitlement. is_override drives the independent-balance path.
        allocated, is_override = _effective_allocation(
            user.id, lt, policy, db, overrides_map=_overrides, org_id=org_id, user=user)

        draws_from_pool = pool_on and (leave_identity.is_pool_master(lt, policy, policies)
                                       or policy.get("shared_pool"))
        if is_override:
            # An explicit override makes this type independent for this
            # employee: remaining = allocated - used, bypassing the shared-pool
            # clamp so a grant actually frees days the employee can apply for.
            remaining       = max(0.0, allocated - type_used)
            total_for_view  = allocated
            draws_from_pool = False
        elif draws_from_pool:
            # Shared pool semantics: deductions hit the Annual pool, but each
            # leave type still has its own visible cap (entitlement). Real
            # availability is the lesser of (type's own remaining) and the
            # remaining shared pool.
            type_remaining = max(0.0, entitlement - type_used)
            remaining      = min(type_remaining, pool_remain)
            total_for_view = entitlement
        else:
            remaining      = max(0.0, allocated - type_used)   # allocated == entitlement
            total_for_view = entitlement
        used_for_view = type_used

        result[lt] = {
            "total":          round(total_for_view, 1),
            "accrued":        round(allocated, 1),
            "used":           round(used_for_view, 1),
            "remaining":      round(remaining, 1),
            "doc_required":   policy.get("doc_required", False),
            "notice_days":    policy.get("notice_days", 0),
            "is_unlimited":   is_unlimited,
            "is_earned":      is_earned,
            "no_annual_limit": bool(policy.get("no_annual_limit")),
            "shared_pool":    draws_from_pool,
            "hard_notice":    bool(policy.get("hard_notice")),
            "evidence_gate":  bool(policy.get("evidence_gate")),
            # Leave Balance Management — additive, non-breaking fields.
            "allocated":      round(allocated, 1),
            "has_override":   is_override,
            # Subset of `used` still awaiting a decision (submitted/under review).
            "pending":        round(_pending_by_type.get(lt, 0.0), 1),
        }
    # NOTE: key is prefixed "_" so consumers can distinguish it from the
    # regular per-type entries. `display_name` is the user-facing label;
    # the UI must prefer it over rendering the raw key.
    result["_shared_pool"] = {
        "display_name": "Shared Pool",
        "enabled":   pool_on,
        "total":     round(pool_total, 1),
        "used":      round(pool_used, 1),
        "remaining": round(pool_remain, 1),
        # Which type sizes the pool — reported so the UI names the tenant's
              # own master rather than assuming the shipped one.
        "source":    _master,
        "members":   sorted([lt for lt, p in policies.items() if p.get("shared_pool")]),
    }
    return result


@router.get("/balance")
def get_balance(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return _compute_user_balances(user, db)


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE BALANCE MANAGEMENT  (HR per-employee allocation overrides)
# Admin-only endpoints behind Workspace Settings → Leave Balance Management.
# All are org-scoped and gated to admin / hr / super_admin. They never touch
# `used` (derived from leave_requests); they only set the `allocated` override.
# ──────────────────────────────────────────────────────────────────────────────
def _require_balance_admin(user: models.User) -> models.User:
    """Only Super Admin / HR / Org Admin may manage employee balances."""
    if (user.role or "").lower() not in ("admin", "hr", "super_admin"):
        raise HTTPException(403, "Admin or HR access required")
    return user


def _balance_status(remaining: float, allocated: float) -> str:
    """Color-coded status bucket for the UI badge (🟢 / 🟡 / 🔴)."""
    if remaining <= 0:
        return "no_balance"
    if allocated > 0 and remaining <= max(1.0, allocated * 0.25):
        return "low"
    return "healthy"


class BalanceAdjustment(BaseModel):
    user_id:         int
    leave_type:      str
    adjustment_type: str               # grant | deduct | reset | custom
    adjustment_value: float = 0.0      # ignored for reset
    reason:          Optional[str] = ""
    effective_date:  Optional[str] = None   # YYYY-MM-DD; defaults to today
    notify:          bool = True            # raise an in-app notification


@router.get("/admin/balances")
def list_employee_balances(
    # Default is the SHIPPED pool master. A tenant whose master is named
    # otherwise gets the right default once R1 is decided (leave/identity.py);
    # callers already pass an explicit type.
    leave_type: str = Query(leave_identity.LEGACY_POOL_MASTER),
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """One row per active employee in the org for the selected leave type, with
    allocated / used / remaining and whether HR has overridden it. Drives the
    Leave Balance Management table and its zero-balance dashboard card."""
    _require_balance_admin(admin)
    org_id   = admin.organization_id
    policies = _get_effective_policies(db, org_id)
    policy   = policies.get(leave_type)
    if policy is None:
        raise HTTPException(400, f"Unknown leave type '{leave_type}'")
    entitlement = float(policy.get("limit", 0))
    # Uncapped types (Compensatory / Company / Unpaid …) are never balance-gated,
    # so a 0 entitlement must NOT render as a red "No balance".
    uncapped    = bool(policy.get("unlimited") or policy.get("no_annual_limit") or policy.get("earned"))

    pool_on        = _shared_pool_enabled(db, org_id)
    draws_pool     = pool_on and (leave_identity.is_pool_master(leave_type, policy, policies)
                                  or policy.get("shared_pool"))
    _master        = leave_identity.pool_master_name(policies)
    pool_types     = ({lt for lt, p in policies.items() if p.get("shared_pool")}
                      | {_master})

    users = db.query(models.User).filter(
        models.User.organization_id == org_id,
        models.User.is_active == True,  # noqa: E712
    ).all()

    _year_start = leave_policy.leave_year_start(db, org_id)

    # Batch: overrides + used-days for THIS type, keyed by user.
    override_by_user = {
        r.user_id: (float(r.allocated_days), r.updated_at)
        for r in db.query(models.EmployeeLeaveBalance).filter(
            models.EmployeeLeaveBalance.organization_id == org_id,
            models.EmployeeLeaveBalance.leave_type == leave_type,
        ).all()
    }
    used_by_user: dict = {}
    for r in db.query(models.LeaveRequest).filter(
        models.LeaveRequest.organization_id == org_id,
        models.LeaveRequest.leave_type == leave_type,
        models.LeaveRequest.from_date >= _year_start,
        models.LeaveRequest.status.notin_(["rejected"]),
    ).all():
        used_by_user[r.user_id] = used_by_user.get(r.user_id, 0.0) + _request_day_cost(r)

    # Batch (only when the selected type draws from the pool): each user's pool
    # size (their Annual override or the Annual entitlement) and pool usage, so a
    # non-overridden pool type's remaining = min(type_remaining, pool_remaining),
    # matching _compute_user_balances / the employee view exactly.
    annual_ovr_by_user, pool_used_by_user = {}, {}
    if draws_pool:
        annual_ovr_by_user = {
            r.user_id: float(r.allocated_days)
            for r in db.query(models.EmployeeLeaveBalance).filter(
                models.EmployeeLeaveBalance.organization_id == org_id,
                models.EmployeeLeaveBalance.leave_type == _master,
            ).all()
        }
        for r in db.query(models.LeaveRequest).filter(
            models.LeaveRequest.organization_id == org_id,
            models.LeaveRequest.leave_type.in_(pool_types),
            models.LeaveRequest.from_date >= _year_start,
            models.LeaveRequest.status.notin_(["rejected"]),
        ).all():
            pool_used_by_user[r.user_id] = pool_used_by_user.get(r.user_id, 0.0) + _request_day_cost(r)

    rows = []
    for u in users:
        ov        = override_by_user.get(u.id)
        # Non-overridden Annual allocation is prorated per the employee's joining
        # date (flat cap for fixed types / unknown joining dates); overrides win.
        allocated = ov[0] if ov else _annual_base_entitlement(u, policy, db, org_id)
        used      = used_by_user.get(u.id, 0.0)
        if ov or not draws_pool:
            # An override detaches the type from the pool; non-pool types use
            # their own cap.
            remaining = max(0.0, allocated - used)
        else:
            pool_total_u   = (annual_ovr_by_user[u.id] if u.id in annual_ovr_by_user
                              else _annual_base_entitlement(u, policies.get(_master, {}), db, org_id))
            pool_remaining = max(0.0, pool_total_u - pool_used_by_user.get(u.id, 0.0))
            remaining      = min(max(0.0, allocated - used), pool_remaining)
        status = "unlimited" if (uncapped and not ov) else _balance_status(remaining, allocated)
        rows.append({
            "user_id":         u.id,
            "name":            u.name,
            "email":           u.email,
            "department":      getattr(u, "department", None),
            "allocated":       round(allocated, 1),
            "used":            round(used, 1),
            "remaining":       round(remaining, 1),
            "has_override":    ov is not None,
            "uncapped":        uncapped,
            "is_unlimited":    bool(policy.get("unlimited")),
            "is_earned":       bool(policy.get("earned")),
            "no_annual_limit": bool(policy.get("no_annual_limit")),
            "updated_at":      iso_z(ov[1]) if ov and ov[1] else None,
            "status":          status,
        })
    rows.sort(key=lambda x: (x["name"] or "").lower())
    return {
        "leave_type":  leave_type,
        "entitlement": round(entitlement, 1),
        "uncapped":    uncapped,
        "rows":        rows,
    }


@router.get("/admin/balances/{user_id}")
def get_employee_balances(
    user_id: int,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """All leave types with allocated / used / remaining for one employee —
    powers the 'select an employee' panel of the adjustment form."""
    _require_balance_admin(admin)
    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == admin.organization_id,
    ).first()
    if not target:
        raise HTTPException(404, "Employee not found")
    # The period the balances cover, for the review modal's "Policy Year".
    # Same boundary _compute_user_balances counts from. Display only, so a
    # failure here leaves it out rather than failing the balances.
    try:
        leave_year = leave_policy.describe_leave_year(
            leave_policy.leave_year_start(db, target.organization_id))
    except Exception:
        leave_year = None
    return {
        "user_id":    target.id,
        "name":       target.name,
        "email":      target.email,
        "department": getattr(target, "department", None),
        "balances":   _compute_user_balances(target, db),
        "leave_year": leave_year,
    }


@router.post("/admin/balances/adjust")
def adjust_employee_balance(
    body:  BalanceAdjustment,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """Grant / Deduct / Reset / Set-custom an employee's allocation for a type.

    Writes/updates the override row, appends an immutable adjustment record,
    logs an audit entry, and (optionally) raises an in-app notification — all in
    one transaction. `used` is never modified, so the math is always
    remaining = allocated - used."""
    _require_balance_admin(admin)
    org_id = admin.organization_id

    target = db.query(models.User).filter(
        models.User.id == body.user_id,
        models.User.organization_id == org_id,
    ).first()
    if not target:
        raise HTTPException(404, "Employee not found")

    policies = _get_effective_policies(db, org_id)
    policy   = policies.get(body.leave_type)
    if policy is None:
        raise HTTPException(400, f"Unknown leave type '{body.leave_type}'")

    atype = (body.adjustment_type or "").lower()
    if atype not in ("grant", "deduct", "reset", "custom"):
        raise HTTPException(400, "adjustment_type must be grant | deduct | reset | custom")
    val = float(body.adjustment_value or 0.0)
    if atype in ("grant", "deduct", "custom") and val < 0:
        raise HTTPException(400, "adjustment_value must be 0 or greater")

    # Look up the override by its UNIQUE key (user_id, leave_type) only — NOT
    # org-scoped. `target` is already confirmed to be in the actor's org above,
    # and the unique constraint is (user_id, leave_type), so filtering by org
    # here would miss a row whose organization_id is stale (e.g. the user moved
    # orgs) and then the insert below would hit a 500 IntegrityError. We refresh
    # organization_id on write so the row re-aligns to the user's current org.
    row = db.query(models.EmployeeLeaveBalance).filter(
        models.EmployeeLeaveBalance.user_id == body.user_id,
        models.EmployeeLeaveBalance.leave_type == body.leave_type,
    ).first()

    used = _compute_used_days(body.user_id, body.leave_type, db)
    prev = float(row.allocated_days) if row else None
    # Baseline for grant/deduct: the current override if one exists, else the
    # FULL annual entitlement (so "15 grant +5 → 20" matches the spec).
    base = prev if prev is not None else float(policy.get("limit", 0))

    now = datetime.now(timezone.utc)
    if atype == "reset":
        new_alloc = None
        if row:
            db.delete(row)
    else:
        if   atype == "custom":
            new_alloc = val
        elif atype == "grant":
            new_alloc = base + val
        else:  # deduct — floored at days already used so remaining can't go negative
            new_alloc = max(used, base - val)
        if row:
            row.allocated_days  = new_alloc
            row.organization_id = org_id   # re-align if the user changed orgs
            row.updated_at      = now
        else:
            row = models.EmployeeLeaveBalance(
                user_id=body.user_id, organization_id=org_id,
                leave_type=body.leave_type, allocated_days=new_alloc,
            )
            db.add(row)

    eff = now
    if body.effective_date:
        try:
            eff = datetime.strptime(body.effective_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "effective_date must be YYYY-MM-DD")

    db.add(models.LeaveBalanceAdjustment(
        user_id=body.user_id, organization_id=org_id, leave_type=body.leave_type,
        adjustment_type=atype, adjustment_value=(None if atype == "reset" else val),
        previous_allocation=prev, new_allocation=new_alloc,
        reason=(body.reason or "").strip() or None, effective_date=eff,
        created_by=admin.id,
    ))

    _log_audit(db, admin.id, "LeaveBalanceAdjusted", "EmployeeLeaveBalance", body.user_id,
               {"leave_type": body.leave_type, "adjustment_type": atype, "value": val,
                "previous": prev, "new": new_alloc, "reason": body.reason}, org_id=org_id)

    if body.notify:
        if new_alloc is None:
            body_txt = (f"Your {body.leave_type} allocation has been reset to the "
                        f"company default by {admin.name or 'HR'}.")
        else:
            body_txt = (f"Your {body.leave_type} allocation is now {new_alloc:g} days "
                        f"({max(0.0, (new_alloc or 0) - used):g} remaining).")
        db.add(models.AuditLog(
            actor_id=admin.id, target_id=body.user_id, organization_id=org_id,
            entity="Notification", action="LeaveBalanceAdjusted",
            payload={
                "title": "Leave balance updated",
                "body": body_txt,
                "category": "schedule",
                "channels": ["in_app"],
                "notification_type": "leave_balance_adjusted",
                "leave_type": body.leave_type,
                "reason": (body.reason or "").strip() or None,
                "action_url": "/dashboard?tab=leave",
            },
        ))

    db.commit()
    remaining = None if new_alloc is None else max(0.0, new_alloc - used)
    return {
        "success":             True,
        "user_id":             body.user_id,
        "leave_type":          body.leave_type,
        "adjustment_type":     atype,
        "previous_allocation": prev,
        "new_allocation":      new_alloc,
        "used":                round(used, 1),
        "remaining":           None if remaining is None else round(remaining, 1),
    }


@router.get("/admin/balances/{user_id}/history")
def get_balance_adjustment_history(
    user_id: int,
    leave_type: Optional[str] = Query(None),
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """Immutable adjustment trail for one employee (optionally one type),
    newest first. Joined to the actor for the 'Admin' column."""
    _require_balance_admin(admin)
    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == admin.organization_id,
    ).first()
    if not target:
        raise HTTPException(404, "Employee not found")

    q = db.query(models.LeaveBalanceAdjustment).filter(
        models.LeaveBalanceAdjustment.user_id == user_id,
        models.LeaveBalanceAdjustment.organization_id == admin.organization_id,
    )
    if leave_type:
        q = q.filter(models.LeaveBalanceAdjustment.leave_type == leave_type)
    adjustments = q.order_by(models.LeaveBalanceAdjustment.created_at.desc()).all()

    actor_ids = {a.created_by for a in adjustments if a.created_by}
    actors = {}
    if actor_ids:
        for u in db.query(models.User).filter(models.User.id.in_(actor_ids)).all():
            actors[u.id] = u.name
    return [
        {
            "id":                  a.id,
            "leave_type":          a.leave_type,
            "adjustment_type":     a.adjustment_type,
            "adjustment_value":    a.adjustment_value,
            "previous_allocation": a.previous_allocation,
            "new_allocation":      a.new_allocation,
            "reason":              a.reason,
            "effective_date":      a.effective_date.strftime("%Y-%m-%d") if a.effective_date else None,
            "created_by":          a.created_by,
            "created_by_name":     actors.get(a.created_by),
            "created_at":          iso_z(a.created_at) if a.created_at else None,
        }
        for a in adjustments
    ]


# ──────────────────────────────────────────────────────────────────────────────
# VALIDATE LEAVE REQUEST  POST /leave/validate
# ──────────────────────────────────────────────────────────────────────────────
class ValidateRequest(BaseModel):
    leave_type:     str
    from_date:      str
    to_date:        str
    has_attachment: bool = False
    is_half_day:    bool = False

@router.post("/validate")
def validate_leave(
    data: ValidateRequest,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    try:
        fd = datetime.strptime(data.from_date, "%Y-%m-%d")
        td = datetime.strptime(data.to_date,   "%Y-%m-%d")
    except ValueError:
        return {"errors": ["Invalid date format"], "flags": [], "auto_lwp": False}

    errors, flags, auto_lwp = _run_validation(
        user.id, data.leave_type, fd, td, data.has_attachment, db,
        org_id=user.organization_id, is_half_day=bool(data.is_half_day),
    )
    return {"errors": errors, "flags": flags, "auto_lwp": auto_lwp}


# ──────────────────────────────────────────────────────────────────────────────
# APPLY FOR LEAVE  POST /leave/apply
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/apply")
async def apply_leave(
    background_tasks: BackgroundTasks,
    leave_type:  str               = Form(...),
    from_date:   str               = Form(...),
    to_date:     str               = Form(...),
    description: str               = Form(...),
    reason:      str               = Form(""),
    is_half_day: str               = Form("false"),    # "true" / "false"
    half_day_period: str           = Form(""),         # "first_half" | "second_half"
    worked_dates: str              = Form(""),          # comp leave: JSON list of "YYYY-MM-DD"
    attachment:  Optional[UploadFile] = File(None),
    db:          Session           = Depends(get_db),
    user:        models.User       = Depends(get_current_user),
):
    if not description.strip():
        raise HTTPException(400, "Description is required")

    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date,   "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if td < fd:
        raise HTTPException(400, "to_date must be >= from_date")

    # ── Leave eligibility waiting period (per product spec) ─────────────────
    # Workers can apply for paid leave only after `official_start_date +
    # waiting_period_months`. Workers with no start date set are
    # grandfathered as eligible so this rule doesn't brick existing orgs.
    # Hard-block here (HTTP 403) — frontend's eligibility banner gives the
    # user enough warning before they ever reach this endpoint.
    # Which types bypass the period is _is_waiting_exempt's single definition,
    # shared with GET /leave/types. This used to inline its own version that
    # omitted the `waiting_exempt` flag, so the two disagreed.
    elig = _compute_leave_eligibility(user, db)
    _pol_all = _get_effective_policies(db, user.organization_id)
    _exempt = _is_waiting_exempt(leave_type, _pol_all)
    if not _exempt and not elig["eligible"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"You become eligible for paid leave on {elig['eligible_from']} "
                f"(after {elig['waiting_period_months']} months from your start date "
                f"{elig['start_date']}). Please try again then."
            ),
        )

    # ── Half-day validation ──────────────────────────────────────────────────
    # The dedicated "Half Day Leave" type ALWAYS counts as half-day regardless
    # of what the client sent in the is_half_day form field — flip the flag
    # server-side so a stale client can't bypass the 0.5-day deduction. For
    # any other leave type the per-request checkbox still drives the flag.
    half_day = (
        leave_type in HALF_DAY_ONLY_TYPES
        or str(is_half_day).strip().lower() in ("true", "1", "yes")
    )
    if half_day:
        # Half Day Leave is single-date by definition; auto-collapse the range
        # so a client that mistakenly posted to_date != from_date still works
        # instead of bouncing the request. Other leave types stay strict.
        if leave_type in HALF_DAY_ONLY_TYPES and fd != td:
            td = fd
        if fd != td:
            raise HTTPException(400, "Half-day leave is only valid for a single day (from_date must equal to_date)")
        if half_day_period not in ("first_half", "second_half"):
            raise HTTPException(400, "half_day_period must be 'first_half' or 'second_half'")
    else:
        half_day_period = None

    # ── Compensatory Leave: "dates already worked" justification ──────────────
    # Comp leave is earned by working overtime / weekends / public holidays, so
    # the employee lists those worked dates here and HR verifies the claim. We
    # require at least one date, each must be a valid YYYY-MM-DD that is not in
    # the future (you can only claim days you've already worked). Stored as a
    # JSON array on the request row; ignored for every other leave type.
    worked_dates_json = None
    # `earned` is the policy flag for "claimed against overtime already
    # worked" — it is what Compensatory Leave carries and what a tenant's
    # own comp-off type would carry. Unlike the pool/unpaid/sick questions
    # this needs no product decision: the metadata already exists.
    if (_get_effective_policies(db, user.organization_id)
            .get(leave_type, {}).get("earned")):
        raw = (worked_dates or "").strip()
        try:
            items = json.loads(raw) if raw else []
            if not isinstance(items, list):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            # Tolerate a plain comma-separated list too (older / hand-built clients).
            items = [d.strip() for d in raw.split(",") if d.strip()]
        today_d  = _tz.local_date(db, getattr(user, "organization_id", None))
        cleaned  = []
        seen     = set()
        for d in items:
            ds = str(d).strip()
            if not ds:
                continue
            try:
                wd = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(400, f"Worked date '{ds}' must be in YYYY-MM-DD format")
            if wd > today_d:
                raise HTTPException(400, f"Worked date {ds} is in the future — you can only claim days you have already worked")
            iso = wd.strftime("%Y-%m-%d")
            if iso not in seen:
                seen.add(iso)
                cleaned.append(iso)
        if not cleaned:
            raise HTTPException(400, "Please add at least one date you already worked to claim Compensatory Leave")
        worked_dates_json = json.dumps(cleaned)

    # Validate attachment
    attach_path = None
    attach_name = None
    if attachment and attachment.filename:
        if attachment.content_type not in ALLOWED_MIME:
            raise HTTPException(415, f"Only {', '.join(DOC_FORMATS)} files are allowed")
        chunk = await attachment.read(MAX_FILE_MB * 1024 * 1024 + 1)
        if len(chunk) > MAX_FILE_MB * 1024 * 1024:
            raise HTTPException(413, f"File exceeds the {MAX_FILE_MB} MB limit")
        if not chunk:
            raise HTTPException(400, "The uploaded document is empty")
        await attachment.seek(0)
        # Extension comes from the content type we just validated, not from the
        # client-supplied filename — see the MIME_EXT note at the top of this
        # module. The original filename is still kept, but only as display text
        # in `attachment_name`; it never reaches the filesystem.
        ext        = MIME_EXT[attachment.content_type]
        org_prefix = f"org_{user.organization_id}/" if user.organization_id else ""
        fname      = f"{org_prefix}{uuid.uuid4().hex}.{ext}"
        from app.storage import put_upload_file, ATTACHMENTS
        attach_path = put_upload_file(ATTACHMENTS, fname, attachment, attachment.content_type)
        attach_name = attachment.filename

    # Run policy validation. Pass the resolved half_day so the balance cost
    # matches what we store below (0.5), not a full day.
    errors, flags, auto_lwp = _run_validation(
        user.id, leave_type, fd, td, bool(attach_path), db,
        org_id=user.organization_id, is_half_day=half_day,
    )

    # Errors from _run_validation are already hard-blocking (notice guardrail +
    # Tier 0 evidence gate). Balance shortfalls are handled via auto_lwp only.
    if errors:
        raise HTTPException(422, {"errors": errors, "flags": flags})

    policies = _get_effective_policies(db, user.organization_id)
    policy   = policies.get(leave_type, {})

    # Day cost — half-day requests always 0.5; full-day spans charge only the
    # days this employee would actually have worked (public holidays, weekends
    # and roster off-days inside the span are not deducted). Same function the
    # validation gate above used, so the balance that was checked is the balance
    # that gets stored.
    leave_days  = leave_policy.leave_day_cost(db, user, fd, td, is_half_day=half_day)
    doc_status  = "submitted" if attach_path else ("pending" if policy.get("doc_required") else "not_required")
    hr_flag     = "HR_REVIEW" in flags
    actual_type = (
        leave_identity.unpaid_name(_get_effective_policies(db, user.organization_id))
        if auto_lwp else leave_type)

    # Mirror the half-day intent into the description prefix too — admin
    # views and email digests already render description verbatim, so this
    # keeps the human-readable surface intact for older consumers that
    # haven't been updated to read the new is_half_day column directly.
    if half_day:
        period_label = "First Half (Morning)" if half_day_period == "first_half" else "Second Half (Evening)"
        description_with_half = f"[Half-day · {period_label}] {description.strip()}"
    else:
        description_with_half = description.strip()

    req = models.LeaveRequest(
        user_id         = user.id,
        organization_id = user.organization_id,
        leave_type      = actual_type,
        from_date       = fd,
        to_date         = td,
        description     = description_with_half,
        reason          = reason or description.strip(),
        status          = "submitted",
        attachment_path = attach_path,
        attachment_name = attach_name,
        is_lwp          = auto_lwp,
        hr_flag         = hr_flag,
        doc_status      = doc_status,
        leave_days      = leave_days,
        is_half_day     = half_day,
        half_day_period = half_day_period if half_day else None,
        worked_dates    = worked_dates_json,
    )
    db.add(req)
    db.flush()

    _log_audit(db, user.id, "LeaveRequested", "LeaveRequest", req.id,
               {"type": actual_type, "from": from_date, "to": to_date,
                "auto_lwp": auto_lwp, "flags": flags}, org_id=user.organization_id)
    _emit_event(db, "LeaveRequested", str(req.id),
                {"leave_id": req.id, "user_id": user.id, "type": actual_type,
                 "employee_name": user.name})
    db.commit()
    db.refresh(req)

    # Build HR summary context for the admin/HR email. Use the SAME derived
    # balance the employee and the apply-gate see (full entitlement or HR
    # override, with the shared-pool clamp) instead of the old month-paced
    # accrual — otherwise HR approves against a different, lower figure.
    _bal_view   = _compute_user_balances(user, db).get(actual_type, {})
    used_b      = _bal_view.get("used", _compute_used_days(user.id, actual_type, db))
    remaining_b = _bal_view.get("remaining", 0.0)
    blackouts   = _get_blackout_dates(db, fd, td, org_id=user.organization_id)
    coverage    = _assess_team_coverage(db, user, fd, td)
    supervisor  = "Supervisor pre-approval: pending manager review — HR must only decide after seeing balance."
    doc_status_line = None
    if leave_identity.is_sick(
            actual_type,
            _get_effective_policies(db, user.organization_id).get(actual_type),
            _get_effective_policies(db, user.organization_id),
    ) and "SICK_UNEARNED" in flags:
        doc_status_line = (
            "Sick leave exceeds earned balance — medical documents "
            + ("have been uploaded" if attach_path else "MUST be uploaded by the employee")
            + " and unearned days will be auto-applied to salary as LWP."
        )

    background_tasks.add_task(
        send_leave_request_to_admin,
        recipients=_alert_recipients(db, user, "leave.request"),
        leave_id=req.id, employee_name=user.name, employee_email=user.email,
        leave_type=actual_type, from_date=from_date, to_date=to_date,
        description=description, reason=reason,
        balance_remaining=remaining_b, leave_days=leave_days,
        blackout_dates=blackouts, team_coverage=coverage,
        supervisor_status=supervisor, doc_status=doc_status_line,
        balance_unlimited=_balance_is_uncapped(_bal_view),
    )

    # ZT-LVE-01 worker confirmation (Operational; fail-open). Runs as a
    # background task so a mail failure can never block leave submission. No
    # reason/description is passed — the confirmation carries no sensitive detail.
    background_tasks.add_task(
        send_leave_submitted_to_employee,
        user.email, user.name, actual_type, from_date, to_date, leave_days,
    )

    # WebSocket broadcast for real-time admin notification — org-scoped
    try:
        from websocket_manager import manager
        import asyncio, json as _wsjson
        if user.organization_id:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(manager.broadcast_to_org(user.organization_id, _wsjson.dumps({
                    "type":            "new_leave_request",
                    "organization_id": user.organization_id,
                    "leave_id":        req.id,
                    "user_id":         user.id,
                    "employee_name":   user.name,
                    "leave_type":      actual_type,
                    "from_date":       from_date,
                    "to_date":         to_date,
                    "days":            leave_days,
                    "timestamp":       iso_z(datetime.now(timezone.utc)),
                })))
    except Exception:
        pass

    msg = "Leave request submitted"
    if auto_lwp:
        msg = "Leave submitted — converted to Unpaid Leave (LWP) due to insufficient balance"
    if hr_flag:
        msg += ". Flagged for HR review."

    return {
        "success":  True,
        "id":       req.id,
        "status":   req.status,
        "auto_lwp": auto_lwp,
        "hr_flag":  hr_flag,
        "flags":    flags,
        "message":  msg,
    }


# ──────────────────────────────────────────────────────────────────────────────
# GET ALL LEAVE REQUESTS  GET /leave/requests  (admin/hr)
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/requests")
def get_leave_requests(
    user_id: Optional[int] = Query(
        None, description="Restrict to one employee in YOUR organization."),
    db:      Session     = Depends(get_db),
    current: models.User = Depends(get_current_user),
):
    """Leave requests for the caller's organization, newest first.

    ``user_id`` narrows the result to one employee. It is HONOURED, not
    ignored: callers were already passing it and silently receiving the whole
    organization, which is a filter that looks like it works and does not. The
    id is tenant-checked first — an employee of another organization is a 404,
    never an empty 200 that could be mistaken for "they have no leave".
    """
    if current.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin or HR access required")
    # Org-scoped user list for multitenancy isolation.
    _leave_user_ids = [
        row.id for row in db.query(models.User.id)
        .filter(models.User.organization_id == current.organization_id)
        .all()
    ]
    if user_id is not None:
        if user_id not in _leave_user_ids:
            raise HTTPException(status_code=404, detail="Employee not found")
        _leave_user_ids = [user_id]
    reqs = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id.in_(_leave_user_ids)
    ).order_by(models.LeaveRequest.created_at.desc()).all()
    # Batch-load related users in a single query — avoids N+1 per request
    # (the cause of slow Leave Summary / Manage Leave load).
    user_ids = {r.user_id for r in reqs}
    users = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()}
        if user_ids else {}
    )
    result = []
    for r in reqs:
        u    = users.get(r.user_id)
        days = _request_day_cost(r)
        result.append({
            "id":              r.id,
            "user_id":         r.user_id,
            "name":            u.name if u else "Unknown",
            "email":           u.email if u else "",
            "department":      u.department if u else "",
            "leave_type":      r.leave_type,
            "from_date":       r.from_date.strftime("%Y-%m-%d"),
            "to_date":         r.to_date.strftime("%Y-%m-%d"),
            "days":            days,
            "description":     r.description,
            "reason":          r.reason or r.description,
            "status":          r.status,
            "review_comment":  r.review_comment,
            "attachment_path": r.attachment_path,
            "attachment_name": r.attachment_name,
            "has_attachment":  bool(r.attachment_path),
            "is_lwp":          getattr(r, "is_lwp",  False),
            "is_awol":         getattr(r, "is_awol", False),
            "hr_flag":         getattr(r, "hr_flag", False),
            "doc_status":      getattr(r, "doc_status", "not_required"),
            "worked_dates":    _parse_worked_dates(getattr(r, "worked_dates", None)),
            "created_at":      r.created_at.isoformat(),
        })
    return result


# ──────────────────────────────────────────────────────────────────────────────
# DEFENSIVE NOTIFY HELPER for leave-status changes.
#
# Why this exists: the previous code did
#     background_tasks.add_task(send_leave_status_to_employee, ...)
# which silently swallowed every exception (FastAPI BackgroundTasks log to
# stderr but never surface to the API or audit trail). Common silent failures
# we observed in prod:
#   • req.user.email is None / blank        → re.match(...) raises TypeError
#   • RESEND_API_KEY unset and SMTP blocked → _send returns False, caller
#                                             never knew the email didn't go
#   • req.from_date is None (ill-formed row) → strftime crashes the task
#
# Now every approval/rejection:
#   1. Always writes a Notification AuditLog row → the bell-icon inbox shows
#      it even if the email transport fails (this is the user-visible part).
#   2. Validates the email address before calling the transport.
#   3. Wraps the send in try/except so a network blip can't break the audit
#      trail and the admin response stays honest about delivery status.
#   4. Logs every failure with structured info to email_logs.txt + stderr.
# ──────────────────────────────────────────────────────────────────────────────
def _notify_leave_status(
    db: Session,
    user: models.User,
    req: models.LeaveRequest,
    status: str,
    comment: str,
) -> dict:
    """Send the leave decision email and record an in-app notification.

    Returns a dict {email_sent: bool, error: str | None, in_app: bool}.
    Caller MUST commit() after this — this helper only stages the AuditLog.
    """
    is_approved = (status or "").lower() == "approved"
    label = "Approved" if is_approved else "Rejected"

    # Format dates defensively — a missing date should never break the
    # notification. Fall back to the raw value or a placeholder.
    def _fmt(d, default="—"):
        try:
            return d.strftime("%Y-%m-%d") if d else default
        except Exception:
            return default

    from_str = _fmt(req.from_date)
    to_str   = _fmt(req.to_date)
    type_label = (req.leave_type or "leave").replace("_", " ").title()

    title = f"Leave {label}"
    body  = (
        f"Your {type_label} request from {from_str} to {to_str} has been "
        f"{'approved' if is_approved else 'rejected'}."
    )
    if comment:
        body += f" Comment: {comment}"

    email_sent = False
    error: Optional[str] = None

    if not user.email or not str(user.email).strip():
        error = "Employee has no email address on file"
        logger.warning(
            "[leave-notify] user_id=%s leave_id=%s — skipping email: %s",
            user.id, req.id, error,
        )
    else:
        try:
            email_sent = bool(send_leave_status_to_employee(
                user.email,
                user.name or "Employee",
                req.leave_type or "leave",
                from_str,
                to_str,
                status,
                comment or "",
            ))
            if not email_sent:
                error = "Email transport returned failure (see email_logs.txt for transport-level error)"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.error(
                "[leave-notify] Exception while sending leave email user_id=%s leave_id=%s: %s\n%s",
                user.id, req.id, error, traceback.format_exc(),
            )

    # Always create the in-app notification — the AuditLog table backs
    # /notifications/history so the employee always sees the decision in
    # the bell icon, even if email delivery failed.
    db.add(models.AuditLog(
        actor_id        = req.reviewed_by_id,
        target_id       = user.id,
        organization_id = user.organization_id,
        entity          = "Notification",
        action          = f"Leave{label}",
        payload         = {
            "title":             title,
            "body":              body,
            "category":          "schedule",
            "channel":           "email",
            "channels":          (["in_app", "email"] if email_sent else ["in_app"]),
            "delivery_status":   "delivered" if email_sent else "failed",
            "notification_type": f"leave_{status}",
            "action_url":        "/dashboard?tab=leave",
            "leave_id":          req.id,
            "leave_type":        req.leave_type,
            "from_date":         from_str,
            "to_date":           to_str,
            "status":            status,
            "comment":           comment or "",
            "error":             error,
        },
    ))

    return {"email_sent": email_sent, "error": error, "in_app": True}


# ──────────────────────────────────────────────────────────────────────────────
# UPDATE STATUS  PUT /leave/{id}/status  (admin/hr)
# ──────────────────────────────────────────────────────────────────────────────
class StatusUpdate(BaseModel):
    status:  str
    comment: Optional[str] = ""

@router.put("/{leave_id}/status")
def update_leave_status(
    leave_id:         int,
    body:             StatusUpdate,
    background_tasks: BackgroundTasks,
    db:               Session     = Depends(get_db),
    admin:            models.User = Depends(get_current_user),
):
    # SHADOW ONLY - placed BEFORE the RBAC check so the two stay visibly
    # separate: this records whether the WORKSPACE is entitled, the check
    # below decides whether this ACTOR may act.
    _shadow(db, admin, "leave.approvals",
            route="PUT /leave/{leave_id}/status", writable=True)
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admins/HR can update leave status")

    req = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.id == leave_id,
        models.LeaveRequest.organization_id == admin.organization_id,
    ).first()
    if not req:
        raise HTTPException(404, "Leave request not found")

    allowed_next = TRANSITIONS.get(req.status, [])
    if body.status not in allowed_next:
        raise HTTPException(400,
            f"Cannot transition from '{req.status}' to '{body.status}'. "
            f"Allowed: {allowed_next or 'none (terminal state)'}")

    req.status         = body.status
    req.reviewed_by_id = admin.id
    req.reviewed_at    = datetime.now(timezone.utc)
    req.review_comment = body.comment

    action_map = {
        "approved":           "LeaveApproved",
        "rejected":           "LeaveRejected",
        "under_review":       "LeaveUnderReview",
        "locked_for_payroll": "LeaveLockedForPayroll",
    }
    _log_audit(db, admin.id, action_map.get(body.status, "LeaveStatusChanged"),
               "LeaveRequest", req.id,
               {"to_status": body.status, "comment": body.comment}, org_id=admin.organization_id)
    _emit_event(db, action_map.get(body.status, "LeaveStatusChanged"), str(req.id),
                {"leave_id": req.id, "status": body.status, "reviewer": admin.id})
    db.commit()

    user = db.query(models.User).filter(models.User.id == req.user_id).first()
    notify_result: Optional[dict] = None
    if user and body.status in ("approved", "rejected"):
        # Run the notify helper synchronously: the email send itself takes
        # roughly 200–800ms (Resend API or SMTP), which is acceptable for an
        # admin-triggered approval, and the synchronous return lets us:
        #   • commit the in-app notification atomically with the status change
        #   • surface delivery failures in the API response so admins know
        #     immediately if the employee did NOT receive the email
        #   • avoid the silent-task failures we saw with BackgroundTasks
        try:
            notify_result = _notify_leave_status(db, user, req, body.status, body.comment or "")
            db.commit()
        except Exception as e:
            # Never let an email/audit failure block the status update.
            db.rollback()
            logger.error(
                "[leave-notify] commit failed leave_id=%s: %s\n%s",
                req.id, e, traceback.format_exc(),
            )
            notify_result = {"email_sent": False, "error": f"audit log failed: {e}", "in_app": False}

    # When a supervisor moves the request to 'under_review', re-notify HR with
    # the supervisor's recommendation so HR can make the final decision in Temveris.
    if user and body.status == "under_review":
        # Same derived balance the employee/apply-gate see (entitlement or HR
        # override + shared-pool clamp), not the old month-paced accrual.
        _bal_view   = _compute_user_balances(user, db).get(req.leave_type, {})
        used_b      = _bal_view.get("used", _compute_used_days(user.id, req.leave_type, db))
        remaining_b = _bal_view.get("remaining", 0.0)
        blackouts   = _get_blackout_dates(db, req.from_date, req.to_date, org_id=admin.organization_id)
        coverage    = _assess_team_coverage(db, user, req.from_date, req.to_date)
        supervisor  = f"The supervisor ({admin.name}) has pre-approved/recommended this leave request."
        leave_days  = _request_day_cost(req)
        background_tasks.add_task(
            send_leave_request_to_admin,
            recipients=_alert_recipients(db, user, "leave.request"),
            leave_id=req.id, employee_name=user.name, employee_email=user.email,
            leave_type=req.leave_type,
            from_date=req.from_date.strftime("%Y-%m-%d"),
            to_date=req.to_date.strftime("%Y-%m-%d"),
            description=req.description, reason=req.reason or "",
            balance_remaining=remaining_b, leave_days=leave_days,
            blackout_dates=blackouts, team_coverage=coverage,
            supervisor_status=supervisor, doc_status=None,
            balance_unlimited=_balance_is_uncapped(_bal_view),
        )

    response: dict = {"success": True, "id": leave_id, "status": body.status}
    if notify_result is not None:
        # Admin-facing delivery feedback. The frontend can surface a
        # toast like "Approved — but employee email failed to send" when
        # email_sent is False, so the admin knows to follow up manually.
        response["email_sent"]   = notify_result.get("email_sent", False)
        response["email_error"]  = notify_result.get("error")
        response["in_app_logged"] = notify_result.get("in_app", False)
    return response


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE POLICIES  GET /leave/policies + PUT /leave/policies
# IMPORTANT: These static routes MUST be declared before the /{leave_id}
# dynamic route below. FastAPI matches in declaration order — if /{leave_id}
# came first, "policies" would be captured as the path param and trigger a
# 422 integer-parse failure before this handler is ever reached.
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/policies")
def get_policies(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    policies = _get_effective_policies(db, user.organization_id)
    return [
        {
            "leave_type":      lt,
            "annual_limit":    p.get("limit", 0),
            "notice_days":     p.get("notice_days", 0),
            "doc_required":    p.get("doc_required", False),
            "monthly_accrual": p.get("accrual", 0.0),
            "shared_pool":     bool(p.get("shared_pool")),
            "hard_notice":     bool(p.get("hard_notice")),
            "evidence_gate":   bool(p.get("evidence_gate")),
            "is_unlimited":    bool(p.get("unlimited")),
            "is_earned":       bool(p.get("earned")),
            "no_annual_limit":  bool(p.get("no_annual_limit")),
        }
        for lt, p in policies.items()
    ]


class PolicyItem(BaseModel):
    leave_type:      str
    annual_limit:    int
    notice_days:     int
    doc_required:    bool
    monthly_accrual: float
    shared_pool:     bool = False
    hard_notice:     bool = False
    evidence_gate:   bool = False
    # Role flags — see LeaveTypeCreate. Optional so a client that predates
    # Phase 5 and omits them does not silently CLEAR a configured role: the
    # writer below only touches a flag the caller actually sent.
    pool_master:     Optional[bool] = None
    is_unpaid:       Optional[bool] = None
    is_sick:         Optional[bool] = None


@router.put("/policies")
def update_policies(
    policies: List[PolicyItem],
    db:        Session     = Depends(get_db),
    admin:     models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can update leave policies")

    org_id = admin.organization_id
    key    = f"leave_policies:{org_id}" if org_id is not None else "leave_policies"

    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == key,
    ).first()

    # MERGE into what is already stored rather than replacing it. This endpoint
    # rebuilt the whole blob from seven fields, so any role flag written by
    # POST /admin/types was destroyed the next time an admin saved policies —
    # which is why the flags could never have a usable write path before.
    stored: dict = {}
    if row and row.value:
        try:
            stored = _json.loads(row.value) or {}
        except Exception:
            stored = {}

    policy_dict = dict(stored)
    for p in policies:
        entry = dict(stored.get(p.leave_type) or {})
        entry.update({
            "limit":         p.annual_limit,
            "notice_days":   p.notice_days,
            "doc_required":  p.doc_required,
            "accrual":       p.monthly_accrual,
            "shared_pool":   p.shared_pool,
            "hard_notice":   p.hard_notice,
            "evidence_gate": p.evidence_gate,
        })
        policy_dict[p.leave_type] = _apply_flags(entry, p)

    _effective_for_check = _get_effective_policies(db, org_id)
    _effective_for_check.update(policy_dict)
    _assert_roles_unambiguous(_effective_for_check, _disabled_leave_types(db, org_id))
    if row:
        row.value           = _json.dumps(policy_dict)
        row.organization_id = org_id
        row.updated_by      = admin.id
        row.updated_at      = datetime.now(timezone.utc)
    else:
        db.add(models.AppSettings(
            organization_id = org_id,
            key             = key,
            value           = _json.dumps(policy_dict),
            description     = "Leave policy overrides (JSON)",
            updated_by      = admin.id,
        ))
    db.commit()
    return {"success": True}


# ──────────────────────────────────────────────────────────────────────────────
# SHARED POOL TOGGLE  GET/PUT /leave/shared-pool  (admin/hr)
# IMPORTANT: Must appear before /{leave_id} for the same routing reason above.
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/shared-pool")
def get_shared_pool(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return {"enabled": _shared_pool_enabled(db, user.organization_id)}


class SharedPoolToggle(BaseModel):
    enabled: bool


@router.put("/shared-pool")
def set_shared_pool(
    body:  SharedPoolToggle,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Only admin or HR can toggle the shared leave pool")
    org_id = admin.organization_id
    key    = f"shared_pool_enabled:{org_id}" if org_id is not None else "shared_pool_enabled"
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == key,
    ).first()
    value = "true" if body.enabled else "false"
    if row:
        row.value           = value
        row.organization_id = org_id
        row.updated_by      = admin.id
        row.updated_at      = datetime.now(timezone.utc)
    else:
        db.add(models.AppSettings(
            organization_id = org_id,
            key             = key,
            value           = value,
            description     = "Shared leave pool toggle (India Mode default: on)",
            updated_by      = admin.id,
        ))
    db.commit()
    return {"success": True, "enabled": body.enabled}


# ──────────────────────────────────────────────────────────────────────────────
# EDIT LEAVE REQUEST  PUT /leave/{id}
#
# Editing rules:
#   • Owner (the worker who applied) may edit ONLY while the request is
#     still in {submitted, under_review}. Once approved/rejected/locked
#     the request is frozen — they have to ask an admin or cancel.
#   • Admin/HR may edit ANY field at ANY status, but the most common
#     case is fixing a typo or correcting dates after a verbal
#     conversation. Status itself stays controlled by /status; we do
#     NOT let edit change status.
#   • Edits are audited so the trail still ties back to who changed
#     what when.
# ──────────────────────────────────────────────────────────────────────────────
class LeaveEditRequest(BaseModel):
    leave_type:      Optional[str] = None
    from_date:       Optional[str] = None  # YYYY-MM-DD
    to_date:         Optional[str] = None  # YYYY-MM-DD
    description:     Optional[str] = None
    reason:          Optional[str] = None
    is_half_day:     Optional[bool] = None
    half_day_period: Optional[str] = None  # "first_half" | "second_half" | ""


_OWNER_EDITABLE_STATES = ("submitted", "under_review")


@router.put("/{leave_id}")
def edit_leave_request(
    leave_id: int,
    body:     LeaveEditRequest,
    db:       Session     = Depends(get_db),
    user:     models.User = Depends(get_current_user),
):
    req = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.id == leave_id,
        models.LeaveRequest.organization_id == user.organization_id,
    ).first()
    if not req:
        raise HTTPException(404, "Leave request not found")

    is_admin = user.role in ("admin", "hr")
    is_owner = req.user_id == user.id

    if not (is_admin or is_owner):
        raise HTTPException(403, "You can only edit your own leave requests")

    if not is_admin and req.status not in _OWNER_EDITABLE_STATES:
        raise HTTPException(
            400,
            f"Leave is '{req.status}' — only admins/HR can edit at this stage",
        )

    # Capture the before-state so the audit row carries a real diff.
    before = {
        "leave_type":      req.leave_type,
        "from_date":       req.from_date.isoformat() if req.from_date else None,
        "to_date":         req.to_date.isoformat()   if req.to_date   else None,
        "description":     req.description,
        "reason":          req.reason,
        "is_half_day":     getattr(req, "is_half_day", None),
        "half_day_period": getattr(req, "half_day_period", None),
    }

    if body.leave_type is not None:
        lt = body.leave_type.strip()
        if not lt:
            raise HTTPException(400, "leave_type cannot be empty")
        req.leave_type = lt

    if body.from_date is not None or body.to_date is not None:
        try:
            new_from = datetime.strptime(body.from_date, "%Y-%m-%d") if body.from_date else req.from_date
            new_to   = datetime.strptime(body.to_date,   "%Y-%m-%d") if body.to_date   else req.to_date
        except ValueError:
            raise HTTPException(400, "Dates must be YYYY-MM-DD")
        if new_to < new_from:
            raise HTTPException(400, "to_date must be >= from_date")
        req.from_date = new_from
        req.to_date   = new_to

    if body.description is not None:
        if not body.description.strip():
            raise HTTPException(400, "Description is required")
        req.description = body.description.strip()

    if body.reason is not None:
        req.reason = body.reason.strip() or None

    if body.is_half_day is not None and hasattr(req, "is_half_day"):
        req.is_half_day = bool(body.is_half_day)

    if body.half_day_period is not None and hasattr(req, "half_day_period"):
        req.half_day_period = body.half_day_period or None

    # Re-check and re-cost a change to WHAT is being taken. Without this an
    # employee could submit an allowed request (e.g. Unpaid) and edit it into a
    # type or span their balance does not cover — bypassing HR's allocation,
    # including 0 — and the stored day count stayed at the old value. Only a real
    # change triggers it, so editing the description alone is never re-checked.
    _iso = lambda d: d.isoformat() if d else None
    what_changed = (
        req.leave_type != before["leave_type"]
        or _iso(req.from_date) != before["from_date"]
        or _iso(req.to_date) != before["to_date"]
        or getattr(req, "is_half_day", None) != before["is_half_day"]
    )
    if what_changed:
        half    = bool(getattr(req, "is_half_day", False))
        subject = db.query(models.User).filter(models.User.id == req.user_id).first()
        if not is_admin:
            # The employee gets exactly the checks a new request gets.
            errors, flags, auto_lwp = _run_validation(
                req.user_id, req.leave_type, req.from_date, req.to_date,
                bool(getattr(req, "attachment_path", None)), db,
                org_id=req.organization_id, is_half_day=half, exclude_leave_id=req.id,
            )
            if errors:
                db.rollback()
                raise HTTPException(422, {"errors": errors, "flags": flags})
            if auto_lwp:
                req.leave_type = leave_identity.unpaid_name(
                    _get_effective_policies(db, req.organization_id))
        cost = leave_policy.leave_day_cost(db, subject, req.from_date, req.to_date, is_half_day=half)
        if not half and cost <= 0:
            # Admins may edit outside the employee rules (e.g. past dates), but
            # a span with no working day is never a leave.
            db.rollback()
            raise HTTPException(422, {"errors": [
                "The selected dates are all non-working days (weekend, public holiday "
                "or scheduled day off). No leave is needed for these dates."],
                "flags": ["NO_WORKING_DAYS"]})
        req.leave_days = cost

    after = {
        "leave_type":      req.leave_type,
        "from_date":       req.from_date.isoformat() if req.from_date else None,
        "to_date":         req.to_date.isoformat()   if req.to_date   else None,
        "description":     req.description,
        "reason":          req.reason,
        "is_half_day":     getattr(req, "is_half_day", None),
        "half_day_period": getattr(req, "half_day_period", None),
    }
    diff = {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}

    if diff:
        _log_audit(db, user.id, "LeaveRequestEdited", "LeaveRequest", req.id,
                   {"changes": diff, "edited_by_admin": is_admin}, org_id=user.organization_id)

    db.commit()
    db.refresh(req)
    return {
        "id":          req.id,
        "leave_type":  req.leave_type,
        "from_date":   req.from_date.isoformat() if req.from_date else None,
        "to_date":     req.to_date.isoformat()   if req.to_date   else None,
        "description": req.description,
        "reason":      req.reason,
        "status":      req.status,
    }


# ──────────────────────────────────────────────────────────────────────────────
# DELETE LEAVE REQUEST  DELETE /leave/{id}
#
# Delete rules mirror Edit:
#   • Owner can delete only while {submitted, under_review} — once a
#     decision has been recorded the request is part of the audit
#     record and can't be removed by the requester.
#   • Admin/HR can delete anything (useful for cleaning up test data,
#     stale duplicates, or genuinely-erroneous rows). Always audited.
# ──────────────────────────────────────────────────────────────────────────────
@router.delete("/{leave_id}")
def delete_leave_request(
    leave_id: int,
    reason:   Optional[str] = Query(None, description="Why the leave is being deleted; recorded in the audit log."),
    db:       Session     = Depends(get_db),
    user:     models.User = Depends(get_current_user),
):
    req = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.id == leave_id,
        models.LeaveRequest.organization_id == user.organization_id,
    ).first()
    if not req:
        raise HTTPException(404, "Leave request not found")

    is_admin = user.role in ("admin", "hr")
    is_owner = req.user_id == user.id

    if not (is_admin or is_owner):
        raise HTTPException(403, "You can only delete your own leave requests")

    if not is_admin and req.status not in _OWNER_EDITABLE_STATES:
        raise HTTPException(
            400,
            f"Leave is '{req.status}' — only admins/HR can delete at this stage",
        )

    # Owner deletes must explain WHY — leaves are a financial record (paid
    # leave hits payroll), so erasing a request without a stated reason is
    # bad governance. Admins/HR can delete without a reason for now since
    # they typically act on the worker's behalf with a paper trail.
    clean_reason = (reason or "").strip()
    if is_owner and not is_admin and not clean_reason:
        raise HTTPException(
            400,
            "Please provide a reason for deleting this leave request.",
        )

    snapshot = {
        "id":         req.id,
        "user_id":    req.user_id,
        "leave_type": req.leave_type,
        "from_date":  req.from_date.isoformat() if req.from_date else None,
        "to_date":    req.to_date.isoformat()   if req.to_date   else None,
        "status":     req.status,
    }
    _log_audit(db, user.id, "LeaveRequestDeleted", "LeaveRequest", req.id,
               {"snapshot": snapshot, "deleted_by_admin": is_admin, "reason": clean_reason or None}, org_id=user.organization_id)

    db.delete(req)
    db.commit()
    return {"success": True, "id": leave_id}


# ──────────────────────────────────────────────────────────────────────────────
# MY LEAVE REQUESTS  GET /leave/my-requests  (employee)
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/my-requests")
def get_my_requests(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    reqs = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id == user.id
    ).order_by(models.LeaveRequest.created_at.desc()).all()

    return [
        {
            "id":              r.id,
            "leave_type":      r.leave_type,
            "from_date":       r.from_date.strftime("%Y-%m-%d"),
            "to_date":         r.to_date.strftime("%Y-%m-%d"),
            "days":            _request_day_cost(r),
            "description":     r.description,
            "status":          r.status,
            "review_comment":  r.review_comment,
            "has_attachment":  bool(r.attachment_path),
            "attachment_name": r.attachment_name,
            "is_lwp":          getattr(r, "is_lwp",  False),
            "hr_flag":         getattr(r, "hr_flag", False),
            "worked_dates":    _parse_worked_dates(getattr(r, "worked_dates", None)),
            "created_at":      r.created_at.isoformat(),
        }
        for r in reqs
    ]


# ──────────────────────────────────────────────────────────────────────────────
# LEGACY EMAIL APPROVE / REJECT LINKS  →  redirect into Temveris
# HR cannot approve or decline without first seeing balance, so these routes
# no longer mutate state — they simply bounce the reviewer to the dashboard.
# ──────────────────────────────────────────────────────────────────────────────
def _redirect_to_app(leave_id: int) -> HTMLResponse:
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    target = f"{frontend_url}/admin?leave={leave_id}"
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="0;url={target}">
<title>Redirecting to Temveris…</title></head>
<body style="margin:0;padding:60px 20px;background:#F1F5F9;font-family:'Segoe UI',Arial,sans-serif;text-align:center;">
  <div style="background:#fff;border-radius:12px;padding:40px;max-width:480px;margin:0 auto;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
    <h2 style="color:#0F172A;margin:0 0 12px;">Opening Temveris…</h2>
    <p style="color:#64748B;font-size:14px;margin:0 0 20px;">
      Leave requests must be reviewed inside Temveris so you can see the employee's
      balance, attachments and coverage context before approving or declining.
    </p>
    <a href="{target}" style="display:inline-block;background:#009BD6;color:#fff;
       padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">
      Continue to Temveris
    </a>
  </div>
</body></html>"""
    return HTMLResponse(html)


@router.get("/approve/{leave_id}", response_class=HTMLResponse, include_in_schema=False)
def email_approve_leave(leave_id: int):
    return _redirect_to_app(leave_id)


@router.get("/reject/{leave_id}", response_class=HTMLResponse, include_in_schema=False)
def email_reject_leave(leave_id: int):
    return _redirect_to_app(leave_id)


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE POLICIES  GET /leave/policies
# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# SUPPORTING DOCUMENT  GET /leave/{id}/attachment
#
# The only authenticated way to read a leave attachment. These are medical
# certificates, hospital letters and birth certificates, so the file is released
# to exactly three parties: the employee who filed the request, an admin/hr
# reviewer in the SAME organization, and super_admin.
#
# Why this exists rather than linking `attachment_path` directly: that path
# resolves to the /leave-attachments mount, which served any file to anyone who
# knew the URL. main.py now requires a token on that mount too, but a bare
# StaticFiles path can only ever answer "is this caller logged in?" — it has no
# idea which leave request the file belongs to, so it cannot answer "may THIS
# caller read THIS employee's certificate?". That check needs the row, so it
# lives here and the UI reads documents through this route.
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/{leave_id}/attachment")
def get_leave_attachment(
    leave_id: int,
    db:       Session     = Depends(get_db),
    user:     models.User = Depends(get_current_user),
):
    req = db.query(models.LeaveRequest).filter(models.LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(404, "Leave request not found")

    is_super = user.role == "super_admin"
    if not is_super and req.organization_id != user.organization_id:
        # Same shape as /{leave_id}/history: a cross-tenant id is "not found",
        # never 403 — a 403 would confirm the request exists.
        raise HTTPException(404, "Leave request not found")
    is_owner    = req.user_id == user.id
    is_reviewer = user.role in ("admin", "hr") or is_super
    if not (is_owner or is_reviewer):
        raise HTTPException(403, "You do not have access to this document")

    if not req.attachment_path:
        raise HTTPException(404, "No supporting document was attached to this request")

    # Stored as "<url_prefix>/<filename>" (see put_upload_file). Recover the
    # bucket-relative filename, which is what both backends key on.
    from app.storage import get_bytes, local_root, ATTACHMENTS
    prefix   = ATTACHMENTS.url_prefix.rstrip("/") + "/"
    filename = req.attachment_path[len(prefix):] if req.attachment_path.startswith(prefix) \
        else req.attachment_path.lstrip("/")

    # Containment check. get_bytes resolves `filename` against the bucket root
    # without validating it, and a stored path is only as trustworthy as the row
    # it came from, so verify here that it cannot walk out of the attachments
    # directory. Done for BOTH backends: "org_1/../../secret" is a traversal in
    # a Supabase key too, and this rejects it before the request is made.
    root = os.path.realpath(local_root(ATTACHMENTS))
    full = os.path.realpath(os.path.join(root, filename))
    # commonpath raises ValueError when the two land on different Windows
    # drives — an absolute stored path does that — so treat it as an escape.
    try:
        contained = os.path.commonpath([root, full]) == root
    except ValueError:
        contained = False
    if not contained:
        raise HTTPException(404, "Document is no longer available")

    # The bytes are streamed by US, in both storage backends, rather than
    # 302-ing the browser to a Supabase signed URL. Three reasons:
    #   • A signed URL is a bearer credential — a shareable, unauthenticated
    #     link to a medical certificate for its full hour. Authorizing the read
    #     and then handing out such a link defeats the check above.
    #   • A cross-origin redirect inside fetch() makes the response depend on
    #     the object host's CORS headers. The SPA is same-origin with this API;
    #     keeping the bytes here keeps it that way.
    #   • One code path means local and production behave identically, so the
    #     tests around this endpoint actually cover what production runs.
    data = get_bytes(ATTACHMENTS, filename)
    if data is None:
        raise HTTPException(404, "Document is no longer available")

    ext   = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    ctype = next((m for m, e in MIME_EXT.items() if e == ext), "application/octet-stream")
    # Original upload name, so the reviewer sees "medical-cert.pdf" and not the
    # uuid it is stored under. Quoted and stripped of quotes/newlines — the name
    # is employee-supplied and lands in a response header. inline → the browser
    # renders it in the tab instead of downloading.
    safe_name = (req.attachment_name or os.path.basename(filename)).replace('"', "").replace("\n", "").replace("\r", "")
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
            # These are personal records; keep them out of any sniffing or
            # embedding context.
            "X-Content-Type-Options": "nosniff",
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# AUDIT TRAIL  GET /leave/{id}/history
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/{leave_id}/history")
def leave_history(
    leave_id:     int,
    db:           Session     = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    req = db.query(models.LeaveRequest).filter(models.LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(404, "Leave request not found")
    is_super = current_user.role == "super_admin"
    if not is_super and req.organization_id != current_user.organization_id:
        raise HTTPException(404, "Leave request not found")

    logs = db.query(models.AuditLog).filter(
        models.AuditLog.entity    == "LeaveRequest",
        models.AuditLog.target_id == leave_id,
    ).order_by(models.AuditLog.created_at.asc()).all()

    return [
        {
            "action":     l.action,
            "actor_id":   l.actor_id,
            "payload":    l.payload,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


# ──────────────────────────────────────────────────────────────────────────────
# LEAVE ELIGIBILITY  GET /leave/eligibility/me
# Called by the worker's Apply Leave page to render the eligibility banner.
# Always returns 200 so the SPA can render the right state — `eligible: false`
# carries the "eligible from" date the UI needs.
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/eligibility/me")
def get_my_leave_eligibility(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return _compute_leave_eligibility(user, db)


# Admin convenience — check eligibility for any worker by id. Used by the
# Edit Worker drawer in AdminDashboard to surface the projected eligible-from
# date next to the Start Date field as admins fill it in.
@router.get("/eligibility/{user_id}")
def get_user_leave_eligibility(
    user_id: int,
    db:      Session     = Depends(get_db),
    actor:   models.User = Depends(get_current_user),
):
    if (actor.role or "").lower() not in ("admin", "hr") and actor.id != user_id:
        raise HTTPException(status_code=403, detail="Admin/HR only")
    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == actor.organization_id,
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return _compute_leave_eligibility(target, db)


# ──────────────────────────────────────────────────────────────────────────────
# WAITING-PERIOD SETTING  GET / PUT /leave/settings/waiting-period
# Admin-only. Persisted via AppSettings — same key-value pattern as
# `leave_policies`, `shared_pool_enabled`, etc.
# ──────────────────────────────────────────────────────────────────────────────
class WaitingPeriodPayload(BaseModel):
    months: int


@router.get("/settings/waiting-period")
def get_waiting_period(
    db:    Session     = Depends(get_db),
    actor: models.User = Depends(get_current_user),
):
    if (actor.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")
    return {
        "months":  _get_leave_waiting_period_months(db, actor.organization_id),
        "default": DEFAULT_LEAVE_WAITING_PERIOD_MONTHS,
    }


@router.put("/settings/waiting-period")
def set_waiting_period(
    payload: WaitingPeriodPayload,
    db:      Session     = Depends(get_db),
    actor:   models.User = Depends(get_current_user),
):
    if (actor.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")
    # Clamp aggressively so a fat-fingered "60" doesn't lock everyone out
    # for five years. Range matches _get_leave_waiting_period_months.
    months = max(0, min(36, int(payload.months)))

    org_id = actor.organization_id
    key    = f"leave_waiting_period_months:{org_id}" if org_id is not None else _LEAVE_WAITING_KEY
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key             == key,
    ).first()
    if row:
        row.value           = str(months)
        row.organization_id = org_id
    else:
        db.add(models.AppSettings(
            organization_id = org_id,
            key             = key,
            value           = str(months),
        ))
    db.commit()

    # Audit row so this policy change is recoverable from the audit log
    # (matches the pattern used by leave-policy / blackout-date setters).
    try:
        db.add(models.AuditLog(
            actor_id        = actor.id,
            target_id       = None,
            organization_id = actor.organization_id,
            entity          = "LeavePolicy",
            action          = "WaitingPeriodUpdated",
            payload         = {"months": months},
        ))
        db.commit()
    except Exception:
        db.rollback()

    return {"months": months, "default": DEFAULT_LEAVE_WAITING_PERIOD_MONTHS}


# ──────────────────────────────────────────────────────────────────────────────
# WORKED-DATE EVIDENCE  GET /leave/{leave_id}/worked-evidence
# For a Compensatory (earned) leave request: what Temveris recorded on each
# date the employee says they worked, so the reviewer can confirm the claim in
# the review modal without leaving it. Read-only; same access rule as the
# attachment route (cross-tenant id → 404, reviewers only).
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/{leave_id}/worked-evidence")
def get_worked_date_evidence(
    leave_id: int,
    db:       Session     = Depends(get_db),
    user:     models.User = Depends(get_current_user),
):
    from datetime import date as _date
    from zoneinfo import ZoneInfo
    from app._worked import session_worked_seconds
    from app._time_adjustments import adjustment_seconds_for_day

    req = db.query(models.LeaveRequest).filter(models.LeaveRequest.id == leave_id).first()
    is_super = user.role == "super_admin"
    if not req or (not is_super and req.organization_id != user.organization_id):
        raise HTTPException(404, "Leave request not found")
    if user.role not in ("admin", "hr") and not is_super:
        raise HTTPException(403, "Only admin or HR can review worked dates")

    org_id = req.organization_id
    try:
        zone = ZoneInfo(_tz.org_timezone_name(db, org_id))
    except Exception:
        zone = timezone.utc
    hhmm = lambda dt: (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(zone).strftime("%H:%M")

    out = []
    for ds in _parse_worked_dates(getattr(req, "worked_dates", None)):
        try:
            day = _date.fromisoformat(ds)
        except ValueError:
            out.append({"date": ds, "valid": False})
            continue
        start, end = _tz.local_day_bounds(db, org_id, day)
        sessions = (
            db.query(models.ClockSession)
            .filter(
                models.ClockSession.user_id       == req.user_id,
                models.ClockSession.clock_in_time >= start,
                models.ClockSession.clock_in_time <  end,
            )
            .order_by(models.ClockSession.clock_in_time)
            .all()
        )
        session_secs = sum(session_worked_seconds(s) for s in sessions)
        adjust_secs  = adjustment_seconds_for_day(db, req.user_id, day)
        outs = [s.clock_out_time for s in sessions if s.clock_out_time]
        screenshots = (
            db.query(models.Activity)
            .filter(
                models.Activity.user_id   == req.user_id,
                models.Activity.timestamp >= start,
                models.Activity.timestamp <  end,
                models.Activity.file_path.isnot(None),
            )
            .count()
        )
        out.append({
            "date":               ds,
            "valid":              True,
            "worked_seconds":     int(session_secs + adjust_secs),
            "session_seconds":    int(session_secs),
            "adjustment_seconds": int(adjust_secs),
            "sessions":           len(sessions),
            "first_clock_in":     hhmm(sessions[0].clock_in_time) if sessions else None,
            "last_clock_out":     hhmm(max(outs)) if outs else None,
            "still_clocked_in":   any(s.clock_out_time is None for s in sessions),
            "screenshots":        screenshots,
        })
    return {"leave_id": req.id, "user_id": req.user_id, "dates": out}
