"""
Temveris – App Review Router  (Admin governance over desktop apps)
====================================================================
Admin-facing endpoints that power the "App Review" page in the dashboard.
Mirrors `app/browser/router.py` (websites) but for desktop applications
detected by the tracker.

Endpoints
---------
GET    /app-review/catalog         Distinct apps + usage stats + current category.
GET    /app-review/pending-count   Banner count of unreviewed apps.
POST   /app-review/category        Upsert one app's category.
POST   /app-review/bulk            Bulk-set the same category on multiple apps.
POST   /app-review/auto-suggest    Apply heuristic categories to unreviewed apps.
DELETE /app-review/category/{app}  Remove an admin override (revert to default).
GET    /app-review/stats           Org-wide rollup for the dashboard "Productive Apps %".

Security
--------
All endpoints are admin/HR-only. All AppCategory reads and writes are scoped
to the caller's organization_id — org A cannot see or modify org B's overrides.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, desc
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from app.app_review.productivity import (
    VALID_CATEGORIES,
    VALID_SOURCES,
    CATEGORY_PRODUCTIVE,
    CATEGORY_NEUTRAL,
    CATEGORY_DISTRACTING,
    CATEGORY_BLOCKED,
    CATEGORY_WEIGHT,
    DEFAULT_CATEGORIES,
    auto_suggest,
    categorise,
    load_overrides,
    _normalize_app,
)

router = APIRouter(prefix="/app-review", tags=["App Review"])


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _require_admin(user: models.User):
    if getattr(user, "role", "") not in ("admin", "hr"):
        raise HTTPException(403, "Admin only")


def _utc_window(days: int):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, days))
    return start, now


# ──────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────
class CategoryIn(BaseModel):
    app_name: str
    category: str = Field(..., description="productive | neutral | distracting | blocked")
    notes: Optional[str] = None


class BulkCategoryIn(BaseModel):
    app_names: List[str]
    category: str = Field(..., description="productive | neutral | distracting | blocked")


# ──────────────────────────────────────────────────────────────────────
# GET /app-review/catalog
# ──────────────────────────────────────────────────────────────────────
@router.get("/catalog")
def catalog(
    days: int = Query(14, ge=1, le=180, description="Window of activity to roll up usage stats over"),
    category: Optional[str] = Query(
        None,
        description="Filter by current category — productive | neutral | distracting | blocked | uncategorized",
    ),
    search: Optional[str] = Query(None, description="Substring match on app name"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """List every distinct app the tracker has seen in the last `days`,
    aggregated with its usage stats and current category.

    Only activities and overrides belonging to the caller's organization are
    returned — cross-org data is never visible."""
    _require_admin(user)
    start, _ = _utc_window(days)
    org_id = user.organization_id

    # Activities scoped to org
    SECONDS_PER_ROW = 30
    rows = (
        db.query(
            models.Activity.app_name.label("app_name"),
            func.count(models.Activity.id).label("samples"),
            func.count(func.distinct(models.Activity.user_id)).label("user_count"),
            func.max(models.Activity.timestamp).label("last_seen"),
            func.min(models.Activity.timestamp).label("first_seen"),
        )
        .filter(
            models.Activity.organization_id == org_id,
            models.Activity.app_name.isnot(None),
            models.Activity.app_name != "",
            models.Activity.timestamp >= start,
        )
        .group_by(models.Activity.app_name)
        .order_by(desc("samples"))
        .all()
    )

    # Overrides scoped to org
    over_rows = (
        db.query(models.AppCategory)
        .filter(models.AppCategory.organization_id == org_id)
        .all()
    )
    overrides = {_normalize_app(r.app_name): r for r in over_rows}

    out = []
    for r in rows:
        if not r.app_name:
            continue
        norm = _normalize_app(r.app_name)
        ov = overrides.get(norm)
        suggested = auto_suggest(r.app_name)
        if ov:
            cur_category = ov.category
            cur_source = ov.source
            reviewed = True
            updated_at = ov.updated_at.isoformat() if ov.updated_at else None
            notes = ov.notes
        else:
            cur_category = DEFAULT_CATEGORIES.get(norm) or DEFAULT_CATEGORIES.get(
                norm.split()[0] if norm else ""
            ) or "uncategorized"
            cur_source = "default" if cur_category != "uncategorized" else "none"
            reviewed = False
            updated_at = None
            notes = None

        if search and search.lower() not in r.app_name.lower():
            continue
        if category:
            wanted = category.lower()
            if wanted == "uncategorized":
                if reviewed:
                    continue
            elif cur_category != wanted:
                continue

        out.append({
            "app_name":       r.app_name,
            "normalized":     norm,
            "category":       cur_category,
            "source":         cur_source,
            "reviewed":       reviewed,
            "suggested":      suggested,
            "notes":          notes,
            "samples":        int(r.samples or 0),
            "user_count":     int(r.user_count or 0),
            "approx_seconds": int((r.samples or 0) * SECONDS_PER_ROW),
            "first_seen":     r.first_seen.isoformat() if r.first_seen else None,
            "last_seen":      r.last_seen.isoformat() if r.last_seen else None,
            "updated_at":     updated_at,
        })
    return {
        "window_days": days,
        "items":       out,
        "total":       len(out),
    }


# ──────────────────────────────────────────────────────────────────────
# GET /app-review/pending-count
# ──────────────────────────────────────────────────────────────────────
@router.get("/pending-count")
def pending_count(
    days: int = Query(7, ge=1, le=60),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    start, _ = _utc_window(days)
    org_id = user.organization_id

    seen_rows = (
        db.query(models.Activity.app_name)
        .filter(
            models.Activity.organization_id == org_id,
            models.Activity.app_name.isnot(None),
            models.Activity.app_name != "",
            models.Activity.timestamp >= start,
        )
        .distinct()
        .all()
    )
    # Only this org's overrides count as "reviewed"
    overrides = {
        _normalize_app(r.app_name)
        for r in db.query(models.AppCategory.app_name)
        .filter(models.AppCategory.organization_id == org_id)
        .all()
    }
    pending = sum(
        1
        for (name,) in seen_rows
        if _normalize_app(name) and _normalize_app(name) not in overrides
    )
    return {"pending": pending, "window_days": days}


# ──────────────────────────────────────────────────────────────────────
# POST /app-review/category — upsert one
# ──────────────────────────────────────────────────────────────────────
@router.post("/category")
def upsert_category(
    payload: CategoryIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    if payload.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(VALID_CATEGORIES)}")
    norm = _normalize_app(payload.app_name)
    if not norm:
        raise HTTPException(400, "app_name is required")
    org_id = user.organization_id

    row = (
        db.query(models.AppCategory)
        .filter(
            models.AppCategory.app_name == norm,
            models.AppCategory.organization_id == org_id,
        )
        .first()
    )
    if row:
        row.category      = payload.category
        row.source        = "manual"
        row.notes         = payload.notes
        row.updated_by_id = user.id
    else:
        row = models.AppCategory(
            organization_id = org_id,
            app_name        = norm,
            category        = payload.category,
            source          = "manual",
            notes           = payload.notes,
            updated_by_id   = user.id,
        )
        db.add(row)
    db.commit()
    return {"success": True, "app_name": norm, "category": payload.category}


# ──────────────────────────────────────────────────────────────────────
# POST /app-review/bulk
# ──────────────────────────────────────────────────────────────────────
@router.post("/bulk")
def bulk_set(
    payload: BulkCategoryIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    if payload.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(VALID_CATEGORIES)}")
    if not payload.app_names:
        return {"success": True, "updated": 0}
    org_id = user.organization_id

    updated = 0
    for raw in payload.app_names:
        norm = _normalize_app(raw)
        if not norm:
            continue
        row = (
            db.query(models.AppCategory)
            .filter(
                models.AppCategory.app_name == norm,
                models.AppCategory.organization_id == org_id,
            )
            .first()
        )
        if row:
            row.category      = payload.category
            row.source        = "manual"
            row.updated_by_id = user.id
        else:
            db.add(models.AppCategory(
                organization_id = org_id,
                app_name        = norm,
                category        = payload.category,
                source          = "manual",
                updated_by_id   = user.id,
            ))
        updated += 1
    db.commit()
    return {"success": True, "updated": updated, "category": payload.category}


# ──────────────────────────────────────────────────────────────────────
# POST /app-review/auto-suggest
# ──────────────────────────────────────────────────────────────────────
@router.post("/auto-suggest")
def auto_suggest_all(
    days: int = Query(14, ge=1, le=180),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    start, _ = _utc_window(days)
    org_id = user.organization_id

    seen_rows = (
        db.query(models.Activity.app_name)
        .filter(
            models.Activity.organization_id == org_id,
            models.Activity.app_name.isnot(None),
            models.Activity.app_name != "",
            models.Activity.timestamp >= start,
        )
        .distinct()
        .all()
    )
    # Only check existing rows for this org
    existing = {
        r.app_name
        for r in db.query(models.AppCategory.app_name)
        .filter(models.AppCategory.organization_id == org_id)
        .all()
    }

    created = 0
    skipped = 0
    for (name,) in seen_rows:
        norm = _normalize_app(name)
        if not norm or norm in existing:
            skipped += 1
            continue
        suggested = auto_suggest(name)
        if not suggested:
            skipped += 1
            continue
        db.add(models.AppCategory(
            organization_id = org_id,
            app_name        = norm,
            category        = suggested,
            source          = "auto",
            updated_by_id   = user.id,
        ))
        created += 1
    db.commit()
    return {"success": True, "created": created, "skipped": skipped}


# ──────────────────────────────────────────────────────────────────────
# DELETE /app-review/category/{app_name}
# ──────────────────────────────────────────────────────────────────────
@router.delete("/category/{app_name:path}")
def delete_category(
    app_name: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    norm = _normalize_app(app_name)
    org_id = user.organization_id

    row = (
        db.query(models.AppCategory)
        .filter(
            models.AppCategory.app_name == norm,
            models.AppCategory.organization_id == org_id,
        )
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return {"success": True, "app_name": norm}


# ──────────────────────────────────────────────────────────────────────
# GET /app-review/stats
# ──────────────────────────────────────────────────────────────────────
@router.get("/stats")
def stats(
    days: int = Query(7, ge=1, le=180),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    start, _ = _utc_window(days)
    org_id = user.organization_id

    rows = (
        db.query(
            models.Activity.app_name.label("app_name"),
            func.count(models.Activity.id).label("samples"),
        )
        .filter(
            models.Activity.organization_id == org_id,
            models.Activity.app_name.isnot(None),
            models.Activity.app_name != "",
            models.Activity.timestamp >= start,
        )
        .group_by(models.Activity.app_name)
        .all()
    )
    overrides = load_overrides(db, org_id)

    by_cat = {
        CATEGORY_PRODUCTIVE:  0,
        CATEGORY_NEUTRAL:     0,
        CATEGORY_DISTRACTING: 0,
        CATEGORY_BLOCKED:     0,
    }
    distinct_apps = 0
    distinct_pending = 0
    for r in rows:
        distinct_apps += 1
        cat = categorise(r.app_name, overrides)
        by_cat[cat] = by_cat.get(cat, 0) + int(r.samples or 0)
        if _normalize_app(r.app_name) not in overrides:
            distinct_pending += 1

    total = sum(by_cat.values()) or 1
    weighted = sum(CATEGORY_WEIGHT[c] * by_cat[c] for c in by_cat)
    productive_pct = round((weighted / total) * 100, 1)

    return {
        "window_days":    days,
        "distinct_apps":  distinct_apps,
        "pending_review": distinct_pending,
        "samples_total":  sum(by_cat.values()),
        "productive_pct": productive_pct,
        "by_category":    by_cat,
    }
