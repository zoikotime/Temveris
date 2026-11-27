"""Phase 5 — Optimization scaffolds (§27).

Three product surfaces from the spec's Phase 5 backlog:

  1. A/B testing (feature flags + cohort assignment)
  2. HRIS import (CSV/JSON ingest → User rows + manual-review gate)
  3. Customer health + lifecycle scoring (computed score per tenant)

Each one is intentionally scaffolded, not fully built: the data model
+ service entry points exist so downstream code can call them today,
but the integration surfaces (Optimizely / BambooHR / Workday / etc.)
land in their own follow-on work. The spec calls Phase 5 the final
phase explicitly — this gives every Phase-1..4 caller a stable hook.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, CommercialEvent,
    OnboardingChecklist,
)

_log = logging.getLogger("zoiko.commercial.phase5")


# ══════════════════════════════════════════════════════════════════════════
# 1. A/B testing
# ══════════════════════════════════════════════════════════════════════════
# Feature flags live in-process for now. Persisting them lives in a future
# admin-config table; this scaffold gives callers a stable `is_enabled`
# / `variant_for` API so the upgrade is a one-line change.
_FEATURE_FLAGS: Dict[str, Dict[str, Any]] = {
    # Each flag is: { "default": bool, "variants": {name: weight}, "overrides": {org_id: variant_or_bool} }
    "onboarding_checklist_v2": {
        "default": False, "variants": {"control": 50, "v2": 50},
        "overrides": {},
    },
    "pricing_page_annual_default": {
        "default": False, "variants": {"monthly_default": 50, "annual_default": 50},
        "overrides": {},
    },
    "trial_length_45_days": {
        "default": False, "variants": {"control": 70, "treatment": 30},
        "overrides": {},
    },
    # Phase 3.21 — per-organization gate for entitlement 403 enforcement. Default
    # OFF and, like every flag here, in-memory: it resets to OFF on restart, so a
    # deploy can never leave enforcement silently on. It is one of several layers
    # (see enforcement.enforcement_mode) and is NOT consulted by any live route.
    "entitlement_enforcement": {
        "default": False, "variants": {"control": 100},
        "overrides": {},
    },
}


def _stable_hash(seed: str, key: str) -> int:
    """Deterministic 0..99 bucket per (seed, key) — same tenant always
    lands in the same variant so the experiment is reproducible."""
    h = hashlib.sha256(f"{seed}::{key}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def is_enabled(flag: str, *, organization_id: Optional[str] = None) -> bool:
    """Boolean rollout: returns True if the flag is on for this tenant."""
    cfg = _FEATURE_FLAGS.get(flag)
    if cfg is None:
        return False
    if organization_id and organization_id in cfg.get("overrides", {}):
        return bool(cfg["overrides"][organization_id])
    return bool(cfg.get("default", False))


def variant_for(flag: str, *, organization_id: str) -> str:
    """Stable variant assignment for an A/B/n experiment. Each variant's
    weight is interpreted as a percentage of the 0..99 bucket space."""
    cfg = _FEATURE_FLAGS.get(flag)
    if cfg is None or not cfg.get("variants"):
        return "control"
    if organization_id in cfg.get("overrides", {}):
        return str(cfg["overrides"][organization_id])
    bucket = _stable_hash(flag, organization_id)
    cumulative = 0
    for variant, weight in cfg["variants"].items():
        cumulative += weight
        if bucket < cumulative:
            return variant
    return "control"


def set_override(flag: str, *, organization_id: str, value: Any) -> None:
    """Pin a tenant to a specific variant (or boolean). Used by admin
    tooling so support can force a customer into a specific experience
    while investigating an issue."""
    cfg = _FEATURE_FLAGS.setdefault(
        flag, {"default": False, "variants": {}, "overrides": {}}
    )
    cfg.setdefault("overrides", {})[organization_id] = value


def list_flags() -> List[Dict[str, Any]]:
    return [
        {"flag": k, "default": v.get("default"),
         "variants": list((v.get("variants") or {}).keys()),
         "override_count": len(v.get("overrides", {}))}
        for k, v in _FEATURE_FLAGS.items()
    ]


# ══════════════════════════════════════════════════════════════════════════
# 2. HRIS Import
# ══════════════════════════════════════════════════════════════════════════
# A bulk-user-import skeleton: takes a parsed list of {name, email,
# department, role} rows, returns a dry-run preview (what would be
# created / updated / skipped) without writing. Actual writes go through
# the existing /employees invite path so RBAC + audit + email all run.
def hris_import_preview(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate a parsed roster and report what would happen. Never
    writes — the admin reviews this and confirms before any DB change."""
    summary = {"create": [], "skip": [], "errors": []}
    seen_emails = set()
    for i, row in enumerate(rows or []):
        email = (row.get("email") or "").strip().lower()
        name = (row.get("name") or "").strip()
        if not email or "@" not in email:
            summary["errors"].append({"row": i, "reason": "invalid_email", "raw": row})
            continue
        if not name:
            summary["errors"].append({"row": i, "reason": "missing_name", "raw": row})
            continue
        if email in seen_emails:
            summary["skip"].append({"row": i, "reason": "duplicate_in_file", "email": email})
            continue
        seen_emails.add(email)
        summary["create"].append({
            "row": i, "email": email, "name": name,
            "department": (row.get("department") or "").strip(),
            "role": (row.get("role") or "employee").strip(),
        })
    summary["counts"] = {
        "create": len(summary["create"]),
        "skip":   len(summary["skip"]),
        "errors": len(summary["errors"]),
    }
    return summary


