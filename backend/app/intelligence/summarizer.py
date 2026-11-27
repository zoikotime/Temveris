"""
AI-powered daily summary generator.

Strategy:
  1. Pull a structured "day shape" from the DB (hours worked, productive
     mix, deep focus blocks, top domains, burnout signals, workflows).
  2. If ANTHROPIC_API_KEY is set, call Claude with a tight role-aware
     prompt to get a 2–3 sentence cinematic narrative + a 6-word headline.
  3. Otherwise (or on any LLM error), fall back to a rule-based template
     that produces similar output without any external call.
  4. Cache the result in `daily_intelligence_summaries` keyed by
     (user_id, summary_date) so the cinematic admin view loads instantly
     on repeat opens and we don't burn tokens on every render.

The cached row is invalidated by the recompute endpoint or when the
caller passes force=True.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List

from sqlalchemy import and_
from sqlalchemy.orm import Session

from database import models
from app.browser.productivity import categorise, load_overrides
from app.intelligence.workflow import detect_workflows
from app.intelligence.focus import detect_focus_sessions, DEEP_FOCUS_MINUTES
from app.intelligence.burnout import compute_burnout

CLAUDE_MODEL = os.getenv("ZOIKO_AI_MODEL", "claude-sonnet-4-6")
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class DayShape:
    user_id: int
    user_name: str
    role_hint: str
    summary_date: str
    hours_worked: float
    break_minutes: int
    productive_pct: float
    deep_work_min: int
    focus_block_count: int
    deepest_block_min: int
    context_switches: int
    top_domains: List[dict]
    workflow_labels: List[str]
    primary_focus: Optional[str]
    burnout_risk: str
    burnout_reasons: List[str]


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────
def generate_daily_summary(
    db: Session,
    user_id: int,
    target_date: date,
    *,
    force: bool = False,
) -> dict:
    """Return (and cache) the daily summary for a user/date."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"error": "User not found"}

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    # Cache lookup
    if not force:
        cached = (
            db.query(models.DailyIntelligenceSummary)
            .filter(
                models.DailyIntelligenceSummary.user_id == user_id,
                models.DailyIntelligenceSummary.summary_date == day_start,
            )
            .first()
        )
        if cached:
            return _row_to_dict(cached, cached_hit=True)

    shape = _compute_day_shape(db, user, target_date, day_start, day_end)

    # No work? Save a "rest day" stub so we don't keep recomputing it.
    if shape.hours_worked <= 0.1:
        out = {
            "headline":          "No work activity recorded",
            "narrative":         f"No clock-in detected for {user.name} on {target_date.isoformat()}.",
            "productivity_pct":  0.0,
            "deep_work_min":     0,
            "context_switches":  0,
            "burnout_risk":      "low",
            "primary_focus":     None,
            "generator":         "rules",
        }
        _persist(db, user_id, day_start, out, payload=asdict(shape))
        return out

    # Try LLM first; fall back to rules on any error.
    out = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            out = _llm_summarise(shape)
        except Exception as e:
            print(f"[summarizer] LLM failed, falling back to rules: {e!r}", flush=True)
            out = None

    if not out:
        out = _rules_summarise(shape)

    out["productivity_pct"]  = shape.productive_pct
    out["deep_work_min"]     = shape.deep_work_min
    out["context_switches"]  = shape.context_switches
    out["burnout_risk"]      = shape.burnout_risk
    out["primary_focus"]     = shape.primary_focus

    _persist(db, user_id, day_start, out, payload=asdict(shape))
    return out


