"""
Database-backed ``ConfigVersionProvider`` — the Step 6 adapter.
===============================================================

STEP 6 of the owner-authorised Phase 2 foundation.

This is the implementation the Step 5 seam was defined for. It adapts to the
approved port; it does not redefine it. Where the storage layer made something
awkward, the awkwardness was absorbed here rather than pushed into the resolver
— the resolver's semantics were ruled on and are not renegotiated by a schema.

NOT WIRED
---------
No route, service or scheduler constructs this. It is instantiated by tests and
will be instantiated by the capture path in a later step.

IT NEVER READS ``app_settings``
-------------------------------
Not as a fallback, not for a historical date, not "just to bootstrap". A date
below the historical floor returns ``None``, and the resolver turns that into
``NO_VERSION``. Copying today's settings backwards would fabricate evidence
about a period nobody captured, and it would reintroduce the exact defect —
historical figures moving when an administrator edits a setting — that this
whole layer exists to remove. An import guard test enforces this.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.requirement_resolution.ports import (
    ConfigVersionRef,
    RequirementCandidate,
)
# The historical classification authority (owner ruling R8). One-way: nothing in
# ``worker_context`` imports this package, so wiring it here introduces no cycle
# and leaves that package's own contract untouched — it is read, never changed.
from app.worker_context import worker_context_as_of

from .errors import EffectiveDateCollision, ImmutableVersionError
from .models import ConfigValue, ConfigVersion, compute_content_hash


#: The ``config_value.key`` this adapter writes and reads for a day's REQUIREMENT.
#:
#: ``config_value`` is a general key/value store: one version can hold rows for
#: several configuration domains, distinguished only by this column. The Step 5
#: port, by contrast, is requirement-only by construction —
#: ``RequirementCandidate`` carries no key, so a row of any other domain that
#: reached the resolver would arrive indistinguishable from a requirement and
#: could win on ``rank``.
#:
#: So the read below filters on this constant. It is a named constant rather
#: than a literal at each site because the two sites must never drift: a write
#: under one key and a read under another would silently resolve every day to
#: ``NO_CANDIDATES``. Pinned here, in the adapter that owns the column, and
#: deliberately not a parameter — a caller that could widen the filter would
#: reintroduce exactly what the filter is for.
REQUIREMENT_KEY = "required_minutes"

#: The Worker classifications a value may be scoped to (owner ruling R2).
#:
#: Exactly two, product employment-type values carried verbatim. NOT legal
#: worker categories — D11 stays closed and nothing here translates between the
#: two. The database carries the same list as a CHECK constraint; this copy is
#: what lets the service refuse before the insert, with a sentence instead of an
#: IntegrityError.
#:
#: Unknown values are NEVER normalised onto one of these (R2). An unrecognised
#: classification is an unresolved one, and R3 makes that fail closed.
AUTHORIZED_WORKER_CLASSIFICATIONS: frozenset = frozenset({"full_time", "part_time"})


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class DatabaseConfigVersionProvider:
    """Reads immutable configuration versions from ``config_version`` /
    ``config_value``. Satisfies the Step 5 ``ConfigVersionProvider`` port.

    Every query filters on ``organization_id``. The composite foreign key makes
    a cross-tenant row structurally impossible, but the filter is applied anyway
    — a tenant boundary that depends on one mechanism has one way to fail.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Write side ──────────────────────────────────────────────────────────
    def capture_version(
        self,
        *,
        organization_id: int,
        config_version_id: str,
        effective_from: date | datetime,
        candidates: Optional[Sequence[RequirementCandidate]] = None,
        candidates_by_user: Optional[Dict[int, Sequence[RequirementCandidate]]] = None,
        candidates_by_classification: Optional[
            Dict[str, Sequence[RequirementCandidate]]] = None,
        captured_by_user_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> ConfigVersionRef:
        """Capture an immutable version and the values frozen into it.

        Refuses before touching the session when the version key is reused or
        the effective date is already claimed, so a rejected capture leaves the
        store byte-identical rather than leaving a partial row to be rolled
        back. The unique constraints remain as the net against a concurrent
        writer that passes these checks and then loses the race.
        """
        eff = _as_date(effective_from)

        clash = self._session.execute(
            select(ConfigVersion).where(
                ConfigVersion.organization_id == organization_id,
                ConfigVersion.version_key == config_version_id,
            )
        ).scalars().first()
        if clash is not None:
            raise ImmutableVersionError(
                f"version {config_version_id!r} already captured for "
                f"organization {organization_id}; versions are immutable"
            )

        occupied = self._session.execute(
            select(ConfigVersion).where(
                ConfigVersion.organization_id == organization_id,
                ConfigVersion.effective_from == eff,
            )
        ).scalars().first()
        if occupied is not None:
            raise EffectiveDateCollision(
                f"organization {organization_id} already has a version "
                f"effective {eff.isoformat()} ({occupied.version_key!r}); "
                f"two versions cannot start on the same date"
            )

        rows: List[dict] = []
        for candidate in candidates or ():
            rows.append(self._row(candidate, scope_user_id=None))
        for user_id, per_user in (candidates_by_user or {}).items():
            for candidate in per_user:
                rows.append(self._row(candidate, scope_user_id=user_id))
        for classification, per_class in (candidates_by_classification or {}).items():
            if classification not in AUTHORIZED_WORKER_CLASSIFICATIONS:
                # R2: refused, never normalised. Storing an unrecognised
                # classification would produce a row no evaluation can ever
                # select, which is a silent misconfiguration rather than a loud
                # one.
                raise ValueError(
                    f"{classification!r} is not an authorized Worker "
                    f"classification; expected one of "
                    f"{sorted(AUTHORIZED_WORKER_CLASSIFICATIONS)}"
                )
            for candidate in per_class:
                rows.append(self._row(
                    candidate, scope_user_id=None,
                    worker_classification=classification))

        version = ConfigVersion(
            organization_id=organization_id,
            version_key=config_version_id,
            effective_from=eff,
            captured_by_user_id=captured_by_user_id,
            note=note,
            content_hash=compute_content_hash(
                organization_id=organization_id,
                version_key=config_version_id,
                effective_from=eff,
                values=rows,
            ),
        )
        self._session.add(version)
        # Assign the surrogate id the value rows need, without committing: the
        # caller owns the transaction boundary.
        self._session.flush()

        for row in rows:
            self._session.add(
                ConfigValue(
                    config_version_id=version.id,
                    organization_id=organization_id,
                    **row,
                )
            )
        self._session.flush()

        return ConfigVersionRef(
            config_version_id=version.version_key,
            effective_from=version.effective_from,
        )

    @staticmethod
    def _row(
        candidate: RequirementCandidate,
        *,
        scope_user_id: Optional[int],
        worker_classification: Optional[str] = None,
    ) -> dict:
        """One candidate as a storable row. ``tier`` is carried verbatim."""
        return {
            "scope_user_id": scope_user_id,
            "worker_classification": worker_classification,
            "tier": candidate.tier,
            "rank": candidate.rank,
            "key": REQUIREMENT_KEY,
            "value_minutes": candidate.minutes,
            "source_ref": candidate.source_ref,
        }

    # ── Read side — the Step 5 port ─────────────────────────────────────────
    def version_as_of(
        self, *, organization_id: int, as_of: date
    ) -> Optional[ConfigVersionRef]:
        """The version effective on ``as_of``, or ``None`` below the floor.

        ``effective_from <= as_of`` is the inclusive lower bound; the newest
        such version wins. Ordered by ``effective_from`` then ``id`` so the
        answer is total even if a database somehow held two rows for one date
        (the unique constraint prevents it; the tiebreak means a violation
        cannot silently produce a nondeterministic answer).
        """
        on = _as_date(as_of)
        row = self._session.execute(
            select(ConfigVersion)
            .where(
                ConfigVersion.organization_id == organization_id,
                ConfigVersion.effective_from <= on,
            )
            .order_by(ConfigVersion.effective_from.desc(), ConfigVersion.id.desc())
            .limit(1)
        ).scalars().first()

        if row is None:
            # Historical floor. No fallback to live configuration — see module
            # docstring.
            return None
        return ConfigVersionRef(
            config_version_id=row.version_key,
            effective_from=row.effective_from,
        )

    def requirement_candidates(
        self,
        *,
        organization_id: int,
        user_id: int,
        as_of: date,
        version: ConfigVersionRef,
    ) -> Sequence[RequirementCandidate]:
        """Candidates frozen into ONE version. Never blended across versions.

        A Worker-scoped set replaces the organization-wide set outright rather
        than merging with it: merging would let a partial per-Worker override
        silently inherit ranks nobody reviewed together.

        WORKER CLASSIFICATION (GV-27, owner rulings R3/R8)
        --------------------------------------------------
        When the governing version carries classification-scoped rows, the
        answer depends on what the Worker WAS on ``as_of`` — not on what they
        are now. The classification comes from ``worker_context_as_of`` at the
        evaluation date, and an unresolved one returns NOTHING rather than
        falling through to the tenant fallback: R3 chose a refusal over a
        plausible figure derived from a classification nobody established.

        WHY THE GATE IS CONDITIONAL
        ---------------------------
        The classification check applies only when the version actually holds
        classification-scoped requirement rows. A version that does not is not
        made unanswerable by an absent timeline — the answer does not depend on
        classification, so an unknown one cannot make it wrong, which is the
        harm R3 names. Gating unconditionally would also refuse every day for
        every tenant that has never captured a timeline, including the already
        promoted GV-16 and GV-17 scenarios, which is a behaviour change no
        ruling asked for.
        """
        row = self._session.execute(
            select(ConfigVersion).where(
                ConfigVersion.organization_id == organization_id,
                ConfigVersion.version_key == version.config_version_id,
            )
        ).scalars().first()
        if row is None:
            return ()

        # A Worker-scoped set still wins outright, and R4 makes it
        # classification-free by construction: a row cannot carry both scopes.
        scoped = self._values(row.id, organization_id, scope_user_id=user_id)
        if scoped:
            return scoped

        classifications = self._classifications_present(row.id, organization_id)
        if not classifications:
            return self._values(row.id, organization_id, scope_user_id=None)

        resolution = worker_context_as_of(
            self._session,
            organization_id=organization_id,
            user_id=user_id,
            as_of=as_of,
        )
        if (not resolution.is_resolved
                or resolution.worker_context
                not in AUTHORIZED_WORKER_CLASSIFICATIONS):
            # R3: UNKNOWN_NO_HISTORY, UNKNOWN_BELOW_FLOOR, or a value outside
            # the authorized vocabulary. No candidates at all -> the resolver
            # reports REQUIREMENT_UNRESOLVED. Deliberately not the rank-6
            # fallback: see the docstring.
            return ()

        return self._values(
            row.id, organization_id, scope_user_id=None,
            worker_classification=resolution.worker_context,
        )

    def _classifications_present(self, version_pk: int, organization_id: int) -> bool:
        """Does this version scope any REQUIREMENT row to a classification?

        Asked before the Worker Context is consulted so a version that does not
        use the dimension costs no extra resolution and keeps its pre-existing
        behaviour exactly.
        """
        return self._session.execute(
            select(ConfigValue.id)
            .where(
                ConfigValue.config_version_id == version_pk,
                ConfigValue.organization_id == organization_id,
                ConfigValue.key == REQUIREMENT_KEY,
                ConfigValue.worker_classification.is_not(None),
            )
            .limit(1)
        ).scalars().first() is not None

    def _values(
        self,
        version_pk: int,
        organization_id: int,
        *,
        scope_user_id: Optional[int],
        worker_classification: Optional[str] = None,
    ) -> Sequence[RequirementCandidate]:
        predicate = (
            ConfigValue.scope_user_id.is_(None)
            if scope_user_id is None
            else ConfigValue.scope_user_id == scope_user_id
        )
        # Classification-INDEPENDENT rows always apply; the classification-
        # scoped ones that match join them, so a version can mix a tenant-wide
        # fallback with per-type defaults and the ladder still decides between
        # them on rank alone. R1 keeps §10.1 untouched: classification selects
        # which rows are in play, never what they outrank.
        classification_predicate = (
            ConfigValue.worker_classification.is_(None)
            if worker_classification is None
            else (ConfigValue.worker_classification.is_(None)
                  | (ConfigValue.worker_classification == worker_classification))
        )
        rows = self._session.execute(
            select(ConfigValue)
            .where(
                ConfigValue.config_version_id == version_pk,
                ConfigValue.organization_id == organization_id,
                # Requirement rows only. Without this the query returns every
                # value frozen into the version regardless of domain, and hands
                # them to a resolver whose type cannot tell them apart.
                ConfigValue.key == REQUIREMENT_KEY,
                predicate,
                classification_predicate,
            )
            # Deterministic order. The resolver sorts by rank regardless, but an
            # unordered read would make the returned sequence itself
            # nondeterministic, which INV-8 does not permit. Classification is
            # in the ordering too: two rows can now share (rank, tier, key) and
            # differ only by it, which without this would be an arbitrary tie.
            .order_by(ConfigValue.rank, ConfigValue.tier, ConfigValue.key,
                      ConfigValue.worker_classification)
        ).scalars().all()

        return tuple(
            RequirementCandidate(
                tier=r.tier,
                rank=r.rank,
                minutes=r.value_minutes,
                source_ref=r.source_ref,
            )
            for r in rows
        )

    # ── Introspection, for diagnostics and the shared contract suite ────────
    def earliest_version_date(self, *, organization_id: int) -> Optional[date]:
        return self._session.execute(
            select(ConfigVersion.effective_from)
            .where(ConfigVersion.organization_id == organization_id)
            .order_by(ConfigVersion.effective_from)
            .limit(1)
        ).scalars().first()

    def version_count(self, *, organization_id: int) -> int:
        return len(
            self._session.execute(
                select(ConfigVersion.id).where(
                    ConfigVersion.organization_id == organization_id
                )
            ).scalars().all()
        )


__all__ = ["DatabaseConfigVersionProvider"]
