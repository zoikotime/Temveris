"""
Configuration-versioning failures, shared by every provider implementation.
===========================================================================

STEP 6 of the owner-authorised Phase 2 foundation.

These live in their own module because the in-memory reference provider and the
database-backed adapter must raise the SAME exception types. That is what lets
one contract test suite hold both to identical behaviour, rather than each
implementation being tested against whatever it happens to do.
"""

from __future__ import annotations


class ConfigVersionError(Exception):
    """Base for configuration-versioning write failures."""


class EffectiveDateCollision(ConfigVersionError):
    """Two versions would share an ``effective_from`` for one organization.

    Rejected rather than merged: if two versions claim the same start date,
    "which version governed that day" has no single answer, and §17
    reproducibility depends on it having exactly one.
    """


class ImmutableVersionError(ConfigVersionError):
    """An attempt to alter, reuse or remove a captured version.

    Configuration corrections add a later version. Rewriting an earlier one
    would silently change what a completed workweek resolves to — precisely
    what the §30.1 non-recalculation guarantee forbids.
    """


__all__ = [
    "ConfigVersionError",
    "EffectiveDateCollision",
    "ImmutableVersionError",
]
