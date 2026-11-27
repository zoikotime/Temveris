"""Commercial notification service (§5 — Notification Service).

Sends lifecycle emails for the four customer journeys:
  - Welcome email (signup completion)
  - Email verification
  - Trial sequence (Day 0/1/7/14/18/25/30/32/35)
  - Conversion / billing / failure notifications

All sends go through the existing app.email_service helper so they share
the Resend → SMTP fallback chain, branded layout, and logging.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.email_service import _send, _base_layout, _btn, FRONTEND_URL
from app.commercial.audit import emit_event
from app.commercial.models import CommercialOrganization
from app.notifications import classification as _notif_classification
from database.models import User

_log = logging.getLogger("zoiko.commercial.notifications")


def _verify_url(raw_token: str) -> str:
    base = FRONTEND_URL.rstrip("/")
    return f"{base}/verify-email?token={raw_token}"


def _app_url() -> str:
    return FRONTEND_URL.rstrip("/")


def _code_block(raw_code: Optional[str]) -> str:
    """The 6-digit code as one cell per digit.

    Same construction as the password-reset OTP email
    (``email_service.send_password_reset_otp_email``): letter-spacing is
    stripped by Outlook desktop, so the spacing has to come from real table
    cells or the digits run together. Renders nothing when no code was minted,
    which is what keeps older link-only sends unchanged.
    """
    if not raw_code:
        return ""
    cells = "".join(
        '<td align="center" style="padding:0 4px;">'
        '  <div style="display:inline-block;min-width:44px;padding:12px 4px;'
        '              background:#F1F5F9;border:1px solid #E2E8F0;border-radius:8px;'
        "              font-family:'Segoe UI',Helvetica,Arial,sans-serif;"
        '              font-size:26px;font-weight:700;color:#0F172A;letter-spacing:0;">'
        f"    {ch}"
        "  </div>"
        "</td>"
        for ch in raw_code
    )
    return (
        '<p style="color:#334155;line-height:1.55;margin:0 0 10px 0;">'
        "  Enter this code to verify your email:"
        "</p>"
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        '       style="margin:0 auto 8px auto;"><tr>' + cells + "</tr></table>"
        '<p style="color:#64748B;font-size:13px;text-align:center;margin:0 0 22px 0;">'
        "  The code expires in 15 minutes."
        "</p>"
    )


def _trial_length_days() -> int:
    """The trial length a customer is actually given.

    ``app/billing/trial`` owns the clock the customer experiences: it is what
    the countdown on the billing page reads, what the reminder ladder is
    scheduled against, and when access actually ends. Email copy has to quote
    that number and no other — this template previously hardcoded "30-day",
    which is ``app/commercial``'s value and is explicitly no longer
    authoritative for customers, so every new signup was promised more than
    twice the trial it received.

    Imported lazily to keep the commercial notification layer from taking a
    module-level dependency on the billing package, matching how
    ``billing.service`` imports the same constant.
    """
    from app.billing.trial import TRIAL_LENGTH_DAYS   # noqa: PLC0415

    return int(TRIAL_LENGTH_DAYS)


# ──────────────────────────────────────────────────────────────────────────
# WELCOME + VERIFICATION
# ──────────────────────────────────────────────────────────────────────────
def send_welcome_and_verification(
    db: Session,
    *,
    to_email: str,
    first_name: str,
    company_name: str,
    raw_verification_token: str,
    organization_id: str,
    workspace_id: Optional[str],
    user_id: int,
    correlation_id: str,
    raw_verification_code: Optional[str] = None,
) -> bool:
    # `raw_verification_code` is optional so every existing caller and test that
    # passes only a token keeps working and keeps sending a link-only email.
    verify_link = _verify_url(raw_verification_token)
    trial_days = _trial_length_days()
    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">Verify your email to finish setting up</h2>
      <p style="color:#334155;line-height:1.55;margin:0 0 22px 0;">
        Welcome to Temveris, {first_name}. <strong>{company_name}</strong> is
        ready — confirm this address and your workspace is yours.
      </p>
      {_code_block(raw_verification_code)}
      <div style="margin:24px 0;">{_btn("Verify Email", verify_link)}</div>
      <p style="color:#64748B;font-size:13px;line-height:1.55;">
        The button is valid for 48 hours. Trouble with it? Paste this into your
        browser: {verify_link}
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
             style="margin:26px 0 0 0;border-top:1px solid #E2E8F0;">
        <tr><td style="padding-top:18px;">
          <p style="color:#334155;line-height:1.55;margin:0;">
            Your <strong>{trial_days}-day free trial</strong> has started. No credit
            card is required, and nothing is charged without your authorisation.
          </p>
          <p style="color:#64748B;font-size:13px;line-height:1.55;margin:8px 0 0 0;">
            Inviting teammates and exporting data unlock once your email is verified.
          </p>
        </td></tr>
      </table>
    """
    html = _base_layout(content, preheader=f"Welcome to Temveris — verify your email")
    # ZT-IAM-01 sender governance: route through the seam so the canonical
    # security@ sender + support@ reply-to are used when sender identities are
    # enabled; None reply-to preserves today's default when they are disabled.
    reply_to = _notif_classification.sender_for(sender_key="support")
    ok = _send(to_email, "Welcome to Temveris — verify your email", html,
               reply_to=reply_to, classification="essential", family="iam.verify_email")

    emit_event(
        db,
        event_type="email_verification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"channel": "email", "to": to_email, "template": "welcome_and_verify"},
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )
    return ok


