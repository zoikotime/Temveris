"""
The persisted day-scoped administrator alert.
=============================================

ZT-SPEC-ECO-002 v2.0 §24.3 / §33.1, owner ruling D-C: B.

ONE ROW PER (TENANT, WORKER, WORK DATE, DAY GENERATION, TYPE, AUDIENCE, MODE).

WHY THIS IS NOT ``eco_notification``
------------------------------------
``eco_notification``'s identity is week-shaped and event-anchored:

    uq_eco_notification_identity
      (organization_id, user_id, workweek_key, tier, mode, sequence,
       notification_type, audience)

with ``workweek_key``, ``tier`` and ``sequence`` all NOT NULL, plus a NOT NULL
foreign key to ``eco_threshold_event``. A day whose requirement is unresolved
produces **no threshold event** — EVAL-WEEK blocks the week and EVAL-EVENT
refuses anything that is not COMPLETE — so that table could only hold this
alert if a threshold event were fabricated to satisfy the FK, or if the day
identity were discarded in favour of a week. Ruling D-C: B forbids the first,
and the second would lose the very thing §24.3 is about.

So this is a separate table, and ``app/alert`` is untouched. That isolation is
also what keeps GV-07 unaffected: nothing here shares a row, a constraint or a
code path with the threshold-event notification.

THE GENERATION IS PART OF THE IDENTITY, DELIBERATELY
-----------------------------------------------------
``EcoDayRecord`` is generation-versioned — ``uq_eco_day_record_generation``
keys on ``(organization_id, user_id, work_date, evaluation_generation)``, and a
corrected day produces a NEW generation rather than editing the old one. The
alert pins the exact generation it was raised from, so:

* re-processing the same authoritative evaluation is idempotent, and
* a later corrected evaluation is a different fact, which this row neither
  claims nor silently follows.

WHY THE RECIPIENT IS STORED AS IDENTITY AND SNAPSHOT
-----------------------------------------------------
``recipient_user_id`` is nullable because D-1 requires an unresolved PRIMARY
ADMIN to be RECORDED rather than substituted. The row is still written when
nobody is designated: the absence is itself the evidence a Tenant needs, and
``recipient_outcome`` says which case this is. ``recipient_snapshot`` freezes
the event-time answer (D-3.7), so a later designation change cannot rewrite who
this alert concerned.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint, event, text,
)

from database.database import Base

from .append_only_ddl import (
    ATTEMPT_TABLE,
    DAY_ALERT_TABLE,
    append_only_statements,
    attempt_append_only_statements,
)
from .outcomes import AUTHORIZED_AUDIENCES, DayAlertType

_AUDIENCE_SQL = ", ".join("'%s'" % a for a in sorted(AUTHORIZED_AUDIENCES))
_TYPE_SQL = ", ".join("'%s'" % t.value for t in DayAlertType)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EcoDayAdministratorAlert(Base):
    """One immutable administrator alert about one authoritative day."""

    __tablename__ = DAY_ALERT_TABLE

    id = Column(Integer, primary_key=True)

    # ── the day this alert is about ─────────────────────────────────────────
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    user_id = Column(Integer, nullable=False, index=True)
    work_date = Column(Date, nullable=False, index=True)

    #: The authoritative record this alert was raised from. RESTRICT: a day
    #: record that an administrator was told about is not silently deletable.
    day_record_id = Column(
        Integer, ForeignKey("eco_day_record.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    #: Copied from the record, so the identity below is readable without a
    #: join and so a later generation is visibly a different fact.
    evaluation_generation = Column(Integer, nullable=False)

    #: The state that justified the alert, frozen. §24.3 attaches the alert to
    #: REQUIREMENT_UNRESOLVED alone, and recording the state makes that
    #: checkable years later rather than merely asserted.
    day_state = Column(String(32), nullable=False)

    # ── what, and for whom ──────────────────────────────────────────────────
    alert_type = Column(String(32), nullable=False)
    #: PRIMARY_ADMIN only (§33.1, ruling 4). The CHECK says so in the database.
    audience = Column(String(32), nullable=False)
    #: OPERATIONAL or SHADOW. Carried so a SHADOW alert can never be mistaken
    #: for an operational one, independently of anything that reads it.
    mode = Column(String(16), nullable=False, index=True)

    # ── recipient (identity, never an address) ──────────────────────────────
    #: NULL when no PRIMARY ADMIN is designated. D-1: recorded, not substituted.
    recipient_user_id = Column(Integer, nullable=True)
    #: RESOLVED or RECIPIENT_UNRESOLVED. Distinguishes "nobody was designated"
    #: from "somebody was" without inspecting the id for NULL, which would
    #: conflate an absent designation with a data fault.
    recipient_outcome = Column(String(32), nullable=False)
    #: The frozen event-time answer (D-3.7). Never re-resolved.
    recipient_snapshot = Column(JSON, nullable=False, default=dict)

    # ── content ─────────────────────────────────────────────────────────────
    #: Rendered at creation and frozen. Delivery sends THIS text; it does not
    #: re-render from current copy.
    rendered_body = Column(Text, nullable=False)
    #: Which implementation-copy id composed the body. NOT a §31 locked key —
    #: Ruling 3 = B — and named so a reviewer can tell the regimes apart.
    rendered_copy_id = Column(String(64), nullable=False)

    # ── audit ───────────────────────────────────────────────────────────────
    raised_at_utc = Column(DateTime(timezone=True), nullable=False,
                           default=_utcnow)
    raised_by = Column(String(64), nullable=False, default="ECO_DAY_ALERT")

    __table_args__ = (
        # The idempotency boundary. Re-processing one authoritative evaluation
        # cannot produce a second alert, and the database is what guarantees it
        # rather than an application-level check that a concurrent pass could
        # race past.
        UniqueConstraint(
            "organization_id", "user_id", "work_date", "evaluation_generation",
            "alert_type", "audience", "mode",
            name="uq_eco_day_administrator_alert_identity",
        ),
        Index("ix_eco_day_administrator_alert_pending",
              "organization_id", "mode"),
        CheckConstraint("audience IN (%s)" % _AUDIENCE_SQL,
                        name="ck_eco_day_administrator_alert_audience"),
        CheckConstraint("alert_type IN (%s)" % _TYPE_SQL,
                        name="ck_eco_day_administrator_alert_type"),
        # §24.3 attaches this alert to one state. A row asserting any other
        # state is unrepresentable rather than merely discouraged.
        CheckConstraint("day_state = 'REQUIREMENT_UNRESOLVED'",
                        name="ck_eco_day_administrator_alert_state"),
        CheckConstraint(
            "recipient_outcome IN ('RESOLVED', 'RECIPIENT_UNRESOLVED')",
            name="ck_eco_day_administrator_alert_recipient_outcome"),
        # D-1, in the database: an unresolved recipient has no id, and a
        # resolved one has one. Neither half can drift from the other.
        CheckConstraint(
            "(recipient_outcome = 'RESOLVED' AND recipient_user_id IS NOT NULL)"
            " OR (recipient_outcome = 'RECIPIENT_UNRESOLVED' AND "
            "recipient_user_id IS NULL)",
            name="ck_eco_day_administrator_alert_recipient_pair"),
        CheckConstraint("length(rendered_body) > 0",
                        name="ck_eco_day_administrator_alert_body_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<EcoDayAdministratorAlert org={self.organization_id} "
                f"user={self.user_id} {self.work_date} gen="
                f"{self.evaluation_generation} {self.alert_type} {self.mode}>")



class EcoDayAlertDeliveryAttempt(Base):
    """One append-only record of one dispatch of a day alert.

    Exists for the same reason ``eco_notification_delivery_attempt`` does, and
    for one more: it is what makes the dispatch pass's selection predicate
    DERIVABLE. "Which alerts have not been dispatched?" is answered by asking
    these rows, so no dispatch-pending flag is introduced and no second source
    of truth about what has been sent can arise.

    Separate from the notification attempt table because it hangs off a
    different parent. Its parent is an alert about a DAY; theirs is a
    notification about a WEEK, FK-bound to a threshold event that does not
    exist here.
    """

    __tablename__ = ATTEMPT_TABLE

    id = Column(Integer, primary_key=True)

    alert_id = Column(
        Integer, ForeignKey(f"{DAY_ALERT_TABLE}.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    attempt_seq = Column(Integer, nullable=False)

    #: The identity this attempt targeted. No address: under the handoff model
    #: ECO never learns one, and a value here could only be a re-resolution.
    recipient_user_id = Column(Integer, nullable=False)

    #: Everything below is the platform's answer, recorded verbatim. ECO
    #: interprets none of it and never re-derives one field from another.
    channel = Column(String(32), nullable=False)
    mode = Column(String(16), nullable=False, index=True)
    outcome = Column(String(16), nullable=False)
    reason_code = Column(String(48), nullable=True)
    correlation_id = Column(String(64), nullable=True)
    #: D-5.5: <= 512 characters, sanitised, no Worker PII, NULL permitted.
    failure_detail = Column(Text, nullable=True)

    attempted_at_utc = Column(DateTime(timezone=True), nullable=False,
                              default=_utcnow)

    __table_args__ = (
        UniqueConstraint("alert_id", "attempt_seq",
                         name="uq_eco_day_alert_attempt_seq"),
        CheckConstraint("attempt_seq >= 1",
                        name="ck_eco_day_alert_attempt_seq_positive"),
        # D-2's vocabulary. A third delivery state is unrepresentable.
        CheckConstraint("outcome IN ('dispatched', 'failed')",
                        name="ck_eco_day_alert_attempt_outcome"),
        CheckConstraint(
            "failure_detail IS NULL OR length(failure_detail) <= 512",
            name="ck_eco_day_alert_attempt_detail_len"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<EcoDayAlertDeliveryAttempt alert={self.alert_id} "
                f"attempt={self.attempt_seq} {self.outcome} {self.mode}>")


# ═════════════════════════════════════════════════════════════════════════════
# Database-level append-only enforcement — the authority (R5)
# ═════════════════════════════════════════════════════════════════════════════
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The Alembic
# revision emits the SAME statements explicitly, because `op.create_table`
# builds its own Table object and this listener does not fire for it.

@event.listens_for(EcoDayAdministratorAlert.__table__, "after_create")
def _install_append_only_guard(target, connection, **kw):
    """Emit the dialect's append-only statements, if the dialect has any."""
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


@event.listens_for(EcoDayAlertDeliveryAttempt.__table__, "after_create")
def _install_attempt_append_only_guard(target, connection, **kw):
    for statement in attempt_append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


__all__ = ["EcoDayAdministratorAlert", "EcoDayAlertDeliveryAttempt"]
