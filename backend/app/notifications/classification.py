"""Temveris — Email Classification & Family Registry
=====================================================
Reference: "Commercial Email Communications Estate" §2 (Purpose and
classification) and §3 (Canonical sender identities).

This module is *pure metadata* — no I/O, no side effects, and no dependency on
the email transport. It exists so later, opt-in features (canonical sender
identities and preference/quiet-hours enforcement) can look up, for any email
"family":

  1. Its canonical classification (Critical / Essential / Operational /
     Commercial-account / Customer-success / Marketing), which drives
     suppression and quiet-hours eligibility.
  2. Its canonical sender-identity KEY, and the resolved From address for that
     key (opt-in: only takes effect when ENABLE_SENDER_IDENTITIES is set;
     otherwise resolution returns None and the caller keeps using the existing
     global FROM_EMAIL — see ``sender_for``).

IMPORTANT — this changes nothing on its own:
  * No existing module imports this file.
  * The existing 16 transactional senders and the commercial senders are not
    touched.
  * The safe default for an UNKNOWN family is ESSENTIAL, which is never
    suppressed — so even once this is wired in, the conservative default is
    "always deliver".

NOTE: This is a distinct axis from the worker-facing notification *types* and
*postures* in ``app/notifications/router.py`` (LOCKED_TYPES / CRITICAL_TYPES /
VALID_POSTURES). Those describe a recipient's preferences; this describes the
purpose of an outbound email. They are intentionally not merged.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

# ── Canonical classifications (spec §2) ──────────────────────────────────────
CRITICAL           = "critical"            # security / payment-final / incident / data-rights
ESSENTIAL          = "essential"           # identity, approvals, receipts — non-suppressible
OPERATIONAL        = "operational"         # workforce / attendance / timesheet routine
COMMERCIAL_ACCOUNT = "commercial_account"  # trial / plan lifecycle — mandatory for owner/billing
CUSTOMER_SUCCESS   = "customer_success"    # adoption summaries
MARKETING          = "marketing"           # consent-required product news

# ECO administrator/operational notifications (ZT-SPEC-ECO-002 §33.1, owner
# architecture decision D-B, option A). DISTINCT from OPERATIONAL, and the
# distinction is load-bearing rather than cosmetic:
#
#   OPERATIONAL is suppressible and quiet-hours eligible. §33.1 marks the
#   PRIMARY ADMIN notification "Always (operational)", so classifying an
#   administrator alert as OPERATIONAL would let a quiet-hours window or a
#   posture setting DROP a notification the specification calls "Always".
#
# The owner ruled a distinct non-suppressible class rather than an exemption
# inside the suppression logic, so the guarantee lives in the vocabulary: this
# value is absent from SUPPRESSIBLE and QUIET_HOURS_ELIGIBLE below, and a class
# that is not a member cannot be suppressed by construction. An exemption would
# have been a branch a later edit could remove.
#
# OPERATIONAL's meaning is UNCHANGED. No existing family moves class.
ECO_ADMIN_OPERATIONAL = "eco_admin_operational"

ALL = {CRITICAL, ESSENTIAL, OPERATIONAL, COMMERCIAL_ACCOUNT, CUSTOMER_SUCCESS,
       MARKETING, ECO_ADMIN_OPERATIONAL}

# Classes a recipient's preferences / unsubscribe may suppress. Critical,
# Essential and Commercial-account mail is always delivered (spec §2:
# "Essential and Critical messages ignore marketing preferences").
#
# ECO_ADMIN_OPERATIONAL is deliberately ABSENT (D-B): §33.1's "Always".
SUPPRESSIBLE = {OPERATIONAL, CUSTOMER_SUCCESS, MARKETING}

# Classes a marketing-scope unsubscribe applies to.
MARKETING_LIKE = {MARKETING, CUSTOMER_SUCCESS}

# Classes that are quiet-hours eligible. Only suppressible classes qualify:
# Temveris has no deferral queue yet, so applying quiet hours to Essential /
# Commercial-account mail would DROP it rather than delay it. Documented choice.
# ECO_ADMIN_OPERATIONAL is excluded here too, by being excluded from
# SUPPRESSIBLE: deferring an "Always" administrator alert into a quiet-hours
# window would drop it, since there is still no deferral queue.
QUIET_HOURS_ELIGIBLE = SUPPRESSIBLE


# ── Family registry ──────────────────────────────────────────────────────────
# family_key -> (classification, sender_key)
# sender_key is a stable label resolved to an address by a later feature; it is
# inert metadata here.
FAMILIES = {
    # Identity, Access & Security
    "iam.verify_email":       (ESSENTIAL, "security"),
    "iam.password_reset":     (ESSENTIAL, "security"),
    "iam.password_reset_otp": (ESSENTIAL, "security"),
    "iam.password_changed":   (ESSENTIAL, "security"),
    "iam.invite":             (ESSENTIAL, "notifications"),

    # Onboarding / Trial (Commercial-account = mandatory for owner/billing admin)
    "onboarding.welcome_verify": (ESSENTIAL, "notifications"),
    "onboarding.verify_resend":  (ESSENTIAL, "security"),
    "onboarding.user_activated": (OPERATIONAL, "welcome"),
    "trial.started":             (COMMERCIAL_ACCOUNT, "notifications"),
    "trial.lifecycle":           (COMMERCIAL_ACCOUNT, "notifications"),
    "provisioning.in_progress":  (OPERATIONAL, "notifications"),

    # Billing
    "billing.dunning":        (CRITICAL, "billing"),
    "billing.payment_failed": (CRITICAL, "billing"),
    "billing.recovery":       (CRITICAL, "billing"),
    "billing.invoice":        (ESSENTIAL, "billing"),
    "billing.cancellation":   (ESSENTIAL, "billing"),
    "billing.plan_changed":   (ESSENTIAL, "billing"),

    # Leave / Approvals
    "leave.submitted": (OPERATIONAL, "approvals"),
    "leave.request":  (ESSENTIAL, "approvals"),
    "leave.decision": (OPERATIONAL, "approvals"),

    # Scheduling & Attendance (operational / policy)
    "attendance.schedule":       (OPERATIONAL, "notifications"),
    "attendance.absent":         (OPERATIONAL, "notifications"),
    "attendance.late":           (OPERATIONAL, "notifications"),
    "attendance.idle":           (OPERATIONAL, "notifications"),
    "attendance.early_clockout": (OPERATIONAL, "notifications"),
    "attendance.break_overrun":  (OPERATIONAL, "notifications"),
    "risk.alert":                (OPERATIONAL, "notifications"),

    # Timesheets / adjustments
    "timeadjust.added":    (OPERATIONAL, "approvals"),
    "timeadjust.reverted": (OPERATIONAL, "approvals"),

    # Support / Service / Data rights
    "support.ticket":     (ESSENTIAL, "support"),
    "system.maintenance": (ESSENTIAL, "status"),
    "system.incident":    (CRITICAL, "status"),
    "data.rights":        (CRITICAL, "privacy"),

    # Customer success / marketing
    "cs.adoption_summary": (CUSTOMER_SUCCESS, "success"),
    "marketing.campaign":  (MARKETING, "updates"),
}

# Safe fallback for an unknown family: ESSENTIAL (never suppressed) from the
# default operational sender.
_DEFAULT: Tuple[str, str] = (ESSENTIAL, "notifications")


def classify(family: Optional[str]) -> Tuple[str, str]:
    """Return (classification, sender_key) for a family key."""
    if not family:
        return _DEFAULT
    return FAMILIES.get(family, _DEFAULT)


def classification_of(family: Optional[str]) -> str:
    return classify(family)[0]


def sender_key_of(family: Optional[str]) -> str:
    return classify(family)[1]


def is_suppressible(classification: Optional[str]) -> bool:
    return (classification or ESSENTIAL).lower() in SUPPRESSIBLE


# ── Canonical sender identities (spec §3) ────────────────────────────────────
# Resolves a sender_key to a From address. OPT-IN and default-safe:
#
#   * Until ``ENABLE_SENDER_IDENTITIES`` is truthy, ``sender_for()`` returns
#     None and every email keeps using the existing global FROM_EMAIL
#     (support@temveris.example). Delivery is unchanged.
#   * When enabled, each family sends from its canonical address on the verified
#     sending domain (default temveris.example). Enable ONLY after the domain's
#     SPF/DKIM/DMARC are verified in the ESP, or new addresses will bounce.
#
# Individual addresses are overridable per-key via SENDER_<KEY> env vars.
def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Feature flag — OFF by default. Read once at import.
SENDER_IDENTITIES_ENABLED = _flag("ENABLE_SENDER_IDENTITIES", "false")

# Verified sending domain (must have SPF/DKIM/DMARC configured in the ESP).
_SENDER_DOMAIN = os.getenv("EMAIL_SENDER_DOMAIN", "temveris.example").strip()

# sender_key -> address, each overridable via SENDER_<KEY> env var.
SENDER_IDENTITIES = {
    "notifications": os.getenv("SENDER_NOTIFICATIONS", f"notifications@{_SENDER_DOMAIN}"),
    "security":      os.getenv("SENDER_SECURITY",      f"security@{_SENDER_DOMAIN}"),
    "approvals":     os.getenv("SENDER_APPROVALS",     f"approvals@{_SENDER_DOMAIN}"),
    "payroll":       os.getenv("SENDER_PAYROLL",       f"payroll@{_SENDER_DOMAIN}"),
    "integrations":  os.getenv("SENDER_INTEGRATIONS",  f"integrations@{_SENDER_DOMAIN}"),
    "billing":       os.getenv("SENDER_BILLING",       f"billing@{_SENDER_DOMAIN}"),
    "support":       os.getenv("SENDER_SUPPORT",       f"support@{_SENDER_DOMAIN}"),
    "status":        os.getenv("SENDER_STATUS",        f"status@{_SENDER_DOMAIN}"),
    "privacy":       os.getenv("SENDER_PRIVACY",       f"privacy@{_SENDER_DOMAIN}"),
    "updates":       os.getenv("SENDER_UPDATES",       f"updates@{_SENDER_DOMAIN}"),
    "success":       os.getenv("SENDER_SUCCESS",       f"success@{_SENDER_DOMAIN}"),
    "welcome":       os.getenv("SENDER_WELCOME",       f"welcome@{_SENDER_DOMAIN}"),
}


def sender_for(
    *,
    family: Optional[str] = None,
    classification: Optional[str] = None,  # accepted for call-site symmetry; unused
    sender_key: Optional[str] = None,
) -> Optional[str]:
    """Resolve a From address for a family/sender_key.

    Returns None when sender identities are disabled (the default), so the
    caller falls back to the global FROM_EMAIL — i.e. zero behavior change until
    ENABLE_SENDER_IDENTITIES is explicitly turned on.
    """
    if not SENDER_IDENTITIES_ENABLED:
        return None
    key = sender_key
    if key is None and family is not None:
        key = sender_key_of(family)
    return SENDER_IDENTITIES.get(key or "notifications")
