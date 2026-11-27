"""
Platform request idempotency (owner ruling D-4).
================================================

    request_key(eco_event_id, recipient_user_id)  ->  "eco:<event>:<recipient>"

D-4's guarantee, restated as the five things this module must make true:

  1. the same logical handoff produces ONE logical dispatch;
  2. a replay returns the SAME channel;
  3. a replay returns the SAME correlation_id;
  4. a replay returns the ORIGINAL logical result;
  5. no duplicate dispatch is created.

WHY THE KEY CANNOT INCLUDE THE CHANNEL
--------------------------------------
Because the channel does not exist yet. The platform selects it, and D-4 exists
precisely to give ECO a key it can form BEFORE the selection it is not allowed
to make. The three-part ECO record identity
``(threshold_event_id, recipient_id, channel)`` is a different identity, applied
at a later moment, and D-4 forbids collapsing the two.

CONCURRENCY
-----------
Duplicate detection is the ``UNIQUE(scope, idempotency_key)`` constraint, not a
read-then-write. A read-then-write loses the race it exists to prevent: two
callers both read "absent" and both insert. Here the loser takes an
``IntegrityError``, re-reads, and is served the winner's row — the same shape
``app/eval_event/service.py`` already uses for the crossing uniqueness boundary.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import (
    Channel,
    DispatchOutcome,
    DispatchResult,
    NotificationHandoff,
    ReasonCode,
)
from .correlation import new_correlation_id
from .models import (
    SCOPE_NOTIFICATION_DISPATCH,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    NotificationDispatchRequest,
)


class RequestHashConflict(Exception):
    """D-4 clause 4: the same key arrived with a materially different payload.

    Raised rather than served the stored result. Serving it would mean the
    caller believes its NEW content was dispatched when the ORIGINAL content
    was — a silent substitution, and exactly the failure a request hash exists
    to catch.
    """


class DispatchInProgress(Exception):
    """A concurrent caller holds this key and has not completed yet.

    Deliberately NOT a result: returning ``failed`` would be a lie (nothing
    failed), and returning ``dispatched`` would be a worse one. The caller
    retries, and the retry is safe because the key is unchanged.
    """


def request_key(*, eco_event_id: int, recipient_user_id: int) -> str:
    """D-4's request identity, rendered. The ONLY place it is constructed."""
    return f"eco:{int(eco_event_id)}:{int(recipient_user_id)}"


def request_hash(payload: Mapping[str, Any]) -> str:
    """sha256 over the canonical payload.

    ``sort_keys`` + fixed separators so two structurally equal payloads hash
    identically regardless of dict ordering, which would otherwise make a
    harmless key reordering look like a payload change.
    """
    blob = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _result_from_row(row: NotificationDispatchRequest) -> DispatchResult:
    """Rebuild the ORIGINAL result. Never recomputed, only replayed."""
    return DispatchResult(
        outcome=DispatchOutcome(row.outcome),
        channel=Channel(row.channel) if row.channel else None,
        reason_code=ReasonCode(row.reason_code),
        correlation_id=row.correlation_id,
        failure_detail=row.failure_detail,
    )


def _find(db: Session, key: str) -> Optional[NotificationDispatchRequest]:
    return db.execute(
        select(NotificationDispatchRequest).where(and_(
            NotificationDispatchRequest.scope == SCOPE_NOTIFICATION_DISPATCH,
            NotificationDispatchRequest.idempotency_key == key,
        ))
    ).scalars().first()


def begin(
    db: Session, handoff: NotificationHandoff,
) -> Tuple[NotificationDispatchRequest, Optional[DispatchResult]]:
    """Claim the key, or return the original result.

    Returns ``(row, replayed_result)``. When ``replayed_result`` is not None the
    caller must return it unchanged and MUST NOT dispatch: that is clauses 1–5
    of D-4 in one branch.

    Raises ``RequestHashConflict`` on a changed payload and
    ``DispatchInProgress`` when a concurrent caller holds the key.
    """
    key = handoff.idempotency_key or request_key(
        eco_event_id=handoff.eco_event_id,
        recipient_user_id=handoff.recipient_user_id)
    digest = request_hash(handoff.canonical_payload())

    existing = _find(db, key)
    if existing is None:
        row = NotificationDispatchRequest(
            scope=SCOPE_NOTIFICATION_DISPATCH,
            idempotency_key=key,
            organization_id=int(handoff.organization_id),
            eco_event_id=int(handoff.eco_event_id),
            recipient_user_id=int(handoff.recipient_user_id),
            request_hash=digest,
            # D-3: minted HERE, at acceptance, before any channel exists.
            correlation_id=new_correlation_id(),
            status=STATUS_IN_PROGRESS,
        )
        db.add(row)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # A concurrent caller won the uniqueness race. The database is the
            # authority; re-read and be served the winner rather than creating
            # a second logical dispatch.
            db.expunge(row)
            existing = _find(db, key)
            if existing is None:  # pragma: no cover - not our IntegrityError
                raise
        else:
            return row, None

    if existing.request_hash != digest:
        raise RequestHashConflict(
            f"idempotency key {key} was accepted with a different payload "
            f"(stored {existing.request_hash[:12]}…, got {digest[:12]}…)")

    if existing.status == STATUS_COMPLETED:
        return existing, _result_from_row(existing)

    raise DispatchInProgress(
        f"idempotency key {key} is held by a concurrent dispatch "
        f"(correlation {existing.correlation_id})")


def complete(
    db: Session, row: NotificationDispatchRequest, result: DispatchResult,
) -> DispatchResult:
    """Record the outcome and close the key.

    The stored ``correlation_id`` is authoritative and is NOT overwritten from
    the result: it was minted at acceptance, and D-3 makes it stable for the
    life of the handoff.
    """
    from datetime import datetime, timezone

    row.status = STATUS_COMPLETED
    row.channel = result.channel.value if result.channel else None
    row.outcome = result.outcome.value
    row.reason_code = result.reason_code.value
    row.failure_detail = result.failure_detail
    row.result_json = {
        "outcome": result.outcome.value,
        "channel": result.channel.value if result.channel else None,
        "reason_code": result.reason_code.value,
        "correlation_id": row.correlation_id,
        "failure_detail": result.failure_detail,
    }
    row.completed_at_utc = datetime.now(timezone.utc)
    db.flush()
    return _result_from_row(row)


__all__ = [
    "DispatchInProgress",
    "RequestHashConflict",
    "begin",
    "complete",
    "request_hash",
    "request_key",
]
