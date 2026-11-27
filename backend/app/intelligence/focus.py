"""
Deep focus session detection.

A focus session is a continuous block of work on a single domain (or
single app for non-browser work) lasting at least FOCUS_MIN_MINUTES,
with no inter-tab gap exceeding FOCUS_GAP_SECONDS. The depth_score is
a 0..1 estimate of how single-tasking the block was, factoring in:

    • idle ratio (high idle → low depth)
    • tab-switch density (many switches → low depth)
    • category alignment (all-work → high depth, mixed → lower)

The output drives the FocusTimeline component and the "deep work min"
field on the daily summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from database import models
from app.browser.productivity import categorise, load_overrides

FOCUS_MIN_MINUTES = 20            # blocks shorter than this don't qualify
FOCUS_GAP_SECONDS = 90            # any gap longer than this ends the block
DEEP_FOCUS_MINUTES = 45           # "deep" tier — used by the daily summary


@dataclass
class FocusBlock:
    started_at: datetime
    ended_at: datetime
    duration_sec: int
    primary_domain: Optional[str]
    primary_category: str
    depth_score: float
    tab_switches: int
    keystrokes: int
    mouse_clicks: int
    is_deep: bool

    def as_dict(self) -> dict:
        return {
            "started_at":     self.started_at.isoformat(),
            "ended_at":       self.ended_at.isoformat(),
            "duration_sec":   self.duration_sec,
            "primary_domain": self.primary_domain,
            "category":       self.primary_category,
            "depth_score":    round(self.depth_score, 3),
            "tab_switches":   self.tab_switches,
            "keystrokes":     self.keystrokes,
            "mouse_clicks":   self.mouse_clicks,
            "is_deep":        self.is_deep,
        }


def detect_focus_sessions(
    db: Session,
    user_id: int,
    day_start: datetime,
    day_end: datetime,
) -> List[FocusBlock]:
    """Return all focus blocks for a user inside the date range."""
    rows = (
        db.query(models.BrowserActivity)
        .filter(
            models.BrowserActivity.user_id == user_id,
            models.BrowserActivity.started_at >= day_start,
            models.BrowserActivity.started_at < day_end,
        )
        .order_by(models.BrowserActivity.started_at.asc())
        .all()
    )
    if not rows:
        return []

    # Domain categories are per-organization policy (DomainCategory carries
    # organization_id). Calling load_overrides without one loaded every
    # tenant's rows, so org A's classification of a domain silently changed
    # org B's focus/workflow results.
    _org_id = (
        db.query(models.User.organization_id)
        .filter(models.User.id == user_id)
        .scalar()
    )
    overrides = load_overrides(db, _org_id)
    blocks: List[FocusBlock] = []
    current: List[models.BrowserActivity] = []
    last_end: Optional[datetime] = None

    for r in rows:
        if last_end is None:
            current = [r]
        else:
            gap = (r.started_at - last_end).total_seconds()
            if gap <= FOCUS_GAP_SECONDS:
                current.append(r)
            else:
                _emit_block(current, blocks, overrides)
                current = [r]
        last_end = r.ended_at or r.started_at
    _emit_block(current, blocks, overrides)

    return blocks


def materialise_focus_sessions(
    db: Session,
    user_id: int,
    day_start: datetime,
    day_end: datetime,
) -> int:
    """Persist focus_sessions rows for this user/day. Idempotent.

    Returns the number of rows written."""
    blocks = detect_focus_sessions(db, user_id, day_start, day_end)
    db.query(models.FocusSession).filter(
        models.FocusSession.user_id == user_id,
        models.FocusSession.started_at >= day_start,
        models.FocusSession.started_at < day_end,
    ).delete(synchronize_session=False)

    n = 0
    for b in blocks:
        db.add(models.FocusSession(
            user_id        = user_id,
            started_at     = b.started_at,
            ended_at       = b.ended_at,
            duration_sec   = b.duration_sec,
            primary_domain = b.primary_domain,
            primary_app    = None,
            category       = b.primary_category,
            depth_score    = b.depth_score,
            tab_switches   = b.tab_switches,
            keystrokes     = b.keystrokes,
            mouse_clicks   = b.mouse_clicks,
        ))
        n += 1
    if n:
        db.commit()
    return n


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _emit_block(cluster, out: List[FocusBlock], overrides: dict) -> None:
    if not cluster:
        return
    total_sec = sum(int(c.duration_sec or 0) for c in cluster)
    if total_sec < FOCUS_MIN_MINUTES * 60:
        return

    # primary domain = the one with the most duration in the cluster
    by_domain: dict = {}
    for c in cluster:
        d = c.domain or "unknown"
        by_domain[d] = by_domain.get(d, 0) + int(c.duration_sec or 0)
    primary = max(by_domain.items(), key=lambda kv: kv[1])[0]

    cat = categorise(primary, overrides)
    if cat == "distraction":
        # We don't call a 30-minute YouTube scroll "deep focus".
        return

    idle_total = sum(int(c.idle_sec or 0) for c in cluster)
    active_total = max(0, total_sec - idle_total)
    keystrokes = sum(int(c.key_presses or 0) for c in cluster)
    clicks = sum(int(c.mouse_clicks or 0) for c in cluster)
    tab_switches = max(0, len(cluster) - 1)

    # Depth signals
    idle_ratio = (idle_total / total_sec) if total_sec else 0
    primary_share = (by_domain[primary] / total_sec) if total_sec else 0
    switch_rate_per_hour = (tab_switches / total_sec) * 3600 if total_sec else 0

    depth_score = (
        0.45 * primary_share                       # single-tasking weight
        + 0.30 * (1.0 - min(1.0, idle_ratio))      # interaction weight
        + 0.25 * (1.0 - min(1.0, switch_rate_per_hour / 30))  # >30 switches/hr → 0
    )
    depth_score = max(0.0, min(1.0, depth_score))

    out.append(FocusBlock(
        started_at        = cluster[0].started_at,
        ended_at          = cluster[-1].ended_at or cluster[-1].started_at,
        duration_sec      = total_sec,
        primary_domain    = primary,
        primary_category  = cat,
        depth_score       = depth_score,
        tab_switches      = tab_switches,
        keystrokes        = keystrokes,
        mouse_clicks      = clicks,
        is_deep           = total_sec >= DEEP_FOCUS_MINUTES * 60 and depth_score >= 0.6,
    ))
