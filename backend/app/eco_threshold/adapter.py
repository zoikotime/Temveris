"""
Database-backed ``ThresholdProvider``.
======================================

Reads the ECO threshold out of the existing ``config_version`` /
``config_value`` tables. No table of its own, no migration, no second
versioning mechanism.

NOT WIRED
---------
No route, service or scheduler constructs this. There is no capture path yet —
who may set a threshold, with what authority and what audit record, is not
ruled. This class only reads whatever valid configuration already exists.

READ-ONLY, STRUCTURALLY
-----------------------
There is no capture, update or delete method here, and none is coming to this
class. Threshold configuration lives in the immutable
``ConfigVersion``/``ConfigValue`` mechanism, whose ORM listeners already refuse
to rewrite or delete a captured row.

VERSION SELECTION IS BORROWED, NOT REIMPLEMENTED
------------------------------------------------
``version_as_of`` delegates to ``DatabaseConfigVersionProvider``. The inclusive
lower bound, the latest-version-wins rule, the total ordering tiebreak and the
historical floor were all settled in Step 6 and are held there by a contract
suite. A second copy of that algorithm would be a second thing to keep correct.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_versioning.adapter import DatabaseConfigVersionProvider
from app.config_versioning.models import ConfigValue, ConfigVersion
from app.requirement_resolution.ports import ConfigVersionRef

from .keys import ECO_THRESHOLD_KEY
from .ports import ThresholdCandidate


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class DatabaseThresholdProvider:
    """Satisfies ``ThresholdProvider`` against the Step 6 tables.

    Every query filters on ``organization_id``. The composite foreign key
    already makes a cross-tenant row structurally impossible, but the filter is
    applied anyway — a tenant boundary resting on one mechanism has one way to
    fail.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        #: Composed, not subclassed: this provider answers a different question
        #: and must not become substitutable for the requirement one.
        self._versions = DatabaseConfigVersionProvider(session)

    # ── Version selection — Step 6's, unchanged ─────────────────────────────
    def version_as_of(
        self, *, organization_id: int, as_of: date
    ) -> Optional[ConfigVersionRef]:
        return self._versions.version_as_of(
            organization_id=organization_id, as_of=_as_date(as_of),
        )

    # ── The threshold rows in that version ──────────────────────────────────
    def threshold_candidates(
        self, *, organization_id: int, version: ConfigVersionRef
    ) -> Sequence[ThresholdCandidate]:
        """Rows bearing the canonical threshold key, at every scope and tier.

        THE QUERY DELIBERATELY DOES NOT FILTER ON ``tier`` OR ON SCOPE
        --------------------------------------------------------------
        It selects on organization, version and the canonical key, and stops
        there. A ``tier = 'ECO_THRESHOLD'`` predicate would make a mis-tiered
        threshold row invisible, and a ``scope_user_id IS NULL`` predicate would
        make a prohibited per-Worker row invisible — and both of those are
        defects the reader is required to report rather than silently skip. So
        the SQL observes; the reader decides.

        It does not read the ``ConfigVersion.values`` relationship, which would
        return every value in the version regardless of configuration domain.
        """
        row = self._session.execute(
            select(ConfigVersion).where(
                ConfigVersion.organization_id == organization_id,
                ConfigVersion.version_key == version.config_version_id,
            )
        ).scalars().first()
        if row is None:
            return ()

        values = self._session.execute(
            select(ConfigValue)
            .where(
                ConfigValue.config_version_id == row.id,
                ConfigValue.organization_id == organization_id,
                ConfigValue.key == ECO_THRESHOLD_KEY,
            )
            # Deterministic order. The reader refuses ambiguity rather than
            # picking a row, so order cannot change an outcome — but an
            # unordered read would still make the returned sequence itself
            # nondeterministic, and a defect report should be reproducible.
            # NOT ordered by rank: rank is meaningless for this domain and
            # ordering by it would suggest otherwise.
            .order_by(ConfigValue.tier, ConfigValue.scope_user_id, ConfigValue.id)
        ).scalars().all()

        out: List[ThresholdCandidate] = [
            ThresholdCandidate(
                key=v.key,
                tier=v.tier,
                scope_user_id=v.scope_user_id,
                value_minutes=v.value_minutes,
            )
            for v in values
        ]
        return tuple(out)


__all__ = ["DatabaseThresholdProvider"]