def send_verification_resend(
    db: Session,
    *,
    to_email: str,
    first_name: str,
    raw_verification_token: str,
    user_id: int,
    organization_id: Optional[str],
    correlation_id: Optional[str] = None,
    raw_verification_code: Optional[str] = None,
) -> bool:
    verify_link = _verify_url(raw_verification_token)
    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">Verify your email</h2>
      <p style="color:#334155;line-height:1.55;">
        Hi {first_name}, here's a fresh code and link.
      </p>
      {_code_block(raw_verification_code)}
      <p style="color:#64748B;font-size:13px;line-height:1.55;">
        Or use this link instead — it's valid for 48 hours.
      </p>
      <div style="margin:12px 0 0 0;">{_btn("Verify Email", verify_link)}</div>
    """
    html = _base_layout(content, preheader="Verify your Temveris email")
    # ZT-IAM-01 sender governance (see send_welcome_and_verification): security@
    # sender + support@ reply-to when identities are enabled; unchanged otherwise.
    reply_to = _notif_classification.sender_for(sender_key="support")
    ok = _send(to_email, "Verify your Temveris email", html,
               reply_to=reply_to, classification="essential", family="iam.verify_email")
    emit_event(
        db,
        event_type="email_verification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"channel": "email", "to": to_email, "template": "verify_resend"},
        organization_id=organization_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )
    return ok


# ──────────────────────────────────────────────────────────────────────────
# TRIAL LIFECYCLE SEQUENCE  (§6.1)
# ──────────────────────────────────────────────────────────────────────────
TRIAL_TEMPLATES = {
    "day_0": {
        "subject": "Your Temveris trial has started",
        "heading": "Welcome to your trial.",
        # Interpolated at send time from the same authority as every other
        # customer-facing trial figure — see `send_trial_email`.
        "body": ("You have {trial_days} days to evaluate Temveris with your whole "
                 "team. Today's goal: invite your first three teammates."),
        "cta_label": "Invite a teammate",
        "cta_path": "/team",
    },
    "day_1": {
        "subject": "Set up your first workforce policy",
        "heading": "Five minutes today, hours saved later.",
        "body": "Configure a workforce policy so your team's clock-ins, breaks, and timesheets follow the same rules. Without a policy your dashboards read as 'unconfigured'.",
        "cta_label": "Configure a policy",
        "cta_path": "/settings",
    },
    "day_7": {
        "subject": "How's your first week going?",
        "heading": "One week in — here's what most teams do next.",
        "body": "Teams that activate the dashboard in week one are 4× more likely to convert. Open your dashboard to see what's working.",
        "cta_label": "Open dashboard",
        "cta_path": "/dashboard",
    },
    "day_14": {
        "subject": "Your trial is halfway through",
        "heading": "Halfway point usage summary.",
        "body": "Halfway through your trial. Check the usage summary to see who is active, who needs help, and where the biggest gaps are.",
        "cta_label": "View summary",
        "cta_path": "/dashboard",
    },
    "day_18": {
        "subject": "Convert your Temveris trial",
        "heading": "Ready to keep going?",
        "body": "Add a payment method now to avoid any interruption when your trial expires. Annual plans get 20% off.",
        "cta_label": "Choose a plan",
        "cta_path": "/billing",
    },
    "day_25": {
        "subject": "5 days left in your Temveris trial",
        "heading": "Trial ends in five days.",
        "body": "Add a payment method to keep your team's data, dashboards, and audit trail running.",
        "cta_label": "Convert now",
        "cta_path": "/billing",
    },
    "day_30": {
        "subject": "Your Temveris trial has ended",
        "heading": "Your trial has ended.",
        "body": "Your workspace is paused. Add a payment method to restore full access. Your data is preserved.",
        "cta_label": "Reactivate",
        "cta_path": "/billing",
    },
    "day_32": {
        "subject": "Last chance to reactivate Temveris",
        "heading": "We've held your workspace for 48 hours.",
        "body": "Add a payment method now to restore full access. After day 35 we'll close the workspace and follow our retention policy.",
        "cta_label": "Reactivate",
        "cta_path": "/billing",
    },
    "day_35": {
        "subject": "Temveris workspace closure notice",
        "heading": "Closure warning",
        "body": "Your trial ended five days ago. We will close the workspace shortly. Reactivate now if you'd like to keep your data.",
        "cta_label": "Reactivate",
        "cta_path": "/billing",
    },
}


def send_trial_email(
    db: Session,
    *,
    day_marker: str,
    to_email: str,
    first_name: str,
    company_name: str,
    organization_id: str,
    user_id: int,
    correlation_id: Optional[str] = None,
) -> bool:
    tpl = TRIAL_TEMPLATES.get(day_marker)
    if tpl is None:
        _log.warning("No trial template for day_marker=%s", day_marker)
        return False

    cta_url = _app_url().rstrip("/") + tpl["cta_path"]
    # Bodies may reference {trial_days}; the rest contain no placeholders, so
    # format_map with a default leaves them untouched.
    body = tpl["body"].replace("{trial_days}", str(_trial_length_days()))
    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">{tpl['heading']}</h2>
      <p style="color:#334155;line-height:1.55;">Hi {first_name},</p>
      <p style="color:#334155;line-height:1.55;">{body}</p>
      <div style="margin:24px 0;">{_btn(tpl['cta_label'], cta_url)}</div>
      <p style="color:#94A3B8;font-size:12px;margin-top:32px;">
        Sent because {company_name} is on a Temveris trial.
      </p>
    """
    html = _base_layout(content, preheader=tpl["subject"])
    ok = _send(to_email, tpl["subject"], html)

    emit_event(
        db,
        event_type=("trial.started" if day_marker == "day_0" else "notification.sent") if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": f"trial_{day_marker}", "category": "trial_lifecycle",
                 "to": to_email, "company": company_name},
        organization_id=organization_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )
    return ok


