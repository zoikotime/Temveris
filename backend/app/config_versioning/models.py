"""
Immutable, effective-dated configuration — SQLAlchemy models.
=============================================================

STEP 6 of the owner-authorised Phase 2 foundation.

These tables are ADDITIVE. ``app_settings`` is untouched and keeps its meaning:
"the configuration in force right now". These tables answer a different and, for
an evidentiary control, indispensable question — "what was the configuration in
force on that date?" — which ``app_settings`` structurally cannot, because its
rows are mutated in place and carry no history.

Registered on the shared ``database.database.Base`` metadata, following the
``app/commercial/models.py`` and ``app/work_ledger/models.py`` precedent.

NOT WIRED
---------
No route, service or scheduler reads or writes these tables. The configuration
capture path is a later step. Nothing in this module reads ``app_settings``, and
a test asserts that it never will: copying today's settings backwards to
manufacture history would fabricate evidence about periods nobody captured.

WHY IMMUTABILITY IS STRUCTURAL, NOT POLICY
------------------------------------------
There is no ``updated_at``, no ``is_active``, no ``is_deleted``. A row that can
be updated is not a version. A correction is a NEW version with a later
``effective_from``; the earlier version stays readable forever so a completed
week can always be re-derived to the same number (§17, §30.1, INV-8).

VOCABULARY
----------
Technical identifiers follow owner ruling D-09 Option C: ``organization_id`` and
the existing user model are canonical for columns, fields and event names.

``tier`` holds the §10.1 precedence label **verbatim** — ``WORKER_OVERRIDE``,
``SHIFT``, ``SCHEDULE``, ``EMPLOYMENT_POLICY``, ``TENANT_TYPE_DEFAULT``,
``TENANT_FALLBACK``. Per the owner ruling, those are specification-level labels
carried through unchanged; they are NOT renamed to ``ORG_*``, no replacement
tokens are invented, and no database enum is introduced (§32: enum values are a
product surface that cannot be corrected later without a breaking change).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, event, func, text,
    inspect as sa_inspect,
)
from sqlalchemy.orm import relationship

from database.database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Aware because §16.4 prohibits naive arithmetic."""
    return datetime.now(timezone.utc)


from .append_only_ddl import value_append_only_statements

class ImmutableConfigurationError(Exception):
    """Raised when a captured configuration row would be altered or removed.

    Configuration is corrected by capturing a LATER version, never by rewriting
    an earlier one. Rewriting would silently change what a completed workweek
    resolves to, which is the §30.1 guarantee this layer exists to hold.
    """