# ──────────────────────────────────────────────────────────────────────
# Day shape extraction (no LLM)
# ──────────────────────────────────────────────────────────────────────
def _compute_day_shape(
    db: Session,
    user: models.User,
    target_date: date,
    day_start: datetime,
    day_end: datetime,
) -> DayShape:
    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == user.id,
            models.ClockSession.clock_in_time >= day_start,
            models.ClockSession.clock_in_time < day_end,
        )
        .all()
    )
    breaks = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.user_id == user.id,
            models.BreakSession.break_start >= day_start,
            models.BreakSession.break_start < day_end,
        )
        .all()
    )

    total_work_sec = 0
    total_break_sec = 0
    for s in sessions:
        end = s.clock_out_time or datetime.now(timezone.utc)
        elapsed = max(0, int((end - s.clock_in_time).total_seconds()))
        brk = int(s.break_seconds or 0)
        total_break_sec += brk
        total_work_sec += max(0, elapsed - brk)

    # productive % = active browser/app time as share of clocked time
    browser_rows = (
        db.query(models.BrowserActivity)
        .filter(
            models.BrowserActivity.user_id == user.id,
            models.BrowserActivity.started_at >= day_start,
            models.BrowserActivity.started_at < day_end,
        )
        .all()
    )
    active_sec = sum(int(b.active_sec or 0) for b in browser_rows)
    productive_pct = (active_sec / total_work_sec * 100) if total_work_sec else 0.0

    # Scoped to THIS user's organization. `load_overrides(db)` with no org
    # loads EVERY tenant's DomainCategory rows, so one workspace classifying
    # youtube.com as "work" changed how productive another workspace's people
    # looked. The org-scoped call is what the browser router already uses.
    overrides = load_overrides(db, getattr(user, "organization_id", None))
    top_by_domain: dict = {}
    for b in browser_rows:
        if not b.domain:
            continue
        top_by_domain[b.domain] = top_by_domain.get(b.domain, 0) + int(b.duration_sec or 0)
    top_domains = [
        {"domain": d, "duration_sec": secs, "category": categorise(d, overrides)}
        for d, secs in sorted(top_by_domain.items(), key=lambda kv: kv[1], reverse=True)[:5]
    ]
    primary_focus = top_domains[0]["domain"] if top_domains else None

    focus_blocks = detect_focus_sessions(db, user.id, day_start, day_end)
    deep_min = sum(int(f.duration_sec or 0) for f in focus_blocks if f.is_deep) // 60
    deepest = max((int(f.duration_sec or 0) for f in focus_blocks), default=0) // 60

    role_hint = " ".join(filter(None, [user.designation, user.department, user.team])).strip()
    workflows = detect_workflows(db, user.id, day_start, day_end, user_role_hint=role_hint)
    labels = list({w.label for w in workflows})

    burnout = compute_burnout(db, user.id, period_days=7)
    burnout_reasons = [s.reason for s in burnout.signals]

    context_switches = max(0, len(browser_rows) - 1)

    return DayShape(
        user_id            = user.id,
        user_name          = user.name,
        role_hint          = role_hint or "team member",
        summary_date       = target_date.isoformat(),
        hours_worked       = round(total_work_sec / 3600, 2),
        break_minutes      = total_break_sec // 60,
        productive_pct     = round(productive_pct, 1),
        deep_work_min      = deep_min,
        focus_block_count  = len(focus_blocks),
        deepest_block_min  = deepest,
        context_switches   = context_switches,
        top_domains        = top_domains,
        workflow_labels    = labels,
        primary_focus      = primary_focus,
        burnout_risk       = burnout.risk_band,
        burnout_reasons    = burnout_reasons,
    )


# ──────────────────────────────────────────────────────────────────────
# LLM path (Claude)
# ──────────────────────────────────────────────────────────────────────
def _llm_summarise(shape: DayShape) -> Optional[dict]:
    """Call Claude with a structured day-shape, return narrative + headline."""
    import urllib.request
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    system_prompt = (
        "You are a workforce intelligence narrator. Your job is to write a "
        "premium-feeling 2–3 sentence daily summary of an employee's day "
        "based ONLY on the structured day-shape you are given. Do NOT invent "
        "facts. The tone is warm, specific, and human — not surveillance-y. "
        "You also produce a 4–7 word headline that captures the day's vibe. "
        "Use the role hint to interpret signals (e.g. for a recruiter, "
        "LinkedIn time is productive). Never moralise. Never use the word "
        "'productive' more than once. Output STRICT JSON only — no markdown, "
        "no code fence, no commentary."
    )

    user_payload = {
        "name":              shape.user_name,
        "role":              shape.role_hint,
        "date":              shape.summary_date,
        "hours_worked":      shape.hours_worked,
        "break_minutes":     shape.break_minutes,
        "productive_pct":    shape.productive_pct,
        "deep_work_min":     shape.deep_work_min,
        "deepest_block_min": shape.deepest_block_min,
        "focus_blocks":      shape.focus_block_count,
        "context_switches":  shape.context_switches,
        "primary_focus":     shape.primary_focus,
        "workflow_labels":   shape.workflow_labels,
        "top_domains":       [d["domain"] for d in shape.top_domains],
        "burnout_risk":      shape.burnout_risk,
        "burnout_reasons":   shape.burnout_reasons,
    }

    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": 400,
        "system":     system_prompt,
        "messages": [{
            "role": "user",
            "content": (
                "Day shape JSON:\n"
                + json.dumps(user_payload, indent=2)
                + '\n\nReturn JSON of shape: {"headline": "...", "narrative": "..."}'
            ),
        }],
    }
    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key":         api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type":      "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # Anthropic responses include a `content` list with text blocks.
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    text = text.strip()
    # The model returns strict JSON per our instructions. Extract it
    # defensively in case it sneaks in fence markers.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict) or "narrative" not in parsed:
        return None
    return {
        "headline":  (parsed.get("headline") or "")[:255],
        "narrative": parsed.get("narrative") or "",
        "generator": "claude",
    }


