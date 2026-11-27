"""
Temveris – Productivity categorisation
=======================================
Maps a browser domain → one of {work, neutral, distraction}.

Resolution order (first hit wins):
    1. Per-org override in `domain_categories` table (admin configurable).
    2. Hard-coded defaults below — curated for "most teams most of the time".
    3. Fallback: "neutral".

Localhost and dev-environment hosts (127.0.0.1, *.local) are treated as work
since a developer on localhost:3000 is almost certainly building, not scrolling.
"""
from __future__ import annotations

from typing import Dict, Optional
from sqlalchemy.orm import Session

from database import models

CATEGORY_WORK        = "work"
CATEGORY_NEUTRAL     = "neutral"
CATEGORY_DISTRACTION = "distraction"
VALID_CATEGORIES     = {CATEGORY_WORK, CATEGORY_NEUTRAL, CATEGORY_DISTRACTION}

# Curated defaults. Keep this conservative — when in doubt, mark as neutral
# so the admin can explicitly promote/demote via the override table.
DEFAULT_CATEGORIES: Dict[str, str] = {
    # — Work: developer & collaboration tools —
    "github.com":           CATEGORY_WORK,
    "gitlab.com":           CATEGORY_WORK,
    "bitbucket.org":        CATEGORY_WORK,
    "stackoverflow.com":    CATEGORY_WORK,
    "stackexchange.com":    CATEGORY_WORK,
    "jira.atlassian.com":   CATEGORY_WORK,
    "atlassian.com":        CATEGORY_WORK,
    "confluence.com":       CATEGORY_WORK,
    "notion.so":            CATEGORY_WORK,
    "linear.app":           CATEGORY_WORK,
    "asana.com":            CATEGORY_WORK,
    "trello.com":           CATEGORY_WORK,
    "slack.com":            CATEGORY_WORK,
    "figma.com":            CATEGORY_WORK,
    "miro.com":             CATEGORY_WORK,
    "docs.google.com":      CATEGORY_WORK,
    "drive.google.com":     CATEGORY_WORK,
    "sheets.google.com":    CATEGORY_WORK,
    "mail.google.com":      CATEGORY_WORK,
    "calendar.google.com":  CATEGORY_WORK,
    "meet.google.com":      CATEGORY_WORK,
    "outlook.office.com":   CATEGORY_WORK,
    "outlook.live.com":     CATEGORY_WORK,
    "teams.microsoft.com":  CATEGORY_WORK,
    "office.com":           CATEGORY_WORK,
    "onedrive.live.com":    CATEGORY_WORK,
    "sharepoint.com":       CATEGORY_WORK,
    "aws.amazon.com":       CATEGORY_WORK,
    "console.aws.amazon.com": CATEGORY_WORK,
    "cloud.google.com":     CATEGORY_WORK,
    "azure.microsoft.com":  CATEGORY_WORK,
    "vercel.com":           CATEGORY_WORK,
    "netlify.com":          CATEGORY_WORK,
    "heroku.com":           CATEGORY_WORK,
    "npmjs.com":            CATEGORY_WORK,
    "pypi.org":             CATEGORY_WORK,
    "docker.com":           CATEGORY_WORK,
    "hub.docker.com":       CATEGORY_WORK,
    "developer.mozilla.org": CATEGORY_WORK,
    "chatgpt.com":          CATEGORY_WORK,
    "claude.ai":            CATEGORY_WORK,
    "gemini.google.com":    CATEGORY_WORK,

    # — Neutral: search / reference / general-web —
    "google.com":           CATEGORY_NEUTRAL,
    "bing.com":             CATEGORY_NEUTRAL,
    "duckduckgo.com":       CATEGORY_NEUTRAL,
    "wikipedia.org":        CATEGORY_NEUTRAL,
    "medium.com":           CATEGORY_NEUTRAL,
    "dev.to":               CATEGORY_NEUTRAL,
    "hashnode.com":         CATEGORY_NEUTRAL,
    "news.ycombinator.com": CATEGORY_NEUTRAL,

    # — Distraction: social, entertainment, shopping —
    "facebook.com":         CATEGORY_DISTRACTION,
    "instagram.com":        CATEGORY_DISTRACTION,
    "twitter.com":          CATEGORY_DISTRACTION,
    "x.com":                CATEGORY_DISTRACTION,
    "tiktok.com":           CATEGORY_DISTRACTION,
    "reddit.com":           CATEGORY_DISTRACTION,
    "youtube.com":          CATEGORY_DISTRACTION,
    "netflix.com":          CATEGORY_DISTRACTION,
    "twitch.tv":            CATEGORY_DISTRACTION,
    "spotify.com":          CATEGORY_DISTRACTION,
    "pinterest.com":        CATEGORY_DISTRACTION,
    "snapchat.com":         CATEGORY_DISTRACTION,
    "amazon.com":           CATEGORY_DISTRACTION,
    "amazon.in":            CATEGORY_DISTRACTION,
    "flipkart.com":         CATEGORY_DISTRACTION,
    "ebay.com":             CATEGORY_DISTRACTION,
    "9gag.com":             CATEGORY_DISTRACTION,
    "buzzfeed.com":         CATEGORY_DISTRACTION,
}


def _is_localhost(domain: str) -> bool:
    return (
        domain in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
        or domain.endswith(".local")
        or domain.endswith(".localhost")
    )


def categorise(domain: Optional[str], overrides: Optional[Dict[str, str]] = None) -> str:
    """Return the category for a single domain."""
    if not domain:
        return CATEGORY_NEUTRAL
    d = domain.lower().lstrip(".")
    if overrides and d in overrides:
        return overrides[d]
    if _is_localhost(d):
        return CATEGORY_WORK
    if d in DEFAULT_CATEGORIES:
        return DEFAULT_CATEGORIES[d]
    # Match on the registrable suffix — "sub.github.com" → github.com.
    parts = d.split(".")
    for i in range(1, len(parts) - 1):
        candidate = ".".join(parts[i:])
        if overrides and candidate in overrides:
            return overrides[candidate]
        if candidate in DEFAULT_CATEGORIES:
            return DEFAULT_CATEGORIES[candidate]
    return CATEGORY_NEUTRAL


def load_overrides(db: Session, organization_id: Optional[int] = None) -> Dict[str, str]:
    """Load admin overrides from the database, scoped to the given org.

    When organization_id is None (legacy / unauthenticated callers) all rows
    are returned for backward compatibility.  Absent table = empty dict."""
    try:
        q = db.query(models.DomainCategory)
        if organization_id is not None:
            q = q.filter(models.DomainCategory.organization_id == organization_id)
        rows = q.all()
    except Exception:
        # A failed query leaves this connection's transaction ABORTED. Callers
        # reuse the SAME session for later queries (e.g. the daily-summary path
        # load_overrides -> detect_focus_sessions), so without a rollback the
        # next query dies with InFailedSqlTransaction and a benign "no overrides"
        # fallback becomes a 500 for the whole request. Log the real error
        # (previously swallowed silently) and roll back to restore the session,
        # while preserving the existing empty-overrides fallback.
        import logging
        logging.getLogger("zoiko.browser").exception(
            "load_overrides: DomainCategory query failed; rolling back so the "
            "shared session stays usable"
        )
        try:
            db.rollback()
        except Exception:
            logging.getLogger("zoiko.browser").exception(
                "load_overrides: rollback after query failure also failed"
            )
        return {}
    return {r.domain.lower(): r.category for r in rows if r.category in VALID_CATEGORIES}
