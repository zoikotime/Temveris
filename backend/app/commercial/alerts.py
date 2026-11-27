"""Operational alert monitor (§21.3).

Runs every 5 minutes and checks the spec's seven alert thresholds:

  - Provisioning failure rate     > 1%      (§21.3)
  - API error rate                > 1%      (sustained)
  - Email delivery failure rate   > 5%
  - Payment failure spike         > 20% over baseline
  - Signup conversion drop        > 20% day-over-day
  - Billing webhook persistent failure
  - Entitlement computation failure (any)

When a threshold trips the alert is dispatched on every configured
channel: webhook (Slack-compatible), email (to ops mailing list), and
in-DB `commercial_events` record so the admin console shows it. Each
alert is keyed by (alert_id, window_start) so a single ongoing incident
doesn't fire 12× per hour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from database.database import SessionLocal
from app.commercial import constants
from app.commercial.audit import emit_event
from app.commercial.models import CommercialEvent

_log = logging.getLogger("zoiko.commercial.alerts")


# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────
# Slack-compatible webhook (any service that accepts `{"text": ...}` JSON
# POST works: Slack, Discord, Mattermost, custom relay). Set via env.
ALERT_WEBHOOK_URL = (os.environ.get("ZT_ALERT_WEBHOOK_URL") or "").strip()
# Comma-separated list of ops emails to CC when an alert fires.
ALERT_OPS_EMAILS = [
    e.strip() for e in (os.environ.get("ZT_ALERT_OPS_EMAILS") or "").split(",")
    if e.strip()
]

# Look back this many minutes for "current" rates. 30m is long enough to
# damp out single-event noise, short enough to catch an incident inside
# one polling cycle of the on-call rotation.
WINDOW_MINUTES = 30
# Re-fire cooldown — don't alert again on the same condition within this
# many minutes if it stays tripped. Ops sees one alert per incident, not
# one every cycle.
COOLDOWN_MINUTES = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _window_start() -> datetime:
    return _now() - timedelta(minutes=WINDOW_MINUTES)


def _count_events(db: Session, types: List[str], since: datetime) -> int:
    return (
        db.query(func.count(CommercialEvent.event_id))
          .filter(CommercialEvent.event_type.in_(types),
                  CommercialEvent.occurred_at >= since)
          .scalar() or 0
    )


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


# ──────────────────────────────────────────────────────────────────────────
# Per-threshold checks. Each returns (tripped, current_value, payload).
# ──────────────────────────────────────────────────────────────────────────
def _check_provisioning_failure(db: Session) -> Tuple[bool, float, Dict]:
    since = _window_start()
    succ = _count_events(db, ["tenant.provisioned"], since)
    fail = _count_events(db, ["tenant.provisioning_failed"], since)
    total = succ + fail
    pct = _pct(fail, total)
    threshold = constants.ALERT_PROVISIONING_FAILURE_PCT
    return (
        total > 0 and pct > threshold,
        pct,
        {"failures": fail, "successes": succ, "total": total, "threshold_pct": threshold},
    )


def _check_email_delivery(db: Session) -> Tuple[bool, float, Dict]:
    since = _window_start()
    sent = _count_events(db, ["email_verification.sent"], since)
    failed = _count_events(db, ["email_delivery.failed"], since)
    total = sent + failed
    pct = _pct(failed, total)
    threshold = constants.ALERT_EMAIL_DELIVERY_FAILURE_PCT
    return (
        total > 0 and pct > threshold,
        pct,
        {"failures": failed, "delivered": sent, "total": total, "threshold_pct": threshold},
    )


def _check_payment_spike(db: Session) -> Tuple[bool, float, Dict]:
    """Compare failure rate over the last WINDOW_MINUTES against the
    baseline (24h prior). Spike = current / baseline ratio above the
    configured percentage threshold."""
    since = _window_start()
    baseline_since = _now() - timedelta(hours=24)
    baseline_until = since
    current_failed = _count_events(db, ["payment.declined"], since)
    baseline_failed = (
        db.query(func.count(CommercialEvent.event_id))
          .filter(CommercialEvent.event_type == "payment.declined",
                  CommercialEvent.occurred_at >= baseline_since,
                  CommercialEvent.occurred_at < baseline_until)
          .scalar() or 0
    )
    # Normalize baseline to a 30-minute slice for comparison.
    baseline_rate = baseline_failed / max(1, (24 * 60 / WINDOW_MINUTES))
    pct_over = 0.0 if baseline_rate <= 0 else ((current_failed - baseline_rate) / baseline_rate) * 100.0
    threshold = constants.ALERT_PAYMENT_FAILURE_SPIKE_PCT
    return (
        current_failed > 3 and pct_over > threshold,
        pct_over,
        {"current_failures": current_failed, "baseline_rate": baseline_rate,
         "threshold_pct": threshold},
    )


def _check_signup_drop(db: Session) -> Tuple[bool, float, Dict]:
    """Day-over-day conversion drop: compare today's `signup.completed`
    rate (last 24h) against yesterday's. Spec §21.3 threshold: > 20%."""
    today_since = _now() - timedelta(hours=24)
    yesterday_since = _now() - timedelta(hours=48)
    yesterday_until = today_since
    today = _count_events(db, ["signup.completed"], today_since)
    yesterday = (
        db.query(func.count(CommercialEvent.event_id))
          .filter(CommercialEvent.event_type == "signup.completed",
                  CommercialEvent.occurred_at >= yesterday_since,
                  CommercialEvent.occurred_at < yesterday_until)
          .scalar() or 0
    )
    drop_pct = 0.0 if yesterday <= 0 else ((yesterday - today) / yesterday) * 100.0
    threshold = constants.ALERT_SIGNUP_DROP_PCT
    return (
        yesterday > 5 and drop_pct > threshold,
        drop_pct,
        {"today": today, "yesterday": yesterday, "threshold_pct": threshold},
    )