# ──────────────────────────────────────────────────────────────────────────
# DUNNING EMAIL SEQUENCE  (§11.2 — failed payment must trigger dunning)
# ──────────────────────────────────────────────────────────────────────────
# Four touchpoints over a 14-day recovery window, after which the
# subscription transitions from grace_period → suspended. Each template
# is sent at most once per (organization, marker) pair (idempotent via
# the same TrialEmailSent table, namespace prefix "dunning_").
DUNNING_TEMPLATES = {
    "dunning_day_1": {
        "subject": "We couldn't process your Temveris payment",
        "heading": "Payment didn't go through.",
        "body": "Your most recent payment for Temveris was declined. Most often this is a temporary card issue — please update your payment method to keep your team's access uninterrupted.",
        "cta_label": "Update payment method",
        "cta_path": "/billing",
        "tone": "neutral",
    },
    "dunning_day_3": {
        "subject": "Action needed — Temveris payment still failing",
        "heading": "We're trying again, but we need your help.",
        "body": "It's been three days and we still haven't been able to charge your payment method. We'll keep retrying, but please update your billing details so your workspace doesn't drop into a restricted state.",
        "cta_label": "Update billing",
        "cta_path": "/billing",
        "tone": "warning",
    },
    "dunning_day_7": {
        "subject": "Final reminder — your Temveris workspace will be restricted",
        "heading": "One week of failed payments.",
        "body": "If we can't collect payment in the next 7 days, your workspace will move to restricted access — your team will be able to view data but not export, invite, or run reports. Update billing now to avoid that.",
        "cta_label": "Resolve now",
        "cta_path": "/billing",
        "tone": "warning",
    },
    "dunning_day_14": {
        "subject": "Your Temveris workspace has been restricted",
        "heading": "Workspace restricted.",
        "body": "We weren't able to collect payment over the last 14 days, so your workspace is now restricted. Your data is safe and your team can still log in to view it. Update your payment method to lift the restriction immediately.",
        "cta_label": "Reactivate",
        "cta_path": "/billing",
        "tone": "danger",
    },
}
_DUNNING_DAY_NUMBERS = {"dunning_day_1": 1, "dunning_day_3": 3, "dunning_day_7": 7, "dunning_day_14": 14}


