"""Billing-owned dunning email templates.

Written here rather than reusing ``app/commercial/notifications.py``: that module is
typed to the commercial models and this phase must not import from or modify
``app/commercial``. The four day markers match the approved ladder (1 / 3 / 7 / 14).

Sending goes through the application's existing ``app.email_service._send``, so this
inherits the platform's Resend→SMTP fallback and, importantly, its ``DISABLE_EMAILS``
switch — when that is set the payload is logged instead of delivered. No parallel mail
path is introduced.

Tone: these tell a paying customer their payment failed and what happens next. They
state the actual consequence and the actual date, because the point of the ladder is
that nobody is surprised by losing write access on day 14.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from database import models

_log = logging.getLogger("zoiko.billing.dunning_email")

_BRAND = "Temveris"


def _shell(heading: str, body_html: str, cta_label: str, cta_url: str,
           footer_note: str) -> str:
    """One plain, table-based layout. Deliberately simple: billing mail must render in
    every client, including the ones that strip CSS."""
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            max-width:560px;margin:0 auto;padding:32px 24px;color:#0F172A;">
  <p style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;
            color:#64748B;margin:0 0 18px;font-weight:700;">{_BRAND} Billing</p>
  <h1 style="font-size:21px;line-height:1.35;margin:0 0 16px;">{heading}</h1>
  {body_html}
  <p style="margin:28px 0 8px;">
    <a href="{cta_url}"
       style="background:#4F46E5;color:#fff;text-decoration:none;padding:12px 22px;
              border-radius:8px;font-weight:700;display:inline-block;">{cta_label}</a>
  </p>
  <p style="font-size:12.5px;color:#64748B;line-height:1.6;margin-top:24px;
            border-top:1px solid #E2E8F0;padding-top:16px;">{footer_note}</p>
</div>"""


def _p(text: str) -> str:
    return f'<p style="font-size:14.5px;line-height:1.65;margin:0 0 12px;">{text}</p>'


def _billing_url() -> str:
    import os
    base = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    return f"{base}/settings/billing-subscription"


def _amount(sub: models.BillingSubscription, plan: Optional[models.BillingPlan]) -> str:
    if not plan:
        return "your subscription"
    seats = max(sub.active_users or 1, 1)
    per = (plan.annual_price_per_user if sub.billing_cycle == "annual"
           else plan.monthly_price_per_user) or 0
    cycle = "year" if sub.billing_cycle == "annual" else "month"
    cur = sub.currency or "USD"
    return f"{plan.name} — {seats} seat{'s' if seats != 1 else ''} at {cur} {per:.2f}/seat/{cycle}"


def build(marker: str, *, company: str, plan_line: str, deadline: str,
          billing_url: str) -> Tuple[str, str]:
    """Return (subject, html) for one ladder step."""
    if marker == "day_1":
        return (
            f"Action needed: your {_BRAND} payment didn't go through",
            _shell(
                "We couldn't process your payment",
                _p(f"Hello {company},")
                + _p(f"Your latest payment for {plan_line} was declined by your bank or card issuer.")
                + _p("This usually means an expired card, insufficient funds, or a block "
                     "from the issuer. Updating your card resolves it immediately.")
                + _p(f"Your service is unaffected for now. If payment is still outstanding "
                     f"on <strong>{deadline}</strong>, your workspace will switch to "
                     f"read-only and admins won't be able to make changes."),
                "Update payment method", billing_url,
                "You're receiving this because you're the billing contact for this "
                f"{_BRAND} workspace.",
            ),
        )
    if marker == "day_3":
        return (
            f"Reminder: {_BRAND} payment still outstanding",
            _shell(
                "Your payment is still outstanding",
                _p(f"Hello {company},")
                + _p(f"We've retried the payment for {plan_line} and it hasn't succeeded.")
                + _p("Your team still has full access. To keep it that way, please update "
                     "your payment method.")
                + _p(f"If the balance is unpaid on <strong>{deadline}</strong>, the "
                     f"workspace becomes read-only."),
                "Update payment method", billing_url,
                "Already paid? It can take a few hours to reconcile — you can ignore this.",
            ),
        )
    if marker == "day_7":
        return (
            f"One week overdue — {_BRAND} access changes soon",
            _shell(
                "Your account is one week overdue",
                _p(f"Hello {company},")
                + _p(f"The payment for {plan_line} has now been outstanding for a week.")
                + _p(f"On <strong>{deadline}</strong> this workspace will become "
                     f"<strong>read-only</strong>: your team can still view data and "
                     f"clock in and out, but admins won't be able to change settings, "
                     f"add people, or run administrative actions."),
                "Update payment method", billing_url,
                "Nothing is deleted, and your data stays intact. Full access returns the "
                "moment payment succeeds.",
            ),
        )
    if marker == "day_14":
        return (
            f"Your {_BRAND} workspace is now read-only",
            _shell(
                "Your workspace is now read-only",
                _p(f"Hello {company},")
                + _p(f"The payment for {plan_line} has been outstanding for 14 days, so "
                     f"this workspace has moved to read-only.")
                + _p("<strong>What still works:</strong> everyone can sign in and view "
                     "their data, employees can clock in and out, and time tracking "
                     "continues normally.")
                + _p("<strong>What's paused:</strong> administrative changes — adding or "
                     "removing people, editing settings, and other admin actions.")
                + _p("Your subscription has <strong>not</strong> been cancelled and "
                     "nothing has been deleted. Updating your payment method restores "
                     "full access immediately."),
                "Restore full access", billing_url,
                "Need help or think this is a mistake? Reply to this email and we'll sort "
                "it out.",
            ),
        )
    raise ValueError(f"unknown dunning marker: {marker}")


def send(db: Session, sub: models.BillingSubscription, *, marker: str,
         recipient: str) -> Tuple[bool, Optional[str]]:
    """Dispatch one ladder email. Returns (ok, error).

    Never raises: a mail failure is recorded on the notice row so the sweep can move
    on. ``app.email_service`` honours ``DISABLE_EMAILS``, so in a development or test
    environment this logs the payload instead of delivering it.
    """
    from app.billing import dunning as billing_dunning       # noqa: PLC0415

    org = (db.query(models.Organization)
             .filter(models.Organization.id == sub.organization_id).first())
    plan = (db.query(models.BillingPlan)
              .filter(models.BillingPlan.id == sub.plan_id).first())
    expires = billing_dunning.grace_expires_at(sub)

    subject, html = build(
        marker,
        company=(org.name if org else "there"),
        plan_line=_amount(sub, plan),
        deadline=expires.strftime("%d %B %Y") if expires else "the due date",
        billing_url=_billing_url(),
    )

    try:
        from app.email_service import _send                  # noqa: PLC0415
        ok = _send(to=recipient, subject=subject, html=html,
                   classification="essential")
        return bool(ok), None if ok else "send returned False"
    except Exception as exc:
        _log.error("dunning email %s failed for org=%s: %s",
                   marker, sub.organization_id, exc)
        return False, str(exc)[:300]
