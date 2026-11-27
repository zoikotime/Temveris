"""Trial lifecycle email templates — the 14-day ladder.

Layout, the CTA button, the paragraph helper and the billing URL are imported from
``dunning_email`` rather than copied: there is one billing email look, and a change to
it should not have to be made twice. Only the copy differs, because the situations do —
dunning tells a paying customer their card failed, these tell a prospect their
evaluation window is running out.

Sending goes through ``app.email_service._send``, so this inherits the platform's
Resend→SMTP fallback and its ``DISABLE_EMAILS`` switch. No parallel mail path exists.

Copy rules applied here:

* Say the real date, not "soon". A customer who has to guess when access changes will
  guess wrong.
* Never mention internal machinery — no dunning, no state machine, no commercial
  subscription, no notices table.
* Day 14 states plainly that nothing was charged and nothing was deleted. That is the
  question a customer actually has at that moment.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.billing.dunning_email import _BRAND, _billing_url, _p, _shell
from database import models

_log = logging.getLogger("zoiko.billing.trial_email")

_SIGNOFF = ("You're receiving this because you're the billing contact for this "
            f"{_BRAND} workspace.")


def _plan_line(plan: Optional[models.BillingPlan]) -> str:
    """What the customer would pay, per user, if they convert on the current plan."""
    if not plan:
        return f"{_BRAND}"
    price = plan.monthly_price_per_user or 0
    return f"{plan.name} at ${price:.2f} per employee / month"


def build(marker: str, *, company: str, ends: str, plan_line: str,
          billing_url: str, remaining: Optional[int] = None) -> Tuple[str, str]:
    """Return (subject, html) for one rung of the trial ladder."""
    if marker == "trial_day_0":
        return (
            f"Welcome to {_BRAND} — your 14-day free trial has started",
            _shell(
                "Your free trial has started",
                _p(f"Hello {company},")
                + _p(f"Your {_BRAND} workspace is live and your <strong>14-day free "
                     f"trial</strong> runs until <strong>{ends}</strong>.")
                + _p("Everything is unlocked for the trial. The fastest way to see "
                     "whether it fits is to invite two or three people and let a real "
                     "week of time run through it.")
                + _p("No card is needed to start, and nothing is charged automatically "
                     "when the trial ends."),
                "Open your workspace", billing_url,
                _SIGNOFF,
            ),
        )
    if marker == "trial_day_7":
        left = remaining if remaining is not None else 7
        return (
            f"You have about {left} days left in your {_BRAND} trial",
            _shell(
                f"About {left} days left in your trial",
                _p(f"Hello {company},")
                + _p(f"You're a week into your {_BRAND} trial, which ends on "
                     f"<strong>{ends}</strong>.")
                + _p("If you haven't yet, this is a good week to check the reports your "
                     "managers would actually use — that's usually what decides it.")
                + _p("Nothing is charged automatically. When you're ready to continue, "
                     "you can add a payment method from the billing page."),
                "View plans", billing_url,
                _SIGNOFF,
            ),
        )
    if marker == "trial_day_11":
        left = remaining if remaining is not None else 3
        return (
            f"Your {_BRAND} trial ends in about {left} days",
            _shell(
                f"Your trial ends in about {left} days",
                _p(f"Hello {company},")
                + _p(f"Your {_BRAND} free trial ends on <strong>{ends}</strong>.")
                + _p(f"To keep your team running without a break, add a payment method "
                     f"before then. You'd continue on {plan_line}.")
                + _p("If you'd rather not continue, you don't need to do anything — "
                     "nothing will be charged."),
                "Add payment method", billing_url,
                _SIGNOFF,
            ),
        )
    if marker == "trial_day_13":
        return (
            f"Your {_BRAND} trial ends tomorrow",
            _shell(
                "Your trial ends tomorrow",
                _p(f"Hello {company},")
                + _p(f"This is a last reminder: your {_BRAND} free trial ends on "
                     f"<strong>{ends}</strong>.")
                + _p(f"Add a payment method today to continue without interruption — "
                     f"you'd continue on {plan_line}.")
                + _p("Your data stays where it is either way. Nothing is charged unless "
                     "you choose a plan."),
                "Add payment method", billing_url,
                _SIGNOFF,
            ),
        )
    if marker == "trial_expired":
        return (
            f"Your {_BRAND} free trial has ended",
            _shell(
                "Your free trial has ended",
                _p(f"Hello {company},")
                + _p(f"Your 14-day {_BRAND} free trial ended on <strong>{ends}</strong>.")
                + _p("<strong>You have not been charged</strong>, and no subscription "
                     "has been started for you.")
                + _p("<strong>Nothing has been deleted.</strong> Your people, "
                     "timesheets, and reports are all still here.")
                + _p(f"To pick up where you left off, choose a plan from the billing "
                     f"page — {plan_line} — and your workspace continues as it was."),
                "Choose a plan", billing_url,
                "Not the right time? You can leave it. If you'd like a hand or a longer "
                "look, just reply to this email.",
            ),
        )
    raise ValueError(f"unknown trial marker: {marker}")


def send(db: Session, sub: models.BillingSubscription, *, marker: str,
         recipient: str) -> Tuple[bool, Optional[str]]:
    """Dispatch one trial email. Returns (ok, error).

    Never raises: a mail failure is recorded on the notice row so the sweep moves on and
    the trial still expires on time. ``app.email_service`` honours ``DISABLE_EMAILS``,
    so in development and in tests this logs the payload instead of delivering it.
    """
    from app.billing import trial as billing_trial              # noqa: PLC0415

    org = (db.query(models.Organization)
             .filter(models.Organization.id == sub.organization_id).first())
    plan = (db.query(models.BillingPlan)
              .filter(models.BillingPlan.id == sub.plan_id).first())
    ends = billing_trial.expires_at(sub)

    subject, html = build(
        marker,
        company=(org.name if org else "there"),
        ends=ends.strftime("%d %B %Y") if ends else "the end of your trial",
        plan_line=_plan_line(plan),
        billing_url=_billing_url(),
        remaining=billing_trial.days_remaining(sub),
    )

    try:
        from app.email_service import _send                     # noqa: PLC0415
        ok = _send(to=recipient, subject=subject, html=html,
                   classification="essential")
        return bool(ok), None if ok else "send returned False"
    except Exception as exc:
        _log.error("trial email %s failed for org=%s: %s",
                   marker, sub.organization_id, exc)
        return False, str(exc)[:300]