def send_dunning_email(
    db: Session,
    *,
    day_marker: str,
    to_email: str,
    first_name: str,
    company_name: str,
    organization_id: str,
    user_id: int,
    correlation_id: Optional[str] = None,
) -> bool:
    tpl = DUNNING_TEMPLATES.get(day_marker)
    if tpl is None:
        _log.warning("No dunning template for marker=%s", day_marker)
        return False

    cta_url = _app_url().rstrip("/") + tpl["cta_path"]
    accent = {"neutral": "#11496A", "warning": "#B45309", "danger": "#B91C1C"}.get(tpl["tone"], "#11496A")
    content = f"""
      <h2 style="margin:0 0 12px 0;color:{accent};">{tpl['heading']}</h2>
      <p style="color:#334155;line-height:1.55;">Hi {first_name},</p>
      <p style="color:#334155;line-height:1.55;">{tpl['body']}</p>
      <div style="margin:24px 0;">{_btn(tpl['cta_label'], cta_url, color=accent)}</div>
      <p style="color:#94A3B8;font-size:12px;margin-top:32px;">
        Sent because {company_name}'s Temveris subscription has a failed payment.
        Reply to this email if you believe this is a mistake.
      </p>
    """
    html = _base_layout(content, preheader=tpl["subject"])
    ok = _send(to_email, tpl["subject"], html)
    emit_event(
        db,
        event_type="notification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": day_marker, "to": to_email, "company": company_name,
                 "category": "dunning"},
        organization_id=organization_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )
    return ok


