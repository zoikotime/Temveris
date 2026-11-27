"""
Active Time Engine — reconcile worker.

A lifespan loop (same contract as breaks/monitor.run_forever) that periodically
recomputes the authoritative daily Active Time score for every user who produced
a tracker window recently. Decouples scoring from ingest so:

  * ingest stays a single cheap insert (no per-request scan/scoring),
  * the dashboard reads a pre-computed, indexed daily row (1 query for all users),
  * continuity-aware scoring + write-once minute finalisation run in the batch.

Scales to many workers: each tick only touches users active in the last
``LOOKBACK_SECONDS`` and does one bounded recompute per user.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import database as _db
from . import service

_log = logging.getLogger("active_time.worker")

# Only recompute users who produced a window within this lookback — keeps each
# tick proportional to currently-active workers, not the whole org. The floor
# is 180s; the effective value scales with the configured screenshot interval
# (see _effective_lookback) so a tracker firing every 5–30 min is still
# re-scored on every tick — and a missed tick (restart) self-heals — instead of
# only in the brief window right after each screenshot.
LOOKBACK_SECONDS = 180
LOOKBACK_MAX_SECONDS = 3600


def _effective_lookback(db) -> int:
    """max(180, 2×screenshot_interval + 60), capped at 1h.

    Deliberately org-agnostic: this sizes ONE cross-tenant reconcile window, so
    it uses the system default rather than any single org's interval.
    """
    try:
        from app.tracking.router import _get_configured_screenshot_interval
        interval = int(_get_configured_screenshot_interval(db) or 30)
    except Exception:
        interval = 30
    return max(LOOKBACK_SECONDS, min(LOOKBACK_MAX_SECONDS, 2 * interval + 60))


def run_once_sync(*, lookback_seconds: int | None = None) -> dict:
    """One reconcile pass over a fresh DB session. Returns a log/test summary."""
    db = _db.SessionLocal()
    summary = {"users": 0, "recomputed": 0, "errors": 0}
    try:
        lb = lookback_seconds if lookback_seconds is not None else _effective_lookback(db)
        since = datetime.now(timezone.utc) - timedelta(seconds=lb)
        user_ids = service.active_users_with_windows_since(db, since)
        summary["users"] = len(user_ids)
        today = service.ist_today()
        # This reconcile spans every tenant, so each user's scoring config must be
        # loaded for THEIR org. One lookup for the whole tick, rather than letting
        # recompute_daily fall back to defaults (which would score the same user
        # differently here than on the request path).
        org_by_user = {}
        if user_ids:
            try:
                from database import models as _models
                org_by_user = {
                    r.id: r.organization_id
                    for r in db.query(_models.User.id, _models.User.organization_id)
                               .filter(_models.User.id.in_(user_ids)).all()
                }
            except Exception:
                org_by_user = {}
        for uid in user_ids:
            try:
                daily = service.recompute_daily(db, uid, today, org_id=org_by_user.get(uid))
                if daily is not None:
                    summary["recomputed"] += 1
            except Exception as e:  # pragma: no cover - one bad user can't stall the tick
                summary["errors"] += 1
                _log.warning("active-time reconcile: user=%s failed: %r", uid, e)
                try:
                    db.rollback()
                except Exception:
                    pass
        return summary
    finally:
        db.close()


async def run_forever(interval_seconds: int = 60) -> None:
    """Lifespan loop — runs until cancelled at shutdown. Each tick is isolated."""
    _log.info("active-time reconcile worker started (interval=%ss)", interval_seconds)
    while True:
        try:
            await asyncio.to_thread(run_once_sync)
        except asyncio.CancelledError:
            _log.info("active-time reconcile worker cancelled, exiting")
            raise
        except Exception as e:  # pragma: no cover - never let a bad tick kill the loop
            _log.warning("active-time reconcile tick failed (will retry): %r", e)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