# ──────────────────────────────────────────────────────────────────────
# Rule-based fallback
# ──────────────────────────────────────────────────────────────────────
def _rules_summarise(shape: DayShape) -> dict:
    parts = []

    if shape.hours_worked >= 8:
        parts.append(f"{shape.user_name} put in a full {shape.hours_worked:.1f}-hour day")
    elif shape.hours_worked >= 5:
        parts.append(f"{shape.user_name} logged {shape.hours_worked:.1f} hours")
    else:
        parts.append(f"{shape.user_name} had a short {shape.hours_worked:.1f}-hour session")

    if shape.deep_work_min >= 90:
        parts.append(f"with {shape.deep_work_min // 60}h {shape.deep_work_min % 60}m of deep-focus work")
    elif shape.deep_work_min >= 30:
        parts.append(f"with {shape.deep_work_min} minutes of sustained focus")
    elif shape.focus_block_count > 0:
        parts.append(f"across {shape.focus_block_count} focused stretch(es)")

    if shape.workflow_labels:
        primary_label = shape.workflow_labels[0]
        parts.append(f"— mostly {primary_label.lower()}")

    if shape.primary_focus:
        parts.append(f"on {shape.primary_focus}")

    narrative = " ".join(parts).rstrip(",") + "."

    # Trailing context-switch / burnout addendum
    if shape.context_switches >= 200:
        narrative += f" Heavy tab-switching today ({shape.context_switches} switches) — likely cross-system work."
    if shape.burnout_risk == "high":
        narrative += " Burnout risk is elevated — consider lighter scheduling tomorrow."
    elif shape.burnout_risk == "moderate":
        narrative += " Watch the pace — a couple of stress signals trending up."

    # Build headline
    if shape.deep_work_min >= 120:
        headline = "Deep focus dominated the day"
    elif shape.workflow_labels:
        headline = f"{shape.workflow_labels[0]} kind of day"
    elif shape.context_switches >= 200:
        headline = "Rapid context-switching mode"
    elif shape.hours_worked >= 8:
        headline = "Full-shift steady work"
    else:
        headline = "Light session, scattered focus"

    return {
        "headline":  headline,
        "narrative": narrative,
        "generator": "rules",
    }


# ──────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────
def _persist(db: Session, user_id: int, summary_date: datetime, out: dict, *, payload: dict) -> None:
    row = (
        db.query(models.DailyIntelligenceSummary)
        .filter(
            models.DailyIntelligenceSummary.user_id == user_id,
            models.DailyIntelligenceSummary.summary_date == summary_date,
        )
        .first()
    )
    if not row:
        row = models.DailyIntelligenceSummary(
            user_id=user_id,
            summary_date=summary_date,
        )
        db.add(row)
    row.narrative         = out.get("narrative", "")
    row.headline          = out.get("headline", "")
    row.productivity_pct  = out.get("productivity_pct", 0.0)
    row.deep_work_min     = int(out.get("deep_work_min", 0) or 0)
    row.context_switches  = int(out.get("context_switches", 0) or 0)
    row.burnout_risk      = out.get("burnout_risk", "low")
    row.primary_focus     = out.get("primary_focus")
    row.generator         = out.get("generator", "rules")
    row.payload           = payload
    db.commit()


def _row_to_dict(row: models.DailyIntelligenceSummary, *, cached_hit: bool = False) -> dict:
    return {
        "headline":         row.headline,
        "narrative":        row.narrative,
        "productivity_pct": row.productivity_pct,
        "deep_work_min":    row.deep_work_min,
        "context_switches": row.context_switches,
        "burnout_risk":     row.burnout_risk,
        "primary_focus":    row.primary_focus,
        "generator":        row.generator,
        "cached":           cached_hit,
        "updated_at":       row.updated_at.isoformat() if row.updated_at else None,
    }