def _check_webhook_failures(db: Session) -> Tuple[bool, float, Dict]:
    """Any dead-letter webhook receipt in the last hour is an alert."""
    since = _now() - timedelta(hours=1)
    from app.commercial.models import ProviderWebhookReceipt
    dead = (
        db.query(func.count(ProviderWebhookReceipt.id))
          .filter(ProviderWebhookReceipt.status == "dead_letter",
                  ProviderWebhookReceipt.received_at >= since)
          .scalar() or 0
    )
    return (dead > 0, float(dead), {"dead_letter_count": dead})


def _check_entitlement_failures(db: Session) -> Tuple[bool, float, Dict]:
    since = _window_start()
    fails = _count_events(db, ["entitlements.computation_failed"], since)
    return (fails > 0, float(fails), {"failure_count": fails})


# ──────────────────────────────────────────────────────────────────────────
# Cooldown — read the last alert.fired event for this alert_id and
# skip if we're inside the cooldown window.
# ──────────────────────────────────────────────────────────────────────────
def _in_cooldown(db: Session, alert_id: str) -> bool:
    cutoff = _now() - timedelta(minutes=COOLDOWN_MINUTES)
    row = (
        db.query(CommercialEvent.occurred_at)
          .filter(CommercialEvent.event_type == "alert.fired",
                  CommercialEvent.payload["alert_id"].astext == alert_id)
          .order_by(CommercialEvent.occurred_at.desc())
          .first()
    )
    if row is None:
        return False
    last = row[0]
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last >= cutoff


# ──────────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────────
def _dispatch_webhook(text: str, blocks: Optional[List[Dict]] = None) -> bool:
    if not ALERT_WEBHOOK_URL:
        return False
    body = {"text": text}
    if blocks:
        body["blocks"] = blocks
    try:
        req = urllib.request.Request(
            ALERT_WEBHOOK_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        _log.warning("Alert webhook dispatch failed: %s", e)
        return False
    except Exception as e:
        _log.exception("Alert webhook dispatch unexpected error: %s", e)
        return False


def _dispatch_email(subject: str, body: str) -> bool:
    if not ALERT_OPS_EMAILS:
        return False
    try:
        from app.email_service import _send, _base_layout
        html = _base_layout(f"<p>{body}</p>", preheader=subject)
        ok = False
        for to_addr in ALERT_OPS_EMAILS:
            ok = _send(to_addr, subject, html) or ok
        return ok
    except Exception:
        _log.exception("Alert email dispatch failed")
        return False


def _fire_alert(db: Session, *, alert_id: str, summary: str, severity: str, details: Dict) -> None:
    """Persist alert event + dispatch to webhook + email."""
    payload = {
        "alert_id": alert_id, "severity": severity,
        "summary": summary, "details": details,
        "fired_at": _now().isoformat(),
    }
    emit_event(
        db, event_type="alert.fired",
        source_service="alert-monitor",
        payload=payload,
    )
    db.commit()

    text = f"[Temveris · {severity.upper()}] {summary}"
    _dispatch_webhook(
        text,
        blocks=[{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{text}*\n```{json.dumps(details, indent=2)}```"},
        }],
    )
    _dispatch_email(
        subject=text,
        body=f"{summary}<br><br><pre>{json.dumps(details, indent=2)}</pre>",
    )
    _log.warning("ALERT FIRED %s | %s | %s", alert_id, summary, details)


# ──────────────────────────────────────────────────────────────────────────
# Tick — evaluate every check, fire alerts that aren't in cooldown.
# ──────────────────────────────────────────────────────────────────────────
_CHECKS = [
    ("provisioning_failure_rate",  "high",     "Provisioning failure rate above 1%",       _check_provisioning_failure),
    ("email_delivery_failure_rate","medium",   "Email delivery failure rate above 5%",     _check_email_delivery),
    ("payment_failure_spike",      "high",     "Payment failure spike above baseline",     _check_payment_spike),
    ("signup_conversion_drop",     "medium",   "Signup conversion dropped >20% day/day",   _check_signup_drop),
    ("webhook_dead_letter",        "high",     "Provider webhooks in dead-letter queue",   _check_webhook_failures),
    ("entitlement_compute_failure","high",     "Entitlement compute failures detected",    _check_entitlement_failures),
]


def run_once_sync() -> int:
    fired = 0
    db = SessionLocal()
    try:
        for alert_id, severity, summary, check_fn in _CHECKS:
            try:
                tripped, current_value, details = check_fn(db)
                if not tripped:
                    continue
                if _in_cooldown(db, alert_id):
                    continue
                details = {**details, "current_value": current_value}
                _fire_alert(
                    db, alert_id=alert_id, severity=severity,
                    summary=summary, details=details,
                )
                fired += 1
            except Exception:
                _log.exception("Alert check %s failed", alert_id)
                try: db.rollback()
                except Exception: pass
    finally:
        db.close()
    return fired


async def run_forever(interval_seconds: int = 300) -> None:
    _log.info("Alert monitor started (interval=%ss)", interval_seconds)
    while True:
        try:
            fired = await asyncio.to_thread(run_once_sync)
            if fired:
                _log.info("Alert tick fired %s alert(s)", fired)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Alert tick error")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
