"""
Temveris — RECIPIENT-AUTHORITY: who is TOLD about an ECO record.
=================================================================

ZT-SPEC-ECO-002 v2.0 §33.1, owner ruling D-3 (approved 2026-09-21).

STATUS: IMPLEMENTED AND WIRED INTO EVENT CREATION ONLY. Nothing is delivered.
The designation table has zero rows, so every tenant resolves today to the
Worker alone — which is byte-identical in effect to the pre-Stage-5 behaviour.

THE PUBLIC ENTRY POINTS

    resolve_eco_recipients(db, organization_id=..., worker_user_id=...)
    build_recipient_snapshot(resolved)          # freeze it onto the event
    recipients_from_snapshot(snapshot)          # read history; NO db argument
    original_recipient_user_ids(snapshot)       # §33.1 "all original recipients"

THE FOUR AUDIENCES
------------------
§33.1's matrix names exactly four, and ruling D-3 replaced the earlier A/B/C
recipient options with them:

    WORKER           the event's subject      always resolved
    PRIMARY ADMIN    designated               "Always" in the matrix
    DIRECT MANAGER   per-Worker relationship  "If configured"
    HR ADMIN         designated               "If configured"

RECIPIENT AUTHORITY IS NOT VIEW AUTHORITY, AND IT IS NOT DELIVERY
------------------------------------------------------------------
    VIEW-AUTHORITY        may this caller READ this record    app.eco_view_authority
    RECIPIENT-AUTHORITY   who should be TOLD about it         <- this package
    ALERT-DELIVERY        actually telling them               <- still open

The third is what still blocks GV-07, and this package deliberately does not
approach it. §34 is explicit that channel selection, notification preferences,
transport, retry and suppression belong to the central Temveris notification
service and "MUST NOT be reimplemented here".

WHAT IS DELIBERATELY ABSENT
---------------------------
* **No delivery of any kind.** No transport, no channel selection, no
  preference resolution, no retry, no suppression, no DeliveryAttempt, no
  email. A test asserts this package imports none of those modules.
* **No DIRECT MANAGER resolution.** There is no reporting line in the schema.
  §6 of the authorization forbids inferring one from team membership, an email
  domain or an arbitrary admin, and forbids falling back to PRIMARY ADMIN — so
  the audience resolves to nobody, with a basis that names the missing
  capability rather than implying the tenant is misconfigured.
* **No designation-management surface.** No route, no service, no seed.
  Creating and revoking a designation is an administrative action with its own
  authorization question, which nobody has ruled on. Tests insert rows
  directly; no application path does.
* **No role mapping.** Not even a seed. ``User.role`` is never read here.
* **No effective dating.** Ruling D-3.6. The only historical boundary that
  matters is EVENT CREATION -> SNAPSHOT.
* **No new audiences for ALERT.** ``AUTHORIZED_AUDIENCES`` stays WORKER-only:
  the authority now exists, but delivering to a non-Worker audience is Stage 6.

THE PERMANENCE RULE (D-3.7)
---------------------------
Once frozen onto an event, the snapshot is authoritative permanently. Later
deactivation, deletion or re-designation changes nothing already frozen. An
undeliverable recipient is a delivery failure the notification service
represents; it is never a reason to mutate historical recipient authority.
``recipients_from_snapshot`` takes no ``Session``, so that rule is enforced by
the signature and not merely documented.
"""

from .models import EcoRecipientDesignation
from .outcomes import (
    AUDIENCE_ORDER,
    AudienceResolution,
    AudienceState,
    CrossTenantRecipient,
    DESIGNATABLE_AUDIENCES,
    EcoAudience,
    RecipientAuthorityUnavailable,
    ResolvedRecipients,
)
from .service import (
    SNAPSHOT_VERSION,
    build_recipient_snapshot,
    original_recipient_user_ids,
    recipients_from_snapshot,
    resolve_eco_recipients,
)

__all__ = [
    # public entry points
    "resolve_eco_recipients",
    "build_recipient_snapshot",
    "recipients_from_snapshot",
    "original_recipient_user_ids",
    # vocabulary
    "AUDIENCE_ORDER",
    "AudienceResolution",
    "AudienceState",
    "DESIGNATABLE_AUDIENCES",
    "EcoAudience",
    "SNAPSHOT_VERSION",
    # failures
    "CrossTenantRecipient",
    "RecipientAuthorityUnavailable",
    # persistence
    "EcoRecipientDesignation",
]