class ConfigVersion(Base):
    """One immutable configuration capture for one organization.

    EFFECTIVE DATING
    ----------------
    ``effective_from`` is an INCLUSIVE lower bound: a version effective on the
    1st governs the 1st. The version in force on a date is the latest whose
    ``effective_from <= date``.

    THE HISTORICAL FLOOR
    --------------------
    A date preceding the earliest captured version resolves to *no version*.
    That is deliberate and is the whole point of the table: the alternative —
    falling back to live configuration — would make a historical figure move
    every time an administrator edited a setting.

    NO MUTABLE STATE
    ----------------
    ``captured_at_utc`` (when it was recorded) is distinct from
    ``effective_from`` (when it takes effect), so a version captured late for an
    earlier date is representable without either field lying.
    """

    __tablename__ = "config_version"

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Stable public identifier. Crosses the Step 5 port as
    #: ``ConfigVersionRef.config_version_id``, so it must survive a database
    #: rebuild — which the surrogate integer ``id`` would not.
    version_key = Column(String(64), nullable=False)

    #: Inclusive lower bound. A DATE, not an instant: policy takes effect on a
    #: day in the organization's own calendar, and storing an instant would make
    #: the boundary depend on a timezone the row does not carry.
    effective_from = Column(Date, nullable=False)

    #: When the capture happened — audit, not semantics. Never used to select a
    #: version; two captures made in either order for the same effective date
    #: must resolve identically (INV-8).
    captured_at_utc = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    #: NULL for a system capture. SET NULL on user deletion: removing a person
    #: must never remove configuration history.
    captured_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    #: Durable actor attribution. Added by revision ``e5f8c2a91d64``.
    #:
    #: WHY A LABEL BESIDE THE FOREIGN KEY
    #: ``captured_by_user_id`` is `ondelete="SET NULL"` — deliberately, because
    #: removing a person must never remove configuration history. The
    #: consequence is that it stops answering "who set this" the moment that
    #: person is deleted, and a database-level SET NULL bypasses the ORM
    #: immutability listeners below entirely. For an evidentiary control, a
    #: configuration change that can become unattributable is a defect.
    #:
    #: So attribution is recorded twice, on purpose: the FK stays as the live
    #: link to a current user, and this column keeps a label that outlives them.
    #: Same pattern as ``rollout.RolloutControl.last_transition_by`` — "an id
    #: where one exists, SYSTEM otherwise".
    #:
    #: NOT NULL with a server default so the column can be added to a table
    #: that may already hold rows. ``SYSTEM`` is the explicit sentinel for a
    #: capture nobody performed by hand; it is never a silent fallback for an
    #: actor that should have been supplied.
    #:
    #: Deliberately NOT part of ``compute_content_hash``: it is provenance
    #: about the capture, not configuration. Including it would make an
    #: identical configuration hash differently depending on who recorded it —
    #: the same reason ``captured_at_utc`` and ``captured_by_user_id`` are
    #: excluded. It carries no personal data beyond an identifying label.
    captured_by_label = Column(
        String(64), nullable=False, server_default="SYSTEM", default="SYSTEM",
    )

    #: SHA-256 over the canonical serialisation of identity + values. Lets a
    #: historical figure be re-derived and proved to have used the same inputs
    #: (INV-8). See :func:`compute_content_hash`.
    content_hash = Column(String(64), nullable=False)

    #: Free-text administrator reason. Never parsed, never shown to a Worker.
    note = Column(Text, nullable=True)

    # Deliberately NOT ``delete-orphan``: detaching a value would delete it, and
    # deletion is refused by the listeners below. Save-update only, so the
    # relationship can compose a version without implying it can dismantle one.
    values = relationship(
        "ConfigValue",
        back_populates="version",
        cascade="save-update, merge",
        order_by="ConfigValue.rank, ConfigValue.tier, ConfigValue.key",
    )

    __table_args__ = (
        # Two versions cannot claim the same start date: "which version governed
        # that day" must have exactly one answer.
        UniqueConstraint(
            "organization_id", "effective_from",
            name="uq_config_version_org_effective_from",
        ),
        UniqueConstraint(
            "organization_id", "version_key",
            name="uq_config_version_org_version_key",
        ),
        # The target of ConfigValue's composite tenant FK. Redundant as a key
        # (``id`` alone is unique) and declared solely so a value row cannot be
        # attached to another organization's version.
        UniqueConstraint("id", "organization_id", name="uq_config_version_id_org"),
        # The hot read: "latest version at or before this date, for this org".
        Index(
            "ix_config_version_org_effective",
            "organization_id", "effective_from",
        ),
        CheckConstraint("length(version_key) > 0", name="ck_config_version_key_nonempty"),
        CheckConstraint("length(content_hash) > 0", name="ck_config_version_hash_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ConfigVersion org={self.organization_id} key={self.version_key!r} "
            f"from={self.effective_from}>"
        )


class ConfigValue(Base):
    """One tier's configured value, frozen into exactly one version.

    A value belongs to one version and is never shared, inherited or blended
    across versions. Resolution reads the values of a single version; there is
    no query in this design that can span two.

    ``value_minutes`` is NULL when a tier is configured but supplies no value —
    which is different from supplying an invalid one. The Step 5 resolver treats
    the first as "this rank did not answer, consult the next" and the second as
    a recorded configuration defect.
    """

    __tablename__ = "config_value"

    id = Column(Integer, primary_key=True, index=True)

    config_version_id = Column(Integer, nullable=False, index=True)

    #: Denormalised for tenant-scoped queries, and load-bearing: it is half of
    #: the composite FK below, which is what makes cross-tenant attachment
    #: structurally impossible rather than merely tested for.
    organization_id = Column(Integer, nullable=False, index=True)

    #: NULL = organization-wide. A non-NULL row applies to that Worker only.
    #:
    #: RESTRICT (owner ruling extending the Worker-history retention boundary
    #: to this column). Three facts make it the only admissible action:
    #:
    #: * this row is immutable, and ``scope_user_id`` is in
    #:   ``_IMMUTABLE_VALUE_FIELDS``;
    #: * ``scope_user_id`` is inside :func:`compute_content_hash`, and the
    #:   capture services verify that the stored digest describes the stored
    #:   values — so removing or altering one leaves the parent version's hash
    #:   describing a value set that no longer exists;
    #: * a database-level action bypasses the ORM listeners entirely, so the
    #:   refusal has to live in the schema.
    #:
    #: SET NULL is PROHIBITED and is not the same choice as
    #: ``ConfigVersion.captured_by_user_id``, which is SET NULL: there, NULL
    #: means "a system capture" and the column is excluded from the digest.
    #: Here NULL means ORGANIZATION-WIDE, so nulling a Worker's override would
    #: silently re-scope it to the whole tenant — at this row's own tier and
    #: rank, which for ``WORKER_OVERRIDE`` is rank 0 and outranks everything.
    #: It would also collide with ``uq_config_value_scope`` whenever an
    #: organization-wide row already exists for the same version, tier and key.
    #:
    #: Removing a value remains what the delete listener already says it is:
    #: capture a LATER version that omits it. Nothing here is ever erased.
    scope_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True,
    )

    #: The Worker classification this value applies to, or NULL.
    #:
    #: Owner ruling R1/R4 (GV-27): classification is an explicit DIMENSION of a
    #: versioned value, never a token folded into ``tier``. §10.1's ladder is
    #: unchanged — rank 5 already means "Tenant Worker-type default", and this
    #: column is what finally lets one version carry both of that tier's answers
    #: at once.
    #:
    #: NULL means "not classification-scoped": the value applies whatever the
    #: Worker was. That is the pre-existing meaning of every row captured before
    #: this column existed, so no backfill is required and none was performed.
    #:
    #: Values are the product employment-type vocabulary carried VERBATIM
    #: (``worker_context.VOCABULARY_PRODUCT_EMPLOYMENT_TYPE``) — never a legal
    #: worker category. D11 stays closed: nothing here translates one into the
    #: other.
    worker_classification = Column(String(64), nullable=True)

    #: §10.1 precedence label, verbatim. Not an enum — see the module docstring.
    tier = Column(String(64), nullable=False)

    #: Precedence order, lowest wins. Held as data rather than derived from
    #: ``tier`` so a version records the precedence that was actually in force.
    rank = Column(Integer, nullable=False)

    key = Column(String(128), nullable=False)

    value_minutes = Column(Integer, nullable=True)

    #: Provenance for the explanation right (INV-10) — where this value came
    #: from, so a decision can be explained rather than merely asserted.
    source_ref = Column(String(255), nullable=True)

    # No ``created_at`` here, deliberately. A value is captured with its version
    # and cannot outlive or predate it, so ``ConfigVersion.captured_at_utc``
    # already answers "when was this recorded". A second timestamp would be
    # redundant data on an immutable row, and two timestamps that could ever
    # disagree is worse than one that cannot.

    version = relationship("ConfigVersion", back_populates="values")

    __table_args__ = (
        # Composite tenant FK. RESTRICT: a version with values cannot be deleted
        # out from under them, because configuration history is not disposable.
        ForeignKeyConstraint(
            ["config_version_id", "organization_id"],
            ["config_version.id", "config_version.organization_id"],
            name="fk_config_value_version_org",
            ondelete="RESTRICT",
        ),
        # Uniqueness is NOT declared here. A plain UniqueConstraint over
        # (config_version_id, scope_user_id, tier, key) does not enforce the
        # rule that matters: BOTH PostgreSQL and SQLite treat NULLs as distinct
        # in a unique index, so two organization-wide rows for the same
        # tier/key would both be storable. The real rule is the COALESCE
        # expression index declared immediately after this class.
        Index("ix_config_value_version_scope", "config_version_id", "scope_user_id"),
        # Mirrors the Step 5 resolver bounds so an out-of-range value cannot be
        # stored in the first place, rather than being caught only at read time.
        CheckConstraint(
            "value_minutes IS NULL OR (value_minutes >= 1 AND value_minutes <= 1440)",
            name="ck_config_value_minutes_range",
        ),
        CheckConstraint("rank >= 0", name="ck_config_value_rank_nonnegative"),
        CheckConstraint("length(tier) > 0", name="ck_config_value_tier_nonempty"),
        # R4: the two scopes are MUTUALLY EXCLUSIVE. A row scoped to both a
        # Worker and a classification would mean "this Worker, but only while
        # part-time", and the ruling declines to invent precedence between the
        # two. Declared in the schema because the ORM is not the only writer.
        CheckConstraint(
            "scope_user_id IS NULL OR worker_classification IS NULL",
            name="ck_config_value_scope_exclusive",
        ),
        # R2: exactly two authorized values, or NULL. Held in the schema rather
        # than only in the service because an unrecognised classification must
        # be unstorable, not merely unselected — R3 makes an unknown
        # classification fail closed, and a typo that reached the table would
        # fail every evaluation for that Worker silently.
        CheckConstraint(
            "worker_classification IS NULL OR "
            "worker_classification IN ('full_time', 'part_time')",
            name="ck_config_value_classification_vocabulary",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ConfigValue v={self.config_version_id} {self.tier}/{self.key} "
            f"rank={self.rank} minutes={self.value_minutes}>"
        )


#: The authoritative uniqueness rule for configuration values.
#:
#: Declared as a functional index rather than a UniqueConstraint because the
#: scope column is nullable and NULL never equals NULL in a unique index — on
#: PostgreSQL *and* on SQLite. Without the COALESCE, two organization-wide rows
#: for the same tier and key would both be storable, and "what did this tier
#: say" would have two answers for one version.
#:
#: COALESCE maps NULL scope onto 0, so this assumes no user carries id 0 — the
#: same assumption the existing ``uq_app_settings_scope`` index makes, and true
#: of every autoincrement identity in this schema.
#:
#: Declared here (not only in the migration) so a database built by
#: ``create_all`` — which is how the test suite and the bootstrap path build
#: one — gets the same rule as a database built by Alembic. The migration
#: creates the identically named index with ``IF NOT EXISTS``, so applying it
#: over a create_all database is a no-op rather than a conflict.
#: R5 added ``worker_classification`` to the identity. Before it, one version
#: could hold only ONE answer per (scope, tier, key) — which is precisely why
#: GV-27 could not be expressed: ``EMPLOYMENT_POLICY + part_time -> 240`` and
#: ``EMPLOYMENT_POLICY + full_time -> 525`` collided. With classification in the
#: key they are distinct rows, and "what did this tier say" still has exactly
#: one answer once you also say *for whom*.
#:
#: COALESCE on the new column too, and to ``''`` rather than ``0`` because the
#: column is text: without it two classification-independent rows for the same
#: tier/key would both be storable again, reopening the hole the original index
#: was written to close.
Index(
    "uq_config_value_scope",
    ConfigValue.__table__.c.config_version_id,
    func.coalesce(ConfigValue.__table__.c.scope_user_id, 0),
    func.coalesce(ConfigValue.__table__.c.worker_classification, ""),
    ConfigValue.__table__.c.tier,
    ConfigValue.__table__.c.key,
    unique=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# Content hashing — INV-8 reproducibility
# ══════════════════════════════════════════════════════════════════════════════
def compute_content_hash(
    *,
    organization_id: int,
    version_key: str,
    effective_from: date,
    values: Iterable[Mapping[str, Any]],
) -> str:
    """SHA-256 over a canonical serialisation of a version's identity + values.

    Deterministic across processes and platforms:

    * values are sorted by ``(scope_user_id, tier, rank, key)``, with NULL scope
      ordered first via a sentinel, so the order rows were supplied in cannot
      change the digest;
    * JSON is emitted with sorted keys, no insignificant whitespace, and
      ``ensure_ascii`` so the byte sequence does not depend on the platform's
      default encoding;
    * ``id``, ``captured_at_utc`` and ``captured_by_user_id`` are excluded —
      they are provenance about the capture, not the configuration, and
      including them would make an identical configuration hash differently
      depending on when it was recorded.

    Identity is inside the digest so a value set cannot be moved to a different
    version or organization and still verify.
    """
    canonical_values = sorted(
        (
            {
                "scope_user_id": v.get("scope_user_id"),
                # R6: classification participates in the digest, serialised as
                # ``null`` when the row is classification-independent. Two
                # versions that differ only in which classification a value
                # applies to are different configurations, and a digest that
                # could not tell them apart would let one be substituted for
                # the other.
                "worker_classification": v.get("worker_classification"),
                "tier": v["tier"],
                "rank": int(v["rank"]),
                "key": v["key"],
                "value_minutes": v.get("value_minutes"),
                "source_ref": v.get("source_ref"),
            }
            for v in values
        ),
        # -1 sorts organization-wide rows ahead of any real user id, and keeps
        # the sort total (None is not comparable with int).
        #
        # Classification is in the SORT KEY as well as the payload, and must be:
        # since R5 two rows can now agree on (scope, tier, rank, key) and differ
        # only by classification, so without it the ordering would be a tie the
        # sort resolves arbitrarily — and the digest would stop being
        # deterministic, which INV-8 does not permit. "" sorts ahead of any real
        # classification, matching the -1 convention above.
        key=lambda v: (
            v["scope_user_id"] if v["scope_user_id"] is not None else -1,
            v["worker_classification"] or "",
            v["tier"], v["rank"], v["key"],
        ),
    )
    payload = {
        "organization_id": int(organization_id),
        "version_key": version_key,
        "effective_from": effective_from.isoformat(),
        "values": canonical_values,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Immutability — enforced at the ORM layer, per the owner's Step 2 precedent
# ══════════════════════════════════════════════════════════════════════════════
# Same approach as work_ledger provenance: an attribute-level listener that
# fails where the mutation happens, plus flush-time listeners as the net. No
# database trigger — the owner ruled ORM-level enforcement sufficient at this
# stage, and the same documented limitation applies: a Core-level bulk UPDATE or
# DELETE that bypasses the ORM entirely is not intercepted here.
#
# ``captured_at_utc``/``content_hash`` are included in the frozen set because a
# version whose hash can be rewritten cannot serve as an integrity check.

_IMMUTABLE_VERSION_FIELDS = frozenset({
    "organization_id", "version_key", "effective_from",
    "captured_at_utc", "captured_by_user_id", "content_hash",
    # Attribution that can be rewritten after the fact is not attribution.
    "captured_by_label",
})

_IMMUTABLE_VALUE_FIELDS = frozenset({
    "config_version_id", "organization_id", "scope_user_id",
    "tier", "rank", "key", "value_minutes", "source_ref",
})


def _reject_field_mutation(target, field: str, entity: str) -> None:
    """Raise if ``field`` has a recorded change on a persisted instance."""
    state = sa_inspect(target)
    history = state.attrs[field].history
    if history.has_changes() and history.deleted:
        old = history.deleted[0]
        new = history.added[0] if history.added else None
        if old != new:
            raise ImmutableConfigurationError(
                f"{entity}.{field} is immutable: flush would change {old!r} -> "
                f"{new!r}. Correct configuration by capturing a LATER version, "
                f"never by rewriting an earlier one."
            )


@event.listens_for(ConfigVersion, "before_update", propagate=True)
def _reject_config_version_update(mapper, connection, target):
    for field in sorted(_IMMUTABLE_VERSION_FIELDS):
        _reject_field_mutation(target, field, "ConfigVersion")


@event.listens_for(ConfigValue, "before_update", propagate=True)
def _reject_config_value_update(mapper, connection, target):
    for field in sorted(_IMMUTABLE_VALUE_FIELDS):
        _reject_field_mutation(target, field, "ConfigValue")


@event.listens_for(ConfigVersion, "before_delete", propagate=True)
def _reject_config_version_delete(mapper, connection, target):
    raise ImmutableConfigurationError(
        f"ConfigVersion id={target.id} cannot be deleted. Configuration history "
        f"is permanent; supersede it with a later version instead."
    )


@event.listens_for(ConfigValue, "before_delete", propagate=True)
def _reject_config_value_delete(mapper, connection, target):
    raise ImmutableConfigurationError(
        f"ConfigValue id={target.id} cannot be deleted. Capture a later version "
        f"that omits or replaces this value instead."
    )



# ===========================================================================
# Database-level append-only enforcement -- the authority (Owner Decision 1A)
# ===========================================================================
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The
# Alembic revision emits the SAME statements, because `op.create_table`
# builds its own Table object and this listener does not fire for it.
#
# The ORM guard above stays exactly as it was. It is not redundant: it
# raises a typed, named exception the application can reason about, where
# the trigger raises a database error. The trigger is the backstop for the
# statements the ORM never sees (Ruling M / D18-F1).

# ``ConfigVersion`` deliberately gets NO trigger: ``note`` is writable by
# design, and ``captured_by_user_id`` is SET NULL, so deleting a user UPDATES
# this table from the foreign key itself. See ``append_only_ddl.py``.
@event.listens_for(ConfigValue.__table__, "after_create")
def _install_value_append_only_guard(target, connection, **kw):
    for statement in value_append_only_statements(
            connection.dialect.name):
        connection.execute(text(statement))

__all__ = [
    "ConfigVersion",
    "ConfigValue",
    "ImmutableConfigurationError",
    "compute_content_hash",
]
