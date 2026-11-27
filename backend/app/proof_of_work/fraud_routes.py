"""
Temveris - Proof-of-Work: Fraud Event Routes
==============================================
GET /pow/fraud-events              – List all fraud events (filterable)
GET /pow/fraud-events/record/{id}  – Fraud events for a specific work record
GET /pow/admin/alerts              – Admin view: all flagged / low-score records
GET /pow/admin/summary             – Admin view: high-level fraud KPIs

Multitenancy isolation
----------------------
All WorkRecord and FraudEvent queries are scoped to the caller's
organization_id. Admins of org A cannot see org B's fraud data.
The admin summary cache is keyed by org_id so one org's result
never poisons another's cached payload.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta

from database.database import get_db
from database import models
from app.auth.service import get_current_user

router = APIRouter(prefix="/pow", tags=["Fraud Detection"])


def _require_admin(user: models.User) -> None:
    """Block non-admin/hr users from admin-only endpoints."""
    if user.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin access required")


# ══════════════════════════════════════════════
# Window-based aggregation — keeps admin numbers in sync with the
# employee Work Authenticity tab. The employee endpoint
# (/pow/records/user/{uid}/windows) rolls per-screenshot rows up into
# 15-min windows and only flags a window when activity is sustained-low
# across the FULL window. The admin summary / alerts views below use
# the SAME thresholds and SAME bucketing so the admin's "Risk Users"
# numbers reconcile with what each employee sees.
#
# Time bound: admin views look at the last RISK_LOOKBACK_DAYS (30) of
# data, not the entire WorkRecord table since system inception. A
# worker who had a bad week six months ago shouldn't still be "High
# Risk" today.
# ══════════════════════════════════════════════
# Dashboard bucketing uses the organization's own wall clock — see
# _dashboard_tz below. There is no module-level offset: a fixed +05:30 put a
# non-Indian tenant's fraud windows on the wrong local day.
def _dashboard_tz(db, org_id):
    from app import _tz as _tzmod
    return _tzmod.org_timezone(db, org_id)


_WINDOW_MINUTES          = 15
_WINDOW_SECONDS          = _WINDOW_MINUTES * 60
_RISK_LOOKBACK_DAYS      = 30
_FLAG_KEYBOARD_TOTAL     = 30
_FLAG_MOUSE_TOTAL        = 15
_WARN_KEYBOARD_TOTAL     = 100
_WARN_MOUSE_TOTAL        = 50

# Risk badges (per-user, computed from window aggregates over the
# lookback window):
#   High   - many sustained-idle 15-min windows OR avg score very low
#   Medium - some sustained-idle windows OR mid-low avg score
#   Low    - everything else
_HIGH_RISK_FLAGGED_WINDOWS = 5
_HIGH_RISK_AVG_SCORE       = 50
_MED_RISK_AVG_SCORE        = 75


def _classify_window(kb_total: int, mouse_total: int) -> str:
    if kb_total < _FLAG_KEYBOARD_TOTAL and mouse_total < _FLAG_MOUSE_TOTAL:
        return "flagged"
    if kb_total < _WARN_KEYBOARD_TOTAL or mouse_total < _WARN_MOUSE_TOTAL:
        return "warning"
    return "validated"


def _aggregate_windows(rows) -> List[dict]:
    """Bucket a flat list of WorkRecord rows into per-(user, 15-min)
    windows using the same bucket math as the employee endpoint
    (epoch-anchored so bucket boundaries are stable across servers).

    Returns a list of dicts ready for downstream count/sort logic. Each
    dict represents one 15-min window for one user — i.e. there's at
    most one row per (user, bucket) pair.
    """
    buckets: Dict = {}
    for r in rows:
        ts = r.timestamp
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bucket_idx = int(ts.timestamp() // _WINDOW_SECONDS)
        key = (r.user_id, bucket_idx)
        b = buckets.get(key)
        if b is None:
            bucket_start = datetime.fromtimestamp(bucket_idx * _WINDOW_SECONDS, tz=timezone.utc)
            b = {
                "user_id":        r.user_id,
                "bucket_idx":     bucket_idx,
                # Floor-aligned analytical boundary (kept for the timeline axis).
                "bucket_start":   bucket_start,
                "bucket_end":     bucket_start + timedelta(seconds=_WINDOW_SECONDS),
                # `window_start` / `window_end` track the ACTUAL first/last
                # activity timestamps inside the bucket — so admin alerts say
                # "activity 09:01–09:14" instead of "09:00–09:15" for a worker
                # who clocked in mid-bucket, which read as a clock-skew bug.
                "window_start":   ts,
                "window_end":     ts,
                "sample_count":   0,
                "keyboard_total": 0,
                "mouse_total":    0,
                "score_sum":      0,
                "any_pending":    False,
                "latest_record":  None,
                "latest_ts":      None,
                "latest_artifact": None,
                "latest_score":   0,
            }
            buckets[key] = b
        # Stretch the observed window to include this sample's timestamp.
        if ts < b["window_start"]:
            b["window_start"] = ts
        if ts > b["window_end"]:
            b["window_end"] = ts
        b["sample_count"]   += 1
        b["keyboard_total"] += int(r.keyboard_activity or 0)
        b["mouse_total"]    += int(r.mouse_activity or 0)
        b["score_sum"]      += int(r.confidence_score or 0)
        if r.validation_status == "pending":
            b["any_pending"] = True
        # Track newest sample so the alert UI can drill down to a real
        # screenshot for context.
        if b["latest_ts"] is None or (ts and ts > b["latest_ts"]):
            b["latest_record"]    = r.record_id
            b["latest_ts"]        = ts
            b["latest_artifact"]  = r.artifact_url
            b["latest_score"]     = int(r.confidence_score or 0)

    # Stamp each bucket with its window-level status + average score
    # exactly once so consumers don't have to re-derive.
    out = []
    for b in buckets.values():
        if b["any_pending"]:
            b["status"] = "pending"
        else:
            b["status"] = _classify_window(b["keyboard_total"], b["mouse_total"])
        b["avg_score"] = round(b["score_sum"] / max(1, b["sample_count"]))
        out.append(b)
    return out


def _fetch_recent_records(db: Session, organization_id: Optional[int] = None):
    """Pull WorkRecord rows from the lookback window, scoped to one org."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RISK_LOOKBACK_DAYS)
    q = db.query(models.WorkRecord).filter(models.WorkRecord.timestamp >= cutoff)
    if organization_id is not None:
        q = q.filter(models.WorkRecord.organization_id == organization_id)
    return q.order_by(models.WorkRecord.timestamp.desc()).all()


