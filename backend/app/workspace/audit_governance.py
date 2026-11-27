"""Audit & Compliance governance layer.

Provides live, persistent endpoints for the Workspace Settings →
Audit & Compliance page:

    GET  /api/workspace/audit/overview            Status banner + counters
    GET  /api/workspace/audit/events              Audit log table (live)
    GET  /api/workspace/audit/export              Audit log CSV (records export)
    GET  /api/workspace/audit/policy-history      Policy versions
    POST /api/workspace/audit/policy-history      Record a new policy version
    GET  /api/workspace/audit/access-events       Login / MFA / role events
    GET  /api/workspace/audit/exports             Export ledger
    POST /api/workspace/audit/exports             Record a new export request
    GET  /api/workspace/audit/evidence            Evidence packages
    POST /api/workspace/audit/evidence            Create evidence package
    GET  /api/workspace/audit/legal-holds         Active + released holds
    POST /api/workspace/audit/legal-holds         Create legal hold
    POST /api/workspace/audit/legal-holds/{id}/release
    GET  /api/workspace/audit/retention           Retention rules
    PUT  /api/workspace/audit/retention/{key}     Update retention rule

All write endpoints append an immutable AuditLog row so every governance
mutation is itself auditable.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, JSON, Text, Boolean,
    UniqueConstraint, desc, func, case, and_,
)
from sqlalchemy.orm import Session, relationship

from database.database import Base, engine, get_db
from database import models
from app.auth.service import get_current_user

_log = logging.getLogger("zoiko.workspace.audit")

router = APIRouter(prefix="/api/workspace/audit", tags=["Audit & Compliance"])


# ──────────────────────────────────────────────────────────────────────────
# Models  (organization_id added for tenant isolation on all five tables)
# ──────────────────────────────────────────────────────────────────────────
class AuditPolicyVersion(Base):
    __tablename__ = "audit_policy_versions"
    id             = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    policy_name    = Column(String(200), nullable=False, index=True)
    policy_type    = Column(String(50),  nullable=False, index=True)
    version        = Column(String(20),  nullable=False)
    effective_date = Column(DateTime(timezone=True), nullable=True)
    scope          = Column(String(200), nullable=True)
    status         = Column(String(40),  nullable=False, default="Current")
    changed_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    diff           = Column(JSON, nullable=True)
    created_at     = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class AuditDataExport(Base):
    __tablename__ = "audit_data_exports"
    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    export_type     = Column(String(80), nullable=False)
    scope           = Column(String(200), nullable=True)
    fmt             = Column(String(40), nullable=False, default="CSV")
    reason          = Column(Text, nullable=False)
    status          = Column(String(40), nullable=False, default="Available")
    exported_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ip_address      = Column(String(45), nullable=True)
    expires_at      = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class AuditEvidencePackage(Base):
    __tablename__ = "audit_evidence_packages"
    __table_args__ = (
        # Per-org uniqueness: two orgs may use the same package code independently.
        UniqueConstraint("organization_id", "package_code", name="uq_evidence_org_code"),
    )
    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    package_code    = Column(String(40), nullable=False, index=True)
    name            = Column(String(200), nullable=False)
    purpose         = Column(String(80), nullable=False)
    scope           = Column(String(300), nullable=True)
    redaction       = Column(String(40), nullable=False, default="Standard")
    status          = Column(String(40), nullable=False, default="Pending review")
    created_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    notes           = Column(Text, nullable=True)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class AuditLegalHold(Base):
    __tablename__ = "audit_legal_holds"
    __table_args__ = (
        # Per-org uniqueness: matter references are local to each tenant.
        UniqueConstraint("organization_id", "matter_ref", name="uq_legal_hold_org_matter"),
    )
    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    name            = Column(String(200), nullable=False)
    matter_ref      = Column(String(80), nullable=False, index=True)
    scope           = Column(String(300), nullable=True)
    reason          = Column(Text, nullable=False)
    status          = Column(String(40), nullable=False, default="Active hold")
    created_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    start_date      = Column(DateTime(timezone=True), default=datetime.utcnow)
    released_at     = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class AuditRetentionRule(Base):
    __tablename__ = "audit_retention_rules"
    __table_args__ = (
        # Per-org uniqueness: each tenant manages its own retention schedule.
        UniqueConstraint("organization_id", "rule_key", name="uq_retention_org_key"),
    )
    id                   = Column(Integer, primary_key=True, index=True)
    organization_id      = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                                  nullable=True, index=True)
    rule_key             = Column(String(80), nullable=False, index=True)
    category             = Column(String(120), nullable=False)
    retention_period     = Column(String(80), nullable=False)
    legal_basis          = Column(String(200), nullable=True)
    deletion_behaviour   = Column(String(200), nullable=True)
    legal_hold_behaviour = Column(String(60), nullable=False, default="Honoured")
    updated_at           = Column(DateTime(timezone=True), default=datetime.utcnow,
                                  onupdate=datetime.utcnow)


# Default retention rules seeded lazily on first GET /retention per org.
DEFAULT_RETENTION = [
    ("audit_logs",        "Audit logs",        "7 years",                    "SOC 2 + statutory",           "Automatic after retention",    "Honoured"),
    ("timesheets",        "Timesheets",        "5 years",                    "Payroll statute (IN/US)",     "Automatic after retention",    "Honoured"),
    ("leave_records",     "Leave records",     "5 years",                    "HR retention policy",         "Automatic after retention",    "Honoured"),
    ("screenshots",       "Screenshots",       "90 days",                    "Monitoring policy",           "Automatic after 90 days",      "Honoured"),
    ("access_events",     "Access events",     "2 years",                    "Security policy",             "Automatic after retention",    "Honoured"),
    ("evidence_packages", "Evidence packages", "Indefinite while case open", "Investigation policy",        "Manual release required",      "Always honoured"),
]


def ensure_tables() -> None:
    """Create audit governance tables.

    Retention rules are no longer globally seeded here — they are seeded
    lazily per-org on the first GET /retention call so each tenant starts
    with its own independent retention schedule.
    """
    try:
        for t in (
            AuditPolicyVersion.__table__,
            AuditDataExport.__table__,
            AuditEvidencePackage.__table__,
            AuditLegalHold.__table__,
            AuditRetentionRule.__table__,
        ):
            t.create(bind=engine, checkfirst=True)
        _log.info("Audit governance tables ensured")
    except Exception:
        _log.exception("Failed to ensure audit governance tables")


def _seed_retention_for_org(db: Session, org_id: Optional[int]) -> None:
    """Insert the default retention schedule for *org_id* if none exists yet."""
    existing = (
        db.query(func.count(AuditRetentionRule.id))
          .filter(AuditRetentionRule.organization_id == org_id)
          .scalar() or 0
    )
    if existing:
        return
    for key, cat, period, basis, deletion, hold in DEFAULT_RETENTION:
        db.add(AuditRetentionRule(
            organization_id=org_id,
            rule_key=key, category=cat, retention_period=period,
            legal_basis=basis, deletion_behaviour=deletion,
            legal_hold_behaviour=hold,
        ))
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _require_admin(user: models.User) -> None:
    if user.role not in ("admin", "hr", "owner", "billing_admin", "super_admin"):
        raise HTTPException(403, "Admin or HR access required")


def _log_audit(db: Session, *, actor_id: Optional[int], entity: str,
               action: str, payload: Optional[Dict[str, Any]] = None,
               ip: Optional[str] = None, org_id: Optional[int] = None) -> None:
    db.add(models.AuditLog(
        actor_id=actor_id, entity=entity, action=action,
        payload=payload or {}, ip_address=ip, organization_id=org_id,
    ))


def _user_name(u: Optional[models.User]) -> str:
    if not u:
        return "System"
    if getattr(u, "name", None):
        return u.name
    return (u.email or "").split("@")[0] or "User"


def _role_label(role: Optional[str]) -> str:
    if not role:
        return "—"
    table = {
        "owner": "Owner", "admin": "Admin", "hr": "HR Admin",
        "billing_admin": "Billing Admin", "employee": "Employee",
    }
    return table.get(role, role.title())


def _risk_from_action(entity: str, action: str) -> str:
    a = (action or "").lower()
    if "fail" in a or "blocked" in a or "denied" in a or "lockout" in a:
        return "Critical" if "lockout" in a or "blocked" in a else "High"
    if "delete" in a or "remove" in a or "revoke" in a:
        return "High"
    if "policy" in a or "password" in a or "security" in a:
        return "Medium"
    if "login" in a or "logout" in a or "mfa" in a:
        return "Info"
    return "Low"


# ──────────────────────────────────────────────────────────────────────────
# Pydantic payloads
# ──────────────────────────────────────────────────────────────────────────
class ExportRequest(BaseModel):
    export_type: str
    scope: Optional[str] = None
    fmt: str = "CSV"
    reason: str


class EvidenceRequest(BaseModel):
    name: str
    purpose: str
    scope: Optional[str] = None
    redaction: str = "Standard"
    notes: Optional[str] = None


class LegalHoldRequest(BaseModel):
    name: str
    matter_ref: str
    scope: Optional[str] = None
    reason: str


class RetentionUpdate(BaseModel):
    retention_period: Optional[str] = None
    legal_basis: Optional[str] = None
    deletion_behaviour: Optional[str] = None
    legal_hold_behaviour: Optional[str] = None


class PolicyVersionRequest(BaseModel):
    policy_name: str
    policy_type: str
    version: str
    effective_date: Optional[datetime] = None
    scope: Optional[str] = None
    status: str = "Current"
    diff: Optional[Dict[str, Any]] = None


# ──────────────────────────────────────────────────────────────────────────
# Overview — banner + 4 stat cards
# ──────────────────────────────────────────────────────────────────────────
@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id    = current_user.organization_id
    now       = datetime.now(timezone.utc)
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ── Query 1: AuditLog — 3 metrics in one conditional aggregation ────────
    # Was: 3 separate COUNT queries against audit_log.
    # Now: one scan, three CASE/SUM expressions inside a single SELECT.
    # Pruned to created_at >= cutoff_60 to avoid a full-table scan.
    _high_risk_cond = (
        models.AuditLog.action.ilike("%fail%")   |
        models.AuditLog.action.ilike("%delete%") |
        models.AuditLog.action.ilike("%revoke%") |
        models.AuditLog.action.ilike("%block%")
    )
    audit_q = db.query(
        func.sum(case(
            (models.AuditLog.created_at >= cutoff_30, 1), else_=0,
        )).label("events_30d"),
        func.sum(case(
            (and_(models.AuditLog.created_at >= cutoff_60,
                  models.AuditLog.created_at <  cutoff_30), 1), else_=0,
        )).label("events_prev"),
        func.sum(case(
            (and_(models.AuditLog.created_at >= cutoff_30, _high_risk_cond), 1), else_=0,
        )).label("high_risk_30d"),
    ).filter(models.AuditLog.created_at >= cutoff_60)
    if not is_super:
        audit_q = audit_q.filter(models.AuditLog.organization_id == org_id)
    _al = audit_q.one()
    events_30d    = int(_al.events_30d    or 0)
    events_prev   = int(_al.events_prev   or 0)
    high_risk_30d = int(_al.high_risk_30d or 0)

    # ── Query 2: AuditPolicyVersion counts + MAX(created_at) ────────────────
    # Was: 2 COUNT queries + 1 ORDER BY … LIMIT 1 row fetch.
    # Now: one aggregation for the counts + max timestamp; one targeted
    # point-lookup for the version string of that specific row.
    pol_agg_q = db.query(
        func.count(AuditPolicyVersion.id).label("total"),
        func.sum(case(
            (AuditPolicyVersion.status == "Current", 1), else_=0,
        )).label("current_count"),
        func.max(AuditPolicyVersion.created_at).label("latest_at"),
    )
    if not is_super:
        pol_agg_q = pol_agg_q.filter(AuditPolicyVersion.organization_id == org_id)
    _pv = pol_agg_q.one()
    policy_versions  = int(_pv.total         or 0)
    current_policies = int(_pv.current_count or 0)

    # ── Query 3: AuditPolicyVersion latest row — version string only ────────
    # A single point-lookup using the MAX(created_at) from Query 2.
    # Only executed when at least one policy version exists.
    policy_version_str = None
    last_reviewed_iso  = None
    if _pv.latest_at is not None:
        latest_row = (
            db.query(AuditPolicyVersion.version, AuditPolicyVersion.created_at)
              .filter(AuditPolicyVersion.created_at == _pv.latest_at)
        )
        if not is_super:
            latest_row = latest_row.filter(AuditPolicyVersion.organization_id == org_id)
        latest_row = latest_row.first()
        if latest_row:
            policy_version_str = latest_row.version
            last_reviewed_iso  = (
                latest_row.created_at.isoformat() if latest_row.created_at else None
            )

    # ── Query 4: AuditEvidencePackage — 3 metrics in one conditional agg ────
    # Was: 3 separate COUNT queries.
    # Now: one scan with three CASE/SUM expressions.
    evid_q = db.query(
        func.count(AuditEvidencePackage.id).label("total"),
        func.sum(case(
            (AuditEvidencePackage.created_at >= month_start, 1), else_=0,
        )).label("month"),
        func.sum(case(
            (AuditEvidencePackage.status == "Pending review", 1), else_=0,
        )).label("pending"),
    )
    if not is_super:
        evid_q = evid_q.filter(AuditEvidencePackage.organization_id == org_id)
    _ep = evid_q.one()
    evidence_total   = int(_ep.total   or 0)
    evidence_month   = int(_ep.month   or 0)
    evidence_pending = int(_ep.pending or 0)

    # ── Query 5: AuditLegalHold — single count (unchanged) ──────────────────
    holds_q = db.query(func.count(AuditLegalHold.id)).filter(
        AuditLegalHold.status == "Active hold"
    )
    if not is_super:
        holds_q = holds_q.filter(AuditLegalHold.organization_id == org_id)
    holds_active = int(holds_q.scalar() or 0)

    pct_change = 0
    if events_prev:
        pct_change = int(round(((events_30d - events_prev) / events_prev) * 100))

    return {
        "policy_version": policy_version_str,
        "last_reviewed":  last_reviewed_iso,
        "logging_active": True,
        "evidence_controls": True,
        "counters": {
            "audit_events_30d":          events_30d,
            "audit_events_pct_change":   pct_change,
            "high_risk_30d":             high_risk_30d,
            "policy_versions":           policy_versions,
            "policy_current":            current_policies,
            "evidence_packages_total":   evidence_total,
            "evidence_packages_month":   evidence_month,
            "evidence_packages_pending": evidence_pending,
            "legal_holds_active":        holds_active,
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Audit Log
# ──────────────────────────────────────────────────────────────────────────
@router.get("/events")
def list_events(
    limit: int = Query(100, ge=1, le=500),
    since_days: int = Query(30, ge=1, le=730),
    actor: Optional[str] = None,
    event_type: Optional[str] = None,
    risk: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id
    cutoff   = datetime.now(timezone.utc) - timedelta(days=since_days)

    query = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(models.AuditLog.created_at >= cutoff)
    )
    if not is_super:
        query = query.filter(models.AuditLog.organization_id == org_id)
    if actor and actor != "all":
        query = query.filter(models.User.email.ilike(f"%{actor}%"))
    if event_type and event_type != "all":
        query = query.filter(
            models.AuditLog.entity.ilike(f"%{event_type}%") |
            models.AuditLog.action.ilike(f"%{event_type}%")
        )
    if q:
        like = f"%{q}%"
        query = query.filter(
            models.AuditLog.entity.ilike(like) |
            models.AuditLog.action.ilike(like) |
            models.User.email.ilike(like)
        )
    rows = query.order_by(desc(models.AuditLog.created_at)).limit(limit).all()

    events = []
    for r, u in rows:
        rk = _risk_from_action(r.entity, r.action)
        if risk and risk != "all" and rk != risk:
            continue
        events.append({
            "id":          f"EVT-{r.id:06d}",
            "occurred_at": r.created_at.isoformat() if r.created_at else None,
            "actor":       _user_name(u),
            "actor_email": u.email if u else None,
            "role":        _role_label(u.role if u else None),
            "event":       r.action,
            "entity":      r.entity,
            "object":      (r.payload or {}).get("object") or r.entity,
            "action":      (r.payload or {}).get("summary") or (r.payload or {}).get("change") or "—",
            "ip_address":  r.ip_address,
            "risk":        rk,
        })

    actors_q = (
        db.query(models.User.email)
          .join(models.AuditLog, models.AuditLog.actor_id == models.User.id)
          .filter(models.AuditLog.created_at >= cutoff)
    )
    if not is_super:
        actors_q = actors_q.filter(models.AuditLog.organization_id == org_id)
    actors = [a[0] for a in actors_q.distinct().limit(50).all()]

    return {"events": events, "actors": actors, "total": len(events)}


@router.get("/export")
def export_csv(
    since_days: int = Query(30, ge=1, le=730),
    reason: str = Query(..., min_length=4),
    request_ip: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id
    cutoff   = datetime.now(timezone.utc) - timedelta(days=since_days)

    audit_q = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(models.AuditLog.created_at >= cutoff)
          .order_by(desc(models.AuditLog.created_at))
    )
    if not is_super:
        audit_q = audit_q.filter(models.AuditLog.organization_id == org_id)
    rows = audit_q.limit(5000).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["EventID", "OccurredAt", "Actor", "ActorEmail", "Role", "Entity", "Action", "IP", "Risk"])
    for r, u in rows:
        writer.writerow([
            f"EVT-{r.id:06d}",
            r.created_at.isoformat() if r.created_at else "",
            _user_name(u), u.email if u else "",
            _role_label(u.role if u else None),
            r.entity, r.action, r.ip_address or "",
            _risk_from_action(r.entity, r.action),
        ])

    export = AuditDataExport(
        organization_id=org_id,
        export_type="Audit report", scope=f"All workspace · Last {since_days} days",
        fmt="CSV", reason=reason, status="Available",
        exported_by_id=current_user.id, ip_address=request_ip,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.add(export)
    _log_audit(db, actor_id=current_user.id, entity="AuditExport",
               action="audit.export.generated",
               payload={"reason": reason, "since_days": since_days, "rows": len(rows)},
               org_id=org_id)
    db.commit()

    headers = {"Content-Disposition": f'attachment; filename="audit-log-{datetime.utcnow():%Y%m%d-%H%M%S}.csv"'}
    return Response(content=buf.getvalue(), media_type="text/csv", headers=headers)


# ──────────────────────────────────────────────────────────────────────────
# Policy History
# ──────────────────────────────────────────────────────────────────────────
@router.get("/policy-history")
def list_policy_versions(
    policy_type: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    query = (
        db.query(AuditPolicyVersion, models.User)
          .outerjoin(models.User, models.User.id == AuditPolicyVersion.changed_by_id)
    )
    if not is_super:
        query = query.filter(AuditPolicyVersion.organization_id == org_id)
    if policy_type and policy_type != "all":
        query = query.filter(AuditPolicyVersion.policy_type == policy_type)
    if status and status != "all":
        query = query.filter(AuditPolicyVersion.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            AuditPolicyVersion.policy_name.ilike(like) |
            AuditPolicyVersion.policy_type.ilike(like)
        )
    rows = query.order_by(desc(AuditPolicyVersion.created_at)).limit(200).all()
    items = [
        {
            "id":             p.id,
            "policy_name":    p.policy_name,
            "policy_type":    p.policy_type,
            "version":        p.version,
            "effective_date": p.effective_date.isoformat() if p.effective_date else None,
            "scope":          p.scope,
            "status":         p.status,
            "changed_by":     _user_name(u),
            "created_at":     p.created_at.isoformat() if p.created_at else None,
        }
        for p, u in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/policy-history")
def record_policy_version(
    body: PolicyVersionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    org_id = current_user.organization_id

    if body.status == "Current":
        # Supersede only this org's prior Current rows for the same policy.
        (db.query(AuditPolicyVersion)
            .filter(
                AuditPolicyVersion.organization_id == org_id,
                AuditPolicyVersion.policy_name     == body.policy_name,
                AuditPolicyVersion.status          == "Current",
            )
            .update({"status": "Superseded"}))

    row = AuditPolicyVersion(
        organization_id=org_id,
        policy_name=body.policy_name, policy_type=body.policy_type,
        version=body.version, effective_date=body.effective_date,
        scope=body.scope, status=body.status, diff=body.diff,
        changed_by_id=current_user.id,
    )
    db.add(row)
    _log_audit(db, actor_id=current_user.id, entity="Policy",
               action=f"policy.{body.policy_type.lower()}.versioned",
               payload={"policy": body.policy_name, "version": body.version, "status": body.status},
               org_id=org_id)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Access Events
# ──────────────────────────────────────────────────────────────────────────
ACCESS_ENTITIES = {"Auth", "User", "Session", "MFA", "SSO", "Role", "RoleAssignment"}


@router.get("/access-events")
def list_access_events(
    since_days: int = Query(7, ge=1, le=180),
    risk: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id
    cutoff   = datetime.now(timezone.utc) - timedelta(days=since_days)

    query = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(
              models.AuditLog.created_at >= cutoff,
              models.AuditLog.entity.in_(list(ACCESS_ENTITIES)),
          )
          .order_by(desc(models.AuditLog.created_at))
    )
    if not is_super:
        query = query.filter(models.AuditLog.organization_id == org_id)
    rows = query.limit(500).all()

    items = []
    for r, u in rows:
        rk = _risk_from_action(r.entity, r.action)
        if risk and risk != "all" and rk != risk:
            continue
        action_lower = (r.action or "").lower()
        if "lockout" in action_lower or "locked" in action_lower:
            result = "Locked"
        elif "fail" in action_lower or "blocked" in action_lower:
            result = "Blocked"
        else:
            result = "Success"
        payload  = r.payload or {}
        location = payload.get("location") or (f"{r.ip_address} · {payload.get('country','')}".strip(" ·") if r.ip_address else "—")
        device   = payload.get("device") or payload.get("user_agent") or "—"
        if q:
            haystack = " ".join([str(u.email if u else ""), r.action or "", location, device]).lower()
            if q.lower() not in haystack:
                continue
        items.append({
            "id":          f"ACE-{r.id:06d}",
            "occurred_at": r.created_at.isoformat() if r.created_at else None,
            "user":        _user_name(u),
            "user_email":  u.email if u else None,
            "role":        _role_label(u.role if u else None),
            "event":       r.action,
            "location":    location,
            "device":      device,
            "result":      result,
            "risk":        rk,
        })
    return {"items": items, "total": len(items)}


# ──────────────────────────────────────────────────────────────────────────
# Exports
# ──────────────────────────────────────────────────────────────────────────
@router.get("/exports")
def list_exports(
    export_type: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    since_days: int = Query(90, ge=1, le=730),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id
    cutoff   = datetime.now(timezone.utc) - timedelta(days=since_days)

    query = (
        db.query(AuditDataExport, models.User)
          .outerjoin(models.User, models.User.id == AuditDataExport.exported_by_id)
          .filter(AuditDataExport.created_at >= cutoff)
    )
    if not is_super:
        query = query.filter(AuditDataExport.organization_id == org_id)
    if export_type and export_type != "all":
        query = query.filter(AuditDataExport.export_type == export_type)
    if status and status != "all":
        query = query.filter(AuditDataExport.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            AuditDataExport.export_type.ilike(like) |
            AuditDataExport.reason.ilike(like)
        )
    rows = query.order_by(desc(AuditDataExport.created_at)).limit(200).all()
    now = datetime.now(timezone.utc)
    items = []
    for e, u in rows:
        status_val = e.status
        if status_val == "Available" and e.expires_at:
            exp = e.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                status_val = "Expired"
        items.append({
            "id":          e.id,
            "exported_at": e.created_at.isoformat() if e.created_at else None,
            "exported_by": _user_name(u),
            "export_type": e.export_type,
            "scope":       e.scope,
            "fmt":         e.fmt,
            "reason":      e.reason,
            "status":      status_val,
        })
    return {"items": items, "total": len(items)}


@router.post("/exports")
def record_export(
    body: ExportRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    if not (body.reason and len(body.reason.strip()) >= 4):
        raise HTTPException(400, "A reason of at least 4 characters is required for every export.")
    org_id = current_user.organization_id
    row = AuditDataExport(
        organization_id=org_id,
        export_type=body.export_type, scope=body.scope,
        fmt=body.fmt, reason=body.reason.strip(),
        status="Available", exported_by_id=current_user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.add(row)
    _log_audit(db, actor_id=current_user.id, entity="AuditExport",
               action="export.recorded",
               payload={"type": body.export_type, "scope": body.scope, "reason": body.reason},
               org_id=org_id)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Evidence Packages
# ──────────────────────────────────────────────────────────────────────────
@router.get("/evidence")
def list_evidence(
    purpose: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    query = (
        db.query(AuditEvidencePackage, models.User)
          .outerjoin(models.User, models.User.id == AuditEvidencePackage.created_by_id)
    )
    if not is_super:
        query = query.filter(AuditEvidencePackage.organization_id == org_id)
    if purpose and purpose != "all":
        query = query.filter(AuditEvidencePackage.purpose == purpose)
    if status and status != "all":
        query = query.filter(AuditEvidencePackage.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            AuditEvidencePackage.name.ilike(like) |
            AuditEvidencePackage.package_code.ilike(like)
        )
    rows = query.order_by(desc(AuditEvidencePackage.created_at)).limit(200).all()
    items = [
        {
            "id":         p.id,
            "code":       p.package_code,
            "name":       p.name,
            "purpose":    p.purpose,
            "scope":      p.scope,
            "redaction":  p.redaction,
            "status":     p.status,
            "created_by": _user_name(u),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p, u in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/evidence")
def create_evidence(
    body: EvidenceRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    org_id = current_user.organization_id
    # Sequence is per-org so codes are locally unique (PKG-2025-0001 can exist
    # in both Org A and Org B without colliding on the composite unique index).
    seq = int(
        db.query(func.count(AuditEvidencePackage.id))
          .filter(AuditEvidencePackage.organization_id == org_id)
          .scalar() or 0
    ) + 1
    code = f"PKG-{datetime.utcnow():%Y}-{seq:04d}"
    row = AuditEvidencePackage(
        organization_id=org_id,
        package_code=code, name=body.name, purpose=body.purpose,
        scope=body.scope, redaction=body.redaction, notes=body.notes,
        status="Pending review", created_by_id=current_user.id,
    )
    db.add(row)
    _log_audit(db, actor_id=current_user.id, entity="EvidencePackage",
               action="evidence.package.created",
               payload={"code": code, "name": body.name, "purpose": body.purpose},
               org_id=org_id)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "code": row.package_code, "status": "ok"}


# ──────────────────────────────────────────────────────────────────────────
# Legal Holds
# ──────────────────────────────────────────────────────────────────────────
@router.get("/legal-holds")
def list_legal_holds(
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    query = (
        db.query(AuditLegalHold, models.User)
          .outerjoin(models.User, models.User.id == AuditLegalHold.created_by_id)
    )
    if not is_super:
        query = query.filter(AuditLegalHold.organization_id == org_id)
    if status and status != "all":
        query = query.filter(AuditLegalHold.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            AuditLegalHold.name.ilike(like) |
            AuditLegalHold.matter_ref.ilike(like)
        )
    rows = query.order_by(desc(AuditLegalHold.created_at)).limit(200).all()
    items = [
        {
            "id":          h.id,
            "name":        h.name,
            "matter_ref":  h.matter_ref,
            "scope":       h.scope,
            "reason":      h.reason,
            "status":      h.status,
            "created_by":  _user_name(u),
            "start_date":  h.start_date.isoformat() if h.start_date else None,
            "released_at": h.released_at.isoformat() if h.released_at else None,
        }
        for h, u in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/legal-holds")
def create_legal_hold(
    body: LegalHoldRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    org_id = current_user.organization_id
    # Duplicate matter_ref check is scoped to the org — two tenants may
    # independently create holds with the same matter reference.
    if (
        db.query(AuditLegalHold)
          .filter(
              AuditLegalHold.organization_id == org_id,
              AuditLegalHold.matter_ref       == body.matter_ref,
          )
          .first()
    ):
        raise HTTPException(400, "A legal hold with this matter reference already exists.")
    row = AuditLegalHold(
        organization_id=org_id,
        name=body.name, matter_ref=body.matter_ref, scope=body.scope,
        reason=body.reason, status="Active hold", created_by_id=current_user.id,
    )
    db.add(row)
    _log_audit(db, actor_id=current_user.id, entity="LegalHold",
               action="legal_hold.created",
               payload={"matter": body.matter_ref, "name": body.name},
               org_id=org_id)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": "ok"}


@router.post("/legal-holds/{hold_id}/release")
def release_legal_hold(
    hold_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    hold_q = db.query(AuditLegalHold).filter(AuditLegalHold.id == hold_id)
    if not is_super:
        hold_q = hold_q.filter(AuditLegalHold.organization_id == org_id)
    row = hold_q.first()
    if not row:
        raise HTTPException(404, "Legal hold not found")

    row.status     = "Released"
    row.released_at = datetime.now(timezone.utc)
    _log_audit(db, actor_id=current_user.id, entity="LegalHold",
               action="legal_hold.released",
               payload={"matter": row.matter_ref},
               org_id=org_id)
    db.commit()
    return {"id": row.id, "status": "Released"}


# ──────────────────────────────────────────────────────────────────────────
# Retention
# ──────────────────────────────────────────────────────────────────────────
@router.get("/retention")
def list_retention(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    if not is_super:
        # Seed defaults for this org on first visit — idempotent.
        _seed_retention_for_org(db, org_id)

    rows_q = db.query(AuditRetentionRule).order_by(AuditRetentionRule.id.asc())
    if not is_super:
        rows_q = rows_q.filter(AuditRetentionRule.organization_id == org_id)
    rows = rows_q.all()

    return {
        "items": [
            {
                "id":                   r.id,
                "rule_key":             r.rule_key,
                "category":             r.category,
                "retention_period":     r.retention_period,
                "legal_basis":          r.legal_basis,
                "deletion_behaviour":   r.deletion_behaviour,
                "legal_hold_behaviour": r.legal_hold_behaviour,
                "updated_at":           r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    }


@router.put("/retention/{rule_key}")
def update_retention(
    rule_key: str,
    body: RetentionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    rule_q = db.query(AuditRetentionRule).filter(AuditRetentionRule.rule_key == rule_key)
    if not is_super:
        rule_q = rule_q.filter(AuditRetentionRule.organization_id == org_id)
    row = rule_q.first()
    if not row:
        raise HTTPException(404, "Retention rule not found")

    diff = {}
    for field in ("retention_period", "legal_basis", "deletion_behaviour", "legal_hold_behaviour"):
        val = getattr(body, field)
        if val is not None and getattr(row, field) != val:
            diff[field] = {"from": getattr(row, field), "to": val}
            setattr(row, field, val)
    if diff:
        _log_audit(db, actor_id=current_user.id, entity="RetentionRule",
                   action="retention.rule.updated",
                   payload={"rule": rule_key, "diff": diff},
                   org_id=org_id)
    db.commit()
    return {"id": row.id, "rule_key": row.rule_key, "diff": diff}