# ──────────────────────────────────────────────────────────────────────────
# PROVISIONING NOTIFICATIONS
# ──────────────────────────────────────────────────────────────────────────
def send_provisioning_in_progress(
    db: Session,
    *,
    to_email: str,
    first_name: str,
    company_name: str,
    organization_id: Optional[str],
    correlation_id: Optional[str],
) -> bool:
    """Sent only on payment-succeeded but workspace-not-ready paths (§6.2)."""
    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">We're preparing your workspace</h2>
      <p style="color:#334155;line-height:1.55;">
        Hi {first_name}, your payment was successful and we're setting up
        <strong>{company_name}</strong>'s workspace. You'll get another email
        when it's ready — usually within a minute or two.
      </p>
      <p style="color:#334155;line-height:1.55;">
        If you don't see a confirmation in 10 minutes, our team has already been
        alerted and will follow up directly.
      </p>
    """
    html = _base_layout(content, preheader="We're preparing your workspace")
    ok = _send(to_email, "We're preparing your Temveris workspace", html)
    emit_event(
        db,
        event_type="notification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": "provisioning_in_progress", "category": "provisioning",
                 "to": to_email},
        organization_id=organization_id,
        correlation_id=correlation_id,
    )
    return ok


def _resolve_owner(db: Session, organization_id: str):
    """Return (organization, owner_user) for a commercial org, or (None, None).

    Shared recipient resolution for owner-addressed billing emails.
    """
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    owner = (
        db.query(User).filter_by(id=org.owner_user_id).one_or_none()
        if (org and org.owner_user_id) else None
    )
    return org, owner


# ──────────────────────────────────────────────────────────────────────────
# ZT-BIL-02 — Invoice / Receipt / Refund  (§12; Essential - billing record)
# Scope: Receipt (paid invoice) and Refund. Standalone-Invoice and Credit Note
# are deferred until those workflows exist. Recipient is the workspace owner.
# ──────────────────────────────────────────────────────────────────────────
def send_billing_document(
    db: Session,
    *,
    organization_id: str,
    invoice,
    document_type: str,
    correlation_id: Optional[str] = None,
) -> bool:
    """Send a billing Receipt or Refund confirmation to the workspace owner.

    Content follows the ZT-BIL-02 template (secure link only, never an
    attachment). Essential/non-suppressible; sends from billing@ when sender
    identities are enabled (family="billing.invoice").
    """
    org, owner = _resolve_owner(db, organization_id)
    if org is None or owner is None or not owner.email:
        _log.warning("ZT-BIL-02 skipped — no owner/email for org %s", organization_id)
        return False

    first_name = (owner.name or owner.email).split()[0]
    workspace_name = org.name

    def _money(cents: Optional[int]) -> str:
        return f"{invoice.currency} {((cents or 0) / 100):,.2f}"

    document_number = invoice.number
    amount_with_currency = _money(invoice.total_cents)
    tax_summary = _money(invoice.tax_cents)
    payment_status = invoice.status
    doc_dt = invoice.paid_at or invoice.issued_at or datetime.now(timezone.utc)
    document_date = doc_dt.strftime("%Y-%m-%d")
    billing_url = f"{FRONTEND_URL.rstrip('/')}/billing"

    if document_type == "Refund":
        subject = f"Refund processed for {workspace_name}"
        preheader = "Review the refund amount, currency, and secure billing document."
    else:
        subject = f"Temveris {document_type} {document_number}"
        preheader = "Review the amount, currency, tax, payment status, and secure billing document."

    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">Your Temveris {document_type} for {workspace_name} is available.</h2>
      <p style="color:#334155;line-height:1.55;">Hello {first_name},</p>
      <p style="color:#334155;line-height:1.55;">
        Document number: <strong>{document_number}</strong><br>
        Amount: <strong>{amount_with_currency}</strong><br>
        Tax: {tax_summary}<br>
        Status: {payment_status}<br>
        Date: {document_date}
      </p>
      <p style="color:#334155;line-height:1.55;">The complete document is available securely in Billing.</p>
      <div style="margin:24px 0;">{_btn("View billing document", billing_url)}</div>
      <p style="color:#94A3B8;font-size:12px;margin-top:24px;">Billing document: {billing_url}</p>
    """
    html = _base_layout(content, preheader=preheader)

    # Reply-To mirrors the sender-identity flag: billing@ when enabled, else the
    # default (None -> _send uses ADMIN_EMAIL) so current behavior is preserved.
    reply_to = _notif_classification.sender_for(sender_key="billing")

    ok = _send(
        owner.email, subject, html,
        reply_to=reply_to,
        classification="essential",
        family="billing.invoice",
    )
    emit_event(
        db,
        event_type="notification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": "billing_document", "document_type": document_type,
                 "invoice_id": invoice.id, "to": owner.email, "category": "billing"},
        organization_id=organization_id,
        user_id=owner.id,
        correlation_id=correlation_id,
    )
    return ok


# ──────────────────────────────────────────────────────────────────────────
# ZT-BIL-07 — Cancellation Scheduled / Canceled / Reversed
# (§12; Essential - subscription). Recipient is the workspace owner. Only
# fields present in the data model are rendered; the doc's retention / export /
# access-end tokens are intentionally omitted (not stored) and surfaced via the
# "Review cancellation" link instead. Period-end "Completed" is deferred.
# ──────────────────────────────────────────────────────────────────────────
_CANCELLATION_COPY = {
    "scheduled": (
        "Temveris cancellation scheduled for {workspace}",
        "Your Temveris subscription cancellation is scheduled.",
        "Review the effective date, remaining access, final billing, export options, and retention schedule.",
    ),
    "canceled": (
        "Temveris subscription canceled for {workspace}",
        "Your Temveris subscription has been canceled.",
        "Review the remaining access, final billing, export options, and retention schedule.",
    ),
    "reversed": (
        "Temveris cancellation reversed",
        "Your Temveris cancellation has been reversed.",
        "Your subscription is active again. No further action is required.",
    ),
}


