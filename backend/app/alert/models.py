"""
The immutable ECO notification record and its append-only attempt history.
==========================================================================

Owner ruling R5, Model B:

    immutable ECO notification record  +  append-only delivery attempts

The notification is a DOWNSTREAM ARTIFACT. ``eco_threshold_event`` remains the
authoritative, append-only source fact for the threshold lifecycle, and nothing
here ever writes to it.

THE IDENTITY, AND ITS TWO SEPARATE AXES
---------------------------------------
    logical episode (R3, UNCHANGED):
        (organization_id, user_id, workweek_key, tier, mode, sequence)

    physical notification identity (audience ruling, Option A):
        episode + notification_type + audience

``notification_type`` is PURPOSE. ``audience`` is WHO. They are deliberately
separate columns and neither is folded into the other: folding audience into
the type would inflate a §32-locked value vocabulary, and folding it into the
episode would make "how many audiences did we render for" change the identity
of the week it describes — which it cannot.

Each audience row is INDEPENDENTLY DURABLE: its own immutable body, its own
recipient snapshot, its own eligibility record, its own attempt history. That
is what makes owner ruling R1's guarantee structural rather than procedural —
a Worker's suppression acts on a Worker row's delivery attempts and cannot
reach an administrator row at all.

WORKER ROWS ONLY, TODAY
-----------------------
The ``audience`` column is open text so a future ADMINISTRATOR or REVIEWER
needs no migration. The SERVICE nevertheless refuses to create anything but
WORKER, because no authoritative reviewer/administrator recipient contract
exists (R2). The database permits the row; the service declines to invent the
recipient that would go in it. Both halves are asserted by test.

NO ``updated_at``
-----------------
There is no update. Delivery state is DERIVED from the attempt history, never
written back onto the notification.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, event, text,
)

from database.database import Base

from .append_only_ddl import (
    ATTEMPT_TABLE,
    NOTIFICATION_TABLE,
    append_only_statements,
    attempt_append_only_statements,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationImmutable(Exception):
    """An attempt to UPDATE or DELETE recorded ECO notification history.

    The record is append-only (R5). A retry appends a delivery attempt; it
    never edits the notification. A notification store that silently accepts a
    rewrite has stopped being evidence.
    """


class EcoNotification(Base):
    """One immutable ECO notification artifact, for ONE audience."""

    __tablename__ = NOTIFICATION_TABLE

    id = Column(Integer, primary_key=True, index=True)

    # ── logical episode identity (R3) — carried verbatim, never extended ────
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    workweek_key = Column(String(16), nullable=False, index=True)
    tier = Column(String(16), nullable=False)
    mode = Column(String(16), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)

    # ── the two separate axes ───────────────────────────────────────────────
    #: PURPOSE. Text, not a DB enum: §32 warns a value surface cannot be
    #: corrected later without a breaking change.
    notification_type = Column(String(32), nullable=False)
    #: WHO. Open text so a future audience needs no migration; the SERVICE,
    #: not the schema, is what restricts today's rows to WORKER.
    audience = Column(String(32), nullable=False, index=True)

    # ── the source fact ─────────────────────────────────────────────────────
    #: RESTRICT: the notification must never outlive the event that explains
    #: it, and the event may never be deleted out from under it.
    originating_event_id = Column(
        Integer, ForeignKey("eco_threshold_event.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    #: The event's own type, copied so the notification reconstructs without
    #: re-reading a row that may be far away in history.
    originating_event_type = Column(String(32), nullable=False)
    #: Provenance copied from the event (week evidence, retroactive lineage,
    #: threshold and total). Frozen: a later correction must not change what
    #: this notification says it was built from.
    event_provenance = Column(JSON, nullable=False, default=dict)

    # ── recipient (identity, never an address) ──────────────────────────────
    recipient_kind = Column(String(16), nullable=False)
    recipient_user_id = Column(Integer, nullable=False)
    #: The full frozen identity snapshot, copied from the event. Never
    #: re-resolved: the people a notification concerns are the people the event
    #: named, whatever has changed since.
    recipient_snapshot = Column(JSON, nullable=False, default=dict)

    # ── rendered content ────────────────────────────────────────────────────
    #: The final body, rendered at creation and frozen. Delivery, whenever it
    #: is built, sends THIS text — it does not re-render from current copy.
    rendered_body = Column(Text, nullable=False)
    #: Which LOCKED_COPY keys composed the body, so a reviewer can prove no
    #: locked string was paraphrased.
    rendered_copy_keys = Column(JSON, nullable=False, default=list)

    # ── eligibility ─────────────────────────────────────────────────────────
    eligibility_outcome = Column(String(32), nullable=False)
    eligibility_reason = Column(Text, nullable=True)
    #: The rollout state read at creation, so "why did this exist" is
    #: answerable without guessing what the control said at the time.
    rollout_state = Column(String(16), nullable=False)

    # ── audit ───────────────────────────────────────────────────────────────
    #: SHA-256 over the immutable content. Two runs at one identity either
    #: agree — and the existing row is returned — or they disagree and the
    #: service raises rather than silently keeping one of them.
    content_hash = Column(String(64), nullable=False)
    created_at_utc = Column(DateTime(timezone=True), nullable=False,
                            default=_utcnow)
    created_by = Column(String(64), nullable=False)

    __table_args__ = (
        # The audience ruling's physical identity: episode + purpose +
        # audience. Eight columns, and every one of them earns its place —
        # dropping audience would make a second audience a duplicate, and
        # dropping notification_type would make a withdrawal collide with the
        # crossing it closes (they share a sequence).
        UniqueConstraint(
            "organization_id", "user_id", "workweek_key", "tier", "mode",
            "sequence", "notification_type", "audience",
            name="uq_eco_notification_identity",
        ),
        # The episode read: "what has been notified for this Worker-week?"
        Index("ix_eco_notification_episode",
              "organization_id", "user_id", "workweek_key", "tier", "mode"),
        CheckConstraint("sequence >= 1",
                        name="ck_eco_notification_sequence_positive"),
        CheckConstraint("length(created_by) > 0",
                        name="ck_eco_notification_actor"),
        CheckConstraint("length(rendered_body) > 0",
                        name="ck_eco_notification_body_nonempty"),
        CheckConstraint("length(content_hash) = 64",
                        name="ck_eco_notification_content_hash"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<EcoNotification org={self.organization_id} user={self.user_id} "
            f"{self.workweek_key} {self.notification_type}/{self.audience} "
            f"seq={self.sequence} {self.mode}>"
        )


class EcoNotificationDeliveryAttempt(Base):
    """One append-only record of one delivery attempt.

    DELIBERATELY INERT IN THIS INCREMENT. The schema exists because R5 defines
    the target architecture and a second migration over the same domain would
    be worse; but NO application path writes to it, nothing is sent, and no
    retry policy exists. Its presence is what makes the notification row able
    to stay immutable: delivery state lives here, so nothing ever has to be
    written back onto the notification.
    """

    __tablename__ = ATTEMPT_TABLE

    id = Column(Integer, primary_key=True, index=True)

    #: Every attempt belongs to exactly ONE audience-specific notification.
    notification_id = Column(
        Integer, ForeignKey(f"{NOTIFICATION_TABLE}.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    attempt_seq = Column(Integer, nullable=False)

    #: Which identity this attempt targeted, and the address USED for it.
    #: The address is history, not identity — it is read at attempt time.
    recipient_kind = Column(String(16), nullable=False)
    recipient_user_id = Column(Integer, nullable=False)
    channel = Column(String(32), nullable=False)
    destination = Column(String(320), nullable=True)

    #: Carried so a SHADOW attempt can never be mistaken for an operational
    #: one, independently of the notification it belongs to.
    mode = Column(String(16), nullable=False, index=True)

    attempted_at_utc = Column(DateTime(timezone=True), nullable=False,
                              default=_utcnow)
    #: D-2: exactly two values. A SUPPRESSED notification is ``dispatched``
    #: carrying a suppression ``reason_code`` — never a third outcome.
    outcome = Column(String(32), nullable=False)

    #: The platform's closed reason code (D-5.5). Recorded because ``outcome``
    #: ALONE cannot answer "was this recipient actually informed?": under D-2 a
    #: suppressed and a transmitted notification both read ``dispatched``, and
    #: only this column distinguishes them. §34 requires that distinction to be
    #: checkable, so any reader answering that question MUST consult both.
    reason_code = Column(String(48), nullable=True)

    #: D-3: the platform's per-handoff correlation id, stable across platform
    #: retries. Its OWN column, and that is forced rather than chosen — D-3
    #: requires it on every completed dispatch including successes, while
    #: D-5.5 permits ``failure_detail`` to be NULL when there is no diagnostic.
    #: Both can hold only if they are separate columns. NEVER a provider id.
    correlation_id = Column(String(64), nullable=True)

    #: D-5.5: transport diagnostics only, <= 512 characters, no Worker PII,
    #: NULL permitted. The bound is a CHECK rather than a VARCHAR length: the
    #: column already shipped as TEXT, and narrowing a type is not an additive
    #: migration. A CHECK enforces the same bound and adds no rewrite.
    failure_detail = Column(Text, nullable=True)

    __table_args__ = (
        # Two concurrent workers cannot mint the same attempt number. Unlike
        # the EVAL-EVENT sequence, a lost race here is benign: an extra attempt
        # is legitimate, so the loser simply takes the next number.
        UniqueConstraint("notification_id", "attempt_seq",
                         name="uq_eco_notification_attempt_seq"),
        CheckConstraint("attempt_seq >= 1",
                        name="ck_eco_notification_attempt_seq_positive"),
        # D-2's vocabulary, in the database. A third ECO delivery state is
        # unrepresentable rather than merely discouraged.
        CheckConstraint("outcome IN ('dispatched', 'failed')",
                        name="ck_eco_notification_attempt_outcome"),
        CheckConstraint(
            "failure_detail IS NULL OR length(failure_detail) <= 512",
            name="ck_eco_notification_attempt_detail_len"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<EcoNotificationDeliveryAttempt notification={self.notification_id} "
            f"attempt={self.attempt_seq} {self.outcome} {self.mode}>"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Database-level append-only enforcement — the authority
# ═════════════════════════════════════════════════════════════════════════════
# Attached to the TABLES so `Base.metadata.create_all()` carries them. The
# Alembic revision emits the SAME statements explicitly, because
# `op.create_table` builds its own Table object and these listeners do not fire.

@event.listens_for(EcoNotification.__table__, "after_create")
def _install_notification_guard(target, connection, **kw):
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


@event.listens_for(EcoNotificationDeliveryAttempt.__table__, "after_create")
def _install_attempt_guard(target, connection, **kw):
    for statement in attempt_append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


# ═════════════════════════════════════════════════════════════════════════════
# ORM refusal — defence in depth, never the guarantee
# ═════════════════════════════════════════════════════════════════════════════

@event.listens_for(EcoNotification, "before_update", propagate=True)
def _reject_notification_update(mapper, connection, target):
    raise NotificationImmutable(
        f"ECO notification id={target.id} cannot be updated: the record is "
        f"immutable (R5). Delivery state belongs on a delivery attempt."
    )


@event.listens_for(EcoNotification, "before_delete", propagate=True)
def _reject_notification_delete(mapper, connection, target):
    raise NotificationImmutable(
        f"ECO notification id={target.id} cannot be deleted: the record is "
        f"compliance-sensitive history and is retained, never removed."
    )


@event.listens_for(EcoNotificationDeliveryAttempt, "before_update",
                   propagate=True)
def _reject_attempt_update(mapper, connection, target):
    raise NotificationImmutable(
        f"delivery attempt id={target.id} cannot be updated: attempts are "
        f"append-only (R5). A retry appends a NEW attempt."
    )


@event.listens_for(EcoNotificationDeliveryAttempt, "before_delete",
                   propagate=True)
def _reject_attempt_delete(mapper, connection, target):
    raise NotificationImmutable(
        f"delivery attempt id={target.id} cannot be deleted: the attempt "
        f"history is what makes a terminal failure durable."
    )


__all__ = [
    "EcoNotification",
    "EcoNotificationDeliveryAttempt",
    "NotificationImmutable",
    "NOTIFICATION_TABLE",
    "ATTEMPT_TABLE",
]
