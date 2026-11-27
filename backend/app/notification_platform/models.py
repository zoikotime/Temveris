"""
The platform's request-idempotency store.
=========================================

Owner ruling D-4. ONE ROW PER LOGICAL HANDOFF.

WHY THIS IS NOT THE ECO DELIVERY RECORD
---------------------------------------
D-4 is explicit that two different identities are in play and must not be
collapsed:

    PLATFORM REQUEST IDENTITY   (eco_event_id, recipient_user_id)
    ECO DELIVERY RECORD IDENTITY (threshold_event_id, recipient_id, channel)

The request identity exists BEFORE a channel does — that is the whole reason
D-4 exists, because ECO cannot name a channel it has not been told yet. The ECO
record identity includes one. So the ECO attempt table is structurally unable
to serve as this store: it would need a channel to key a row that is created
before any channel is chosen.

They also belong to different owners. This table is platform-owned; ECO never
reads or writes it.

WHY IT FOLLOWS ``CommercialIdempotencyKey``
-------------------------------------------
That table already solves this exact problem in this repository, and the shape
is not accidental:

  * ``UNIQUE(scope, idempotency_key)`` — the database, not the application,
    is what makes concurrent duplicates impossible.
  * ``request_hash`` — answers "same key, different payload" by rejecting it
    rather than silently serving the first result for the second request.
  * ``status`` in_progress -> completed — distinguishes "already done" from
    "somebody is doing it right now", which a boolean cannot.
  * ``response_json`` — lets a replay return the ORIGINAL result rather than
    recomputing one that might differ.

D-4 requires every one of those properties. Following a pattern already proven
here is better than minting a second one that has to earn the same trust.

WHAT IS STORED, AND WHY EACH FIELD IS NEEDED
--------------------------------------------
``correlation_id`` and ``channel`` are columns in their own right, not merely
fields inside ``result_json``, because D-4 requires a replay to return the SAME
channel and the SAME correlation id. Storing them where they can be read and
asserted directly — rather than only inside an opaque blob — is what makes that
guarantee checkable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, DateTime, Index, Integer, JSON, String,
    UniqueConstraint,
)

from database.database import Base

from .contracts import MAX_FAILURE_DETAIL_CHARS

#: Mirrors ``CommercialIdempotencyKey.scope``: one namespace per logical
#: operation, so a key minted here can never collide with one minted elsewhere.
SCOPE_NOTIFICATION_DISPATCH = "notification_dispatch"

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationDispatchRequest(Base):
    """One logical handoff, and the result it produced.

    Platform-owned. Nothing in ``app/eval_event``, ``app/alert`` or any other
    ECO package reads or writes this table, and a test asserts that.
    """

    __tablename__ = "notification_dispatch_request"

    id = Column(Integer, primary_key=True)

    #: Namespace, per the ``CommercialIdempotencyKey`` convention.
    scope = Column(String(40), nullable=False,
                   default=SCOPE_NOTIFICATION_DISPATCH)

    #: D-4's request key, rendered. See ``idempotency.request_key``.
    idempotency_key = Column(String(200), nullable=False)

    #: Tenant scoping. Carried so a platform-side read can be tenant-filtered
    #: without parsing the key, and so cross-tenant leakage is visible in the
    #: data rather than only in the code path that wrote it.
    organization_id = Column(Integer, nullable=False, index=True)

    #: The two components of the request identity, stored separately from the
    #: rendered key so they stay queryable.
    eco_event_id = Column(Integer, nullable=False)
    recipient_user_id = Column(Integer, nullable=False)

    #: sha256 over the canonical payload. D-4 clause 4: the same key with a
    #: materially different payload is REJECTED, never quietly served the
    #: first result.
    request_hash = Column(String(64), nullable=False)

    #: D-3. Generated at handoff ACCEPTANCE — before channel selection — so it
    #: exists even for a request that never reaches a channel, and so it cannot
    #: be mistaken for something a provider issued.
    correlation_id = Column(String(64), nullable=False)

    status = Column(String(20), nullable=False, default=STATUS_IN_PROGRESS)

    #: The platform's selection. NULL while in progress, and NULL on a result
    #: that never selected one (``NO_ELIGIBLE_CHANNEL``). D-4 requires a replay
    #: to return this same value.
    channel = Column(String(32), nullable=True)

    outcome = Column(String(16), nullable=True)
    reason_code = Column(String(48), nullable=True)
    #: D-5.5: <= 512 characters, sanitised, no Worker PII, NULL permitted.
    failure_detail = Column(String(MAX_FAILURE_DETAIL_CHARS), nullable=True)

    #: Everything needed to rebuild the original ``DispatchResult`` on replay.
    result_json = Column(JSON, nullable=True)

    created_at_utc = Column(DateTime(timezone=True), nullable=False,
                            default=_utcnow)
    completed_at_utc = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # THE guarantee. Two concurrent handoffs for one logical request race
        # on this constraint and exactly one wins — enforced by the database,
        # not by application-level checking, which is the only way it holds
        # under concurrency.
        UniqueConstraint("scope", "idempotency_key",
                         name="uq_notification_dispatch_request_key"),
        Index("ix_notification_dispatch_request_event",
              "eco_event_id", "recipient_user_id"),
        CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_notification_dispatch_request_status"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('dispatched', 'failed')",
            name="ck_notification_dispatch_request_outcome"),
        CheckConstraint(
            "failure_detail IS NULL OR length(failure_detail) <= 512",
            name="ck_notification_dispatch_request_detail_len"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<NotificationDispatchRequest {self.idempotency_key} "
                f"{self.status} {self.outcome} {self.channel}>")


__all__ = [
    "SCOPE_NOTIFICATION_DISPATCH",
    "STATUS_COMPLETED",
    "STATUS_IN_PROGRESS",
    "NotificationDispatchRequest",
]