def send_cancellation(
    db: Session,
    *,
    organization_id: str,
    subscription,
    cancellation_status: str,
    correlation_id: Optional[str] = None,
) -> bool:
    """Send a ZT-BIL-07 cancellation email (scheduled / canceled / reversed).

    Essential/non-suppressible; sends from billing@ when sender identities are
    enabled (family="billing.cancellation"). Only verified fields are rendered.
    """
    tpl = _CANCELLATION_COPY.get(cancellation_status)
    if tpl is None:
        _log.warning("ZT-BIL-07 unknown cancellation_status=%s", cancellation_status)
        return False

    org, owner = _resolve_owner(db, organization_id)
    if org is None or owner is None or not owner.email:
        _log.warning("ZT-BIL-07 skipped — no owner/email for org %s", organization_id)
        return False

    subject_tmpl, heading, guidance = tpl
    workspace_name = org.name
    subject = subject_tmpl.format(workspace=workspace_name)
    billing_url = f"{FRONTEND_URL.rstrip('/')}/billing"

    effective_dt = subscription.cancelled_at or subscription.cancel_requested_at
    effective_line = ""
    if effective_dt is not None:
        effective_line = (
            f'<p style="color:#334155;line-height:1.55;">'
            f'Effective date: <strong>{effective_dt.strftime("%Y-%m-%d")}</strong></p>'
        )

    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">{heading}</h2>
      <p style="color:#334155;line-height:1.55;">Hello {(owner.name or owner.email).split()[0]},</p>
      <p style="color:#334155;line-height:1.55;">
        The Temveris subscription for <strong>{workspace_name}</strong> is {cancellation_status}.
      </p>
      {effective_line}
      <p style="color:#334155;line-height:1.55;">{guidance}</p>
      <p style="color:#334155;line-height:1.55;">
        Cancellation does not remove legal or contractual retention obligations.
      </p>
      <div style="margin:24px 0;">{_btn("Review cancellation", billing_url)}</div>
      <p style="color:#94A3B8;font-size:12px;margin-top:24px;">Subscription: {billing_url}</p>
    """
    html = _base_layout(
        content,
        preheader="Review the effective date, remaining access, final billing, export options, and retention schedule.",
    )

    reply_to = _notif_classification.sender_for(sender_key="billing")
    ok = _send(
        owner.email, subject, html,
        reply_to=reply_to,
        classification="essential",
        family="billing.cancellation",
    )
    emit_event(
        db,
        event_type="notification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": "cancellation", "cancellation_status": cancellation_status,
                 "subscription_id": subscription.id, "to": owner.email, "category": "billing"},
        organization_id=organization_id,
        user_id=owner.id,
        correlation_id=correlation_id,
    )
    return ok


# ──────────────────────────────────────────────────────────────────────────
# ZT-BIL-01 — Subscription Activated or Plan Changed
# (§12; Essential - billing). Sent on trial conversion and genuine plan /
# cadence / seat changes (NOT payment recovery — that is ZT-BIL-05). The doc's
# next_invoice_date token is not in the data model and is intentionally omitted;
# details live behind the "View subscription" link.
# ──────────────────────────────────────────────────────────────────────────
def send_subscription_confirmation(
    db: Session,
    *,
    organization_id: str,
    subscription,
    plan_event: str,
    correlation_id: Optional[str] = None,
) -> bool:
    """Send a ZT-BIL-01 subscription-activated / plan-changed confirmation.

    Essential/non-suppressible; sends from billing@ when sender identities are
    enabled (family="billing.plan_changed"). Only verified fields are rendered.
    """
    org, owner = _resolve_owner(db, organization_id)
    if org is None or owner is None or not owner.email:
        _log.warning("ZT-BIL-01 skipped — no owner/email for org %s", organization_id)
        return False

    workspace_name = org.name
    effective_dt = subscription.active_since or datetime.now(timezone.utc)
    billing_url = f"{FRONTEND_URL.rstrip('/')}/billing"
    subject = f"Temveris {plan_event} confirmed for {workspace_name}"

    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">Your Temveris subscription for {workspace_name} has been {plan_event}.</h2>
      <p style="color:#334155;line-height:1.55;">Hello {(owner.name or owner.email).split()[0]},</p>
      <p style="color:#334155;line-height:1.55;">
        Plan: <strong>{subscription.plan_code}</strong><br>
        Billing cadence: {subscription.billing_cycle}<br>
        Included seats or usage: {subscription.seat_count}<br>
        Effective date: {effective_dt.strftime("%Y-%m-%d")}
      </p>
      <p style="color:#334155;line-height:1.55;">Review the order summary and billing terms in your workspace.</p>
      <div style="margin:24px 0;">{_btn("View subscription", billing_url)}</div>
      <p style="color:#94A3B8;font-size:12px;margin-top:24px;">Billing: {billing_url}</p>
    """
    html = _base_layout(
        content,
        preheader="Review the plan, effective date, billing cadence, and included usage.",
    )

    reply_to = _notif_classification.sender_for(sender_key="billing")
    ok = _send(
        owner.email, subject, html,
        reply_to=reply_to,
        classification="essential",
        family="billing.plan_changed",
    )
    emit_event(
        db,
        event_type="notification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": "subscription_confirmation", "plan_event": plan_event,
                 "subscription_id": subscription.id, "to": owner.email, "category": "billing"},
        organization_id=organization_id,
        user_id=owner.id,
        correlation_id=correlation_id,
    )
    return ok