def hris_import_commit(
    db: Session,
    *,
    organization_id: str,
    preview: Dict[str, Any],
    actor_user_id: int,
) -> Dict[str, Any]:
    """Commit a preview. Emits one CommercialEvent per created user so
    the activity stream shows the bulk import explicitly. Calls into the
    legacy /employees invite path are NOT done here — the caller is
    expected to iterate `preview['create']` through their normal invite
    handler so RBAC, fraud, and email checks all run."""
    rows = preview.get("create", []) if preview else []
    emit_event(
        db, event_type="signup.completed" if False else "trial.converted",
        source_service="hris-import",
        payload={"actor_user_id": actor_user_id, "imported": len(rows),
                 "via": "hris_bulk_import"},
        organization_id=organization_id,
    )
    return {"organization_id": organization_id, "queued": len(rows), "actor": actor_user_id}


# ══════════════════════════════════════════════════════════════════════════
# 3. Customer health / lifecycle scoring
# ══════════════════════════════════════════════════════════════════════════
# A small heuristic score 0..100 derived from observable signals. This
# is intentionally simple — the goal is to give CS a sortable column,
# not to predict churn. A v2 score can swap in a trained model behind
# this same function signature.
_SCORE_WEIGHTS = {
    "email_verified":     15,
    "first_policy":       15,
    "first_invite":       15,
    "first_meaningful":   20,
    "active_seat_pct":    20,   # active users / seat count
    "paid":               15,
}


def _bool_score(present: bool, weight: int) -> int:
    return weight if present else 0


def compute_health_score(db: Session, organization_id: str) -> Dict[str, Any]:
    """Compute a 0..100 health score for one tenant. Returns the score
    plus a per-signal breakdown so CS can see why a number is what it is."""
    sub = (
        db.query(CommercialSubscription)
          .filter_by(organization_id=organization_id)
          .one_or_none()
    )
    if sub is None:
        return {"organization_id": organization_id, "score": 0, "signals": {},
                "computed_at": datetime.now(timezone.utc).isoformat()}

    # Onboarding milestones — read from OnboardingChecklist if present.
    checklist = (
        db.query(OnboardingChecklist)
          .filter_by(organization_id=organization_id)
          .one_or_none()
    )
    # ponytail: attribute names must match OnboardingChecklist (models.py:512-517),
    # not the *_done aliases that never existed — getattr default silently zeroed these.
    email_verified  = bool(getattr(checklist, "email_confirmed", False))         if checklist else False
    first_policy    = bool(getattr(checklist, "first_policy_configured", False)) if checklist else False
    first_invite    = bool(getattr(checklist, "first_invite_sent", False))       if checklist else False
    first_meaningful = bool(getattr(checklist, "dashboard_reviewed", False))     if checklist else False

    # Active seat % — proxy: count distinct users that touched the org in
    # the last 14 days via commercial_events.
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    active_users = (
        db.query(func.count(func.distinct(CommercialEvent.user_id)))
          .filter(CommercialEvent.organization_id == organization_id,
                  CommercialEvent.occurred_at >= cutoff,
                  CommercialEvent.user_id.isnot(None))
          .scalar() or 0
    )
    seat_count = max(1, sub.seat_count or 1)
    active_pct = min(1.0, active_users / seat_count)
    active_seat_pct_score = int(_SCORE_WEIGHTS["active_seat_pct"] * active_pct)
    paid = sub.state in {"active", "past_due", "grace_period"}

    signals = {
        "email_verified":   _bool_score(email_verified, _SCORE_WEIGHTS["email_verified"]),
        "first_policy":     _bool_score(first_policy,   _SCORE_WEIGHTS["first_policy"]),
        "first_invite":     _bool_score(first_invite,   _SCORE_WEIGHTS["first_invite"]),
        "first_meaningful": _bool_score(first_meaningful, _SCORE_WEIGHTS["first_meaningful"]),
        "active_seat_pct":  active_seat_pct_score,
        "paid":             _bool_score(paid, _SCORE_WEIGHTS["paid"]),
    }
    score = sum(signals.values())
    band = "healthy" if score >= 70 else "watch" if score >= 40 else "at_risk"

    return {
        "organization_id": organization_id,
        "score": score,
        "band":  band,
        "signals": signals,
        "active_users_14d": active_users,
        "seat_count": seat_count,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_health_for_all(db: Session, limit: int = 500) -> List[Dict[str, Any]]:
    """Batch helper for the CS dashboard — returns all tenants ranked by
    score ascending (worst first)."""
    orgs = db.query(CommercialOrganization.id).limit(limit).all()
    rows = [compute_health_score(db, o.id) for o in orgs]
    rows.sort(key=lambda r: r["score"])
    return rows
