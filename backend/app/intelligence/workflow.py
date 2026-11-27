"""
Workflow reconstruction — sequence detection on top of BrowserActivity.

Given a day of tab sessions, this module clusters tabs into "workflows"
(sequences of related domains within a short time window) and labels
them with a heuristic role-aware tag. The output drives the
WorkflowRibbon component on the cinematic admin view, e.g.:

    GitHub → VS Code → Docker → Postman   (Backend deploy flow)
    Figma → Notion → Slack                (Design review flow)
    Jira → Slack → Calendar               (Standup / planning flow)

The classifier is intentionally heuristic — no LLM call per request — so
the endpoint stays fast. Role-aware tags use the labelled domain sets
below; if a workflow's domains don't fit any signature, the label
collapses to "Mixed work".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from database import models
from app.browser.productivity import categorise, load_overrides

# A workflow groups tab sessions whose gap is at most this many seconds.
WORKFLOW_GAP_SECONDS = 5 * 60          # 5 minutes
WORKFLOW_MIN_HOPS = 2                  # require ≥ 2 distinct domains to count
WORKFLOW_MIN_DURATION_SEC = 60         # ignore micro-clusters under 1 min total

# Domain → signature tag. Each signature has a label + a set of
# "anchor" domains. A workflow gets a signature label if ≥ 1 of its
# domains is an anchor for that signature.
_SIGNATURES: List[Tuple[str, set]] = [
    ("Backend engineering", {
        "github.com", "gitlab.com", "bitbucket.org",
        "stackoverflow.com", "docker.com", "hub.docker.com",
        "npmjs.com", "pypi.org",
        "console.aws.amazon.com", "cloud.google.com", "azure.microsoft.com",
        "vercel.com", "netlify.com", "heroku.com",
    }),
    ("Frontend / design", {
        "figma.com", "miro.com", "framer.com", "dribbble.com", "behance.net",
        "tailwindcss.com", "mui.com",
    }),
    ("Product / planning", {
        "jira.atlassian.com", "atlassian.com", "linear.app",
        "notion.so", "asana.com", "trello.com", "monday.com",
        "shortcut.com", "productboard.com",
    }),
    ("Collaboration / comms", {
        "slack.com", "teams.microsoft.com", "meet.google.com", "zoom.us",
        "discord.com", "webex.com",
    }),
    ("Customer / GTM", {
        "salesforce.com", "hubspot.com", "intercom.com",
        "freshworks.com", "zendesk.com", "pipedrive.com",
    }),
    ("Research / docs", {
        "developer.mozilla.org", "docs.python.org", "fastapi.tiangolo.com",
        "reactjs.org", "react.dev", "wikipedia.org",
        "chatgpt.com", "claude.ai", "gemini.google.com",
    }),
    ("Recruiting", {
        "linkedin.com", "indeed.com", "lever.co", "greenhouse.io", "ashbyhq.com",
    }),
]

# Role-context overrides for "is this domain productive for THIS person?"
# Mapping from a user.department / user.designation (lower-cased, free-form
# substring match) → set of domains that flip from neutral/distraction to
# work. Example: "recruiter" → linkedin.com counts as work.
_ROLE_PROMOTIONS: List[Tuple[str, set]] = [
    ("recruit",  {"linkedin.com"}),
    ("sales",    {"linkedin.com"}),
    ("market",   {"twitter.com", "x.com", "instagram.com", "tiktok.com", "youtube.com"}),
    ("design",   {"dribbble.com", "behance.net", "pinterest.com", "instagram.com"}),
    ("video",    {"youtube.com", "vimeo.com"}),
    ("content",  {"youtube.com", "medium.com", "substack.com"}),
    ("research", {"youtube.com", "reddit.com", "wikipedia.org"}),
    ("data",     {"kaggle.com", "huggingface.co"}),
]


@dataclass
class WorkflowSegment:
    """A single tab session inside a workflow."""
    domain: str
    tab_title: Optional[str]
    started_at: datetime
    ended_at: datetime
    duration_sec: int
    category: str
    favicon_url: Optional[str] = None


@dataclass
class Workflow:
    """A sequence of related tab sessions clustered by time proximity."""
    started_at: datetime
    ended_at: datetime
    duration_sec: int
    label: str
    segments: List[WorkflowSegment] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "started_at":   self.started_at.isoformat(),
            "ended_at":     self.ended_at.isoformat(),
            "duration_sec": self.duration_sec,
            "label":        self.label,
            "hops":         len(self.segments),
            "ribbon":       " → ".join(_pretty(s.domain) for s in self.segments),
            "segments": [
                {
                    "domain":        s.domain,
                    "tab_title":     s.tab_title,
                    "started_at":    s.started_at.isoformat(),
                    "ended_at":      s.ended_at.isoformat(),
                    "duration_sec":  s.duration_sec,
                    "category":      s.category,
                    "favicon_url":   s.favicon_url,
                }
                for s in self.segments
            ],
        }


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────
def detect_workflows(
    db: Session,
    user_id: int,
    day_start: datetime,
    day_end: datetime,
    *,
    user_role_hint: Optional[str] = None,
) -> List[Workflow]:
    """Return all detected workflows for a user inside a date range."""
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
    role_set = _role_promotions_for(user_role_hint)

    clusters: List[List[models.BrowserActivity]] = []
    current: List[models.BrowserActivity] = []
    last_end: Optional[datetime] = None

    for r in rows:
        if last_end is None:
            current = [r]
        else:
            gap = (r.started_at - last_end).total_seconds()
            if gap <= WORKFLOW_GAP_SECONDS:
                current.append(r)
            else:
                if current:
                    clusters.append(current)
                current = [r]
        last_end = r.ended_at or r.started_at

    if current:
        clusters.append(current)

    workflows: List[Workflow] = []
    for cluster in clusters:
        domains = {c.domain for c in cluster if c.domain}
        total_dur = sum(int(c.duration_sec or 0) for c in cluster)
        if len(domains) < WORKFLOW_MIN_HOPS or total_dur < WORKFLOW_MIN_DURATION_SEC:
            continue

        # Resolve category with role-aware promotion.
        segments = [
            WorkflowSegment(
                domain        = c.domain or "unknown",
                tab_title     = c.tab_title,
                started_at    = c.started_at,
                ended_at      = c.ended_at or c.started_at,
                duration_sec  = int(c.duration_sec or 0),
                category      = _resolve_category(c.domain, overrides, role_set),
                favicon_url   = c.favicon_url,
            )
            for c in cluster
        ]
        workflows.append(Workflow(
            started_at    = cluster[0].started_at,
            ended_at      = (cluster[-1].ended_at or cluster[-1].started_at),
            duration_sec  = total_dur,
            label         = _label_for_workflow(domains, role_set),
            segments      = segments,
        ))

    return workflows


def materialise_transitions(
    db: Session,
    user_id: int,
    day_start: datetime,
    day_end: datetime,
) -> int:
    """Persist (from→to) transitions for a user/day into workflow_transitions.

    Used by a background recompute so we can show *historical* transition
    counts without re-scanning BrowserActivity every dashboard request.
    Returns the number of transitions written.
    """
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
    if len(rows) < 2:
        return 0

    # Wipe + replace for this date range so re-runs are idempotent.
    db.query(models.WorkflowTransition).filter(
        models.WorkflowTransition.user_id == user_id,
        models.WorkflowTransition.transitioned_at >= day_start,
        models.WorkflowTransition.transitioned_at < day_end,
    ).delete(synchronize_session=False)

    n = 0
    for prev, curr in zip(rows[:-1], rows[1:]):
        if not prev.domain or not curr.domain:
            continue
        if prev.domain == curr.domain:
            continue
        prev_end = prev.ended_at or prev.started_at
        gap = max(0, int((curr.started_at - prev_end).total_seconds()))
        db.add(models.WorkflowTransition(
            user_id         = user_id,
            transitioned_at = curr.started_at,
            from_kind       = "tab",
            from_key        = prev.domain,
            to_kind         = "tab",
            to_key          = curr.domain,
            gap_sec         = gap,
        ))
        n += 1
    if n:
        db.commit()
    return n


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _resolve_category(domain: Optional[str], overrides: dict, role_set: set) -> str:
    if not domain:
        return "neutral"
    d = domain.lower().lstrip(".")
    if d in role_set:
        return "work"
    return categorise(d, overrides)


def _role_promotions_for(role_hint: Optional[str]) -> set:
    if not role_hint:
        return set()
    h = role_hint.lower()
    out: set = set()
    for needle, promotions in _ROLE_PROMOTIONS:
        if needle in h:
            out |= promotions
    return out


def _label_for_workflow(domains: set, role_set: set) -> str:
    # Score each signature by how many of its anchor domains appear.
    best_label = "Mixed work"
    best_score = 0
    for label, anchors in _SIGNATURES:
        hits = len(domains & anchors)
        if hits > best_score:
            best_score = hits
            best_label = label
    if best_score == 0 and role_set:
        # Role-promoted domains might be the only thing here (e.g. recruiter
        # spending the whole cluster on linkedin) — surface that.
        if domains & role_set:
            return "Role-aligned outreach"
    return best_label


def _pretty(domain: str) -> str:
    # Shorten very long subdomains for the ribbon string.
    if not domain:
        return "?"
    parts = domain.split(".")
    if len(parts) >= 2:
        # github.com → GitHub; docs.python.org → docs.python
        if parts[-2] in {"co", "com", "io", "ai", "org", "net"}:
            return parts[-2 - 1].capitalize() if len(parts) >= 3 and parts[-3] not in {"www"} else parts[0].capitalize()
        return ".".join(parts[:-1])
    return domain