# ──────────────────────────────────────────────────────────────────────────
# ZT-BIL-05 (recovery only) — "Payment received: Temveris service restored"
# (§12; Critical - billing, non-suppressible). Sent when a payment brings a
# past_due/grace_period subscription back to active. Past-due and restricted
# states remain served by the existing dunning sequence. Only verified values
# are rendered; due_date / retention_status / billing_action_message are not in
# the data model and are intentionally omitted (see "Review billing status").
# ──────────────────────────────────────────────────────────────────────────
def send_payment_recovery(
    db: Session,
    *,
    organization_id: str,
    subscription,
    invoice,
    correlation_id: Optional[str] = None,
) -> bool:
    """Send the ZT-BIL-05 service-restored confirmation to the workspace owner.

    Critical/non-suppressible; sends from billing@ when sender identities are
    enabled (family="billing.recovery").
    """
    org, owner = _resolve_owner(db, organization_id)
    if org is None or owner is None or not owner.email:
        _log.warning("ZT-BIL-05 skipped — no owner/email for org %s", organization_id)
        return False

    workspace_name = org.name
    amount_with_currency = f"{invoice.currency} {((invoice.total_cents or 0) / 100):,.2f}"
    billing_url = f"{FRONTEND_URL.rstrip('/')}/billing"
    subject = "Payment received: Temveris service restored"

    content = f"""
      <h2 style="margin:0 0 12px 0;color:#0F172A;">Payment received — your Temveris service is restored.</h2>
      <p style="color:#334155;line-height:1.55;">Hello {(owner.name or owner.email).split()[0]},</p>
      <p style="color:#334155;line-height:1.55;">
        The billing status for <strong>{workspace_name}</strong> is now restored.
      </p>
      <p style="color:#334155;line-height:1.55;">
        Amount received: <strong>{amount_with_currency}</strong><br>
        Access status: restored
      </p>
      <p style="color:#334155;line-height:1.55;">Review the billing status and history in your workspace.</p>
      <div style="margin:24px 0;">{_btn("Review billing status", billing_url)}</div>
      <p style="color:#94A3B8;font-size:12px;margin-top:24px;">Billing: {billing_url}</p>
    """
    html = _base_layout(
        content,
        preheader="Your payment was received and Temveris access has been restored.",
    )

    reply_to = _notif_classification.sender_for(sender_key="billing")
    ok = _send(
        owner.email, subject, html,
        reply_to=reply_to,
        classification="critical",
        family="billing.recovery",
    )
    emit_event(
        db,
        event_type="notification.sent" if ok else "email_delivery.failed",
        source_service="notification-service",
        payload={"template": "payment_recovery", "subscription_id": subscription.id,
                 "invoice_id": invoice.id, "to": owner.email, "category": "billing"},
        organization_id=organization_id,
        user_id=owner.id,
        correlation_id=correlation_id,
    )
    return ok