# ══════════════════════════════════════════════
# GET /pow/fraud-events  –  List fraud events
# ══════════════════════════════════════════════
@router.get("/fraud-events")
def list_fraud_events(
    severity: Optional[str] = Query(None, description="Filter: low|medium|high|critical"),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """
    List fraud events. Admins see all events in their org; employees see only
    their own. Cross-org data is never returned.
    """
    q = db.query(models.FraudEvent).order_by(models.FraudEvent.created_at.desc())

    if severity:
        q = q.filter(models.FraudEvent.severity == severity)

    if user.role in ("admin", "hr"):
        # Admins see all fraud events belonging to their organization
        q = q.filter(models.FraudEvent.organization_id == user.organization_id)
    else:
        # Non-admin users only see fraud events linked to their own records
        user_record_ids = (
            db.query(models.WorkRecord.record_id)
            .filter(
                models.WorkRecord.user_id == user.id,
                models.WorkRecord.organization_id == user.organization_id,
            )
            .subquery()
        )
        q = q.filter(models.FraudEvent.wec_id.in_(user_record_ids))

    total = q.count()
    events = q.offset(skip).limit(limit).all()

    return {
        "total": total,
        "fraud_events": [
            {
                "id": e.id,
                "wec_id": e.wec_id,
                "reason": e.reason,
                "severity": e.severity,
                "details": e.details,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
    }


# ══════════════════════════════════════════════
# GET /pow/fraud-events/record/{record_id}
# ══════════════════════════════════════════════
@router.get("/fraud-events/record/{record_id}")
def get_fraud_events_for_record(
    record_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Get all fraud events associated with a specific work record."""
    record = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.record_id == record_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # Return 404 (not 403) for cross-org access to avoid leaking existence
    if record.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Record not found")

    # Non-admin can only see events linked to their own records
    if record.user_id != user.id and user.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Not authorized")

    events = (
        db.query(models.FraudEvent)
        .filter(
            models.FraudEvent.wec_id == record_id,
            models.FraudEvent.organization_id == user.organization_id,
        )
        .order_by(models.FraudEvent.created_at.desc())
        .all()
    )

    return {
        "record_id": record_id,
        "confidence_score": record.confidence_score,
        "validation_status": record.validation_status,
        "fraud_events": [
            {
                "id": e.id,
                "reason": e.reason,
                "severity": e.severity,
                "details": e.details,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
    }


# ══════════════════════════════════════════════
# GET /pow/admin/alerts
# Admin view: flagged 15-min windows (NOT individual 30s screenshots),
# bound to the last RISK_LOOKBACK_DAYS. Same windowing math as the
# employee Work Authenticity tab so the admin's flagged numbers
# reconcile with what each employee sees.
# ══════════════════════════════════════════════
@router.get("/admin/alerts")
def admin_list_alerts(
    limit: int = Query(200, ge=1, le=1000),
    skip:  int = Query(0,   ge=0),
    db:    Session     = Depends(get_db),
    user:  models.User = Depends(get_current_user),
):
    _require_admin(user)

    rows    = _fetch_recent_records(db, organization_id=user.organization_id)
    windows = _aggregate_windows(rows)

    # Only flagged windows surface as alerts. Sort newest-window first
    # so the admin sees the most recent incidents at the top.
    flagged = [w for w in windows if w["status"] == "flagged"]
    flagged.sort(key=lambda w: w["window_start"], reverse=True)

    total = len(flagged)
    page  = flagged[skip : skip + limit]

    # Pull the user rows we need in one trip — scoped to this org
    user_ids = {w["user_id"] for w in page}
    users_by_id = {
        u.id: u for u in
        db.query(models.User).filter(
            models.User.id.in_(user_ids),
            models.User.organization_id == user.organization_id,
        ).all()
    } if user_ids else {}

    alerts = []
    for w in page:
        usr = users_by_id.get(w["user_id"])
        alerts.append({
            # Drill-down hooks: latest_record points at the most recent
            # screenshot inside the window so admins can click through
            # to a real artifact.
            "record_id":         w["latest_record"],
            "user_id":           w["user_id"],
            "user_name":         usr.name  if usr else None,
            "user_email":        usr.email if usr else None,
            "confidence_score":  w["avg_score"],
            "validation_status": w["status"],
            "keyboard_activity": w["keyboard_total"],   # window total
            "mouse_activity":    w["mouse_total"],      # window total
            "sample_count":      w["sample_count"],
            "window_minutes":    _WINDOW_MINUTES,
            "window_start":      w["window_start"].isoformat(),
            "window_end":        w["window_end"].isoformat(),
            "bucket_start":      w["bucket_start"].isoformat(),
            "bucket_end":        w["bucket_end"].isoformat(),
            # `timestamp` was historically the aligned bucket boundary, which
            # could pre-date the worker's actual clock-in by a few minutes and
            # surfaced as a false "activity-before-login" signal. Use the real
            # first-activity timestamp instead.
            "timestamp":         w["window_start"].isoformat(),
            "artifact_url":      w["latest_artifact"],
        })

    return {
        "total":          total,
        "alerts":         alerts,
        "lookback_days":  _RISK_LOOKBACK_DAYS,
        "window_minutes": _WINDOW_MINUTES,
    }


# ══════════════════════════════════════════════
# GET /pow/admin/summary
# Admin view: headline KPIs + top high-risk users, computed from
# 15-min windows over the last RISK_LOOKBACK_DAYS. The numbers
# reconcile with what each employee sees on their Work Authenticity
# tab (same windowing thresholds, same lookback semantics).
# ══════════════════════════════════════════════

# IST-anchored cache so the dashboard risk numbers reset at 00:00 IST
# without serving the prior day's payload for up to a minute via TTL.
# Cache key includes organization_id so org A's payload never
# overwrites org B's entry.
_ADMIN_SUMMARY_CACHE: dict = {}
_ADMIN_SUMMARY_TTL_SEC = 60


def _admin_summary_cache_key(db, top_n: int, organization_id: Optional[int]) -> str:
    # The cache rolls over at THIS organization's midnight. It used to use a
    # module-level India clock, so a US tenant's summary went stale mid-morning
    # and a fresh one appeared at 14:30 local.
    from app import _tz as _tzmod
    today_local = _tzmod.local_date(db, organization_id).isoformat()
    return f"pow:admin_summary:{organization_id}:{top_n}:{today_local}"


@router.get("/admin/summary")
def admin_fraud_summary(
    top_n: int = Query(10, ge=1, le=50),
    db:    Session     = Depends(get_db),
    user:  models.User = Depends(get_current_user),
):
    _require_admin(user)

    cache_key = _admin_summary_cache_key(db, top_n, user.organization_id)
    cached    = _ADMIN_SUMMARY_CACHE.get(cache_key)
    if cached and cached["expires"] > datetime.now(timezone.utc):
        return cached["data"]

    rows    = _fetch_recent_records(db, organization_id=user.organization_id)
    windows = _aggregate_windows(rows)

    # Headline counts (in WINDOWS, not in screenshots — matches employee
    # tab semantics).
    total_records  = len(windows)
    total_flagged  = sum(1 for w in windows if w["status"] == "flagged")
    total_warnings = sum(1 for w in windows if w["status"] == "warning")

    score_total    = sum(w["avg_score"] for w in windows)
    avg_confidence = round(score_total / total_records, 1) if total_records else 0.0

    # Per-user aggregation. We re-bucket the windows by user_id, which
    # is cheap because windows are already at most 96/day per user (24h
    # * 4 windows/hr).
    user_ids: Dict[int, dict] = {}
    for w in windows:
        u = user_ids.get(w["user_id"])
        if u is None:
            u = {
                "user_id":         w["user_id"],
                "total_windows":   0,
                "flagged_windows": 0,
                "warning_windows": 0,
                "score_sum":       0,
            }
            user_ids[w["user_id"]] = u
        u["total_windows"]   += 1
        u["score_sum"]       += w["avg_score"]
        if w["status"] == "flagged":
            u["flagged_windows"] += 1
        elif w["status"] == "warning":
            u["warning_windows"] += 1

    # Resolve user names in a single query — scoped to this org
    if user_ids:
        users = db.query(models.User).filter(
            models.User.id.in_(user_ids.keys()),
            models.User.organization_id == user.organization_id,
        ).all()
        users_by_id = {u.id: u for u in users}
    else:
        users_by_id = {}

    risk_users = []
    for uid, agg in user_ids.items():
        usr      = users_by_id.get(uid)
        avg_conf = round(agg["score_sum"] / max(1, agg["total_windows"]), 1)

        if avg_conf < _HIGH_RISK_AVG_SCORE or agg["flagged_windows"] >= _HIGH_RISK_FLAGGED_WINDOWS:
            risk = "High"
        elif avg_conf < _MED_RISK_AVG_SCORE or agg["flagged_windows"] >= 1:
            risk = "Medium"
        else:
            risk = "Low"

        risk_users.append({
            "user_id":          uid,
            "user_name":        usr.name  if usr else None,
            "user_email":       usr.email if usr else None,
            "total_records":    agg["total_windows"],
            # Kept the legacy field name `flagged_sessions` so the
            # frontend that reads d.flagged_sessions keeps working.
            # The number now means "flagged 15-min windows".
            "flagged_sessions": agg["flagged_windows"],
            "warning_sessions": agg["warning_windows"],
            "avg_confidence":   avg_conf,
            "risk_level":       risk,
        })

    risk_users.sort(key=lambda r: (-r["flagged_sessions"], r["avg_confidence"]))
    top_risk = risk_users[:top_n]

    payload = {
        "total_users":     len(user_ids),
        "total_records":   total_records,    # = total windows
        "total_flagged":   total_flagged,    # = flagged windows
        "total_warnings":  total_warnings,
        "avg_confidence":  avg_confidence,
        "high_risk_count": sum(1 for r in risk_users if r["risk_level"] == "High"),
        "risk_users":      top_risk,
        "lookback_days":   _RISK_LOOKBACK_DAYS,
        "window_minutes":  _WINDOW_MINUTES,
    }

    _ADMIN_SUMMARY_CACHE[cache_key] = {
        "data":    payload,
        "expires": datetime.now(timezone.utc) + timedelta(seconds=_ADMIN_SUMMARY_TTL_SEC),
    }
    return payload
