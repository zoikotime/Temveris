"""
The single authoritative minute-rounding site.
==============================================

EVAL-DAY, under owner ruling R11.

§16.2 places exactly one rounding locus in the whole evaluation. This module is
it. Nothing else in the ECO path may convert seconds to minutes, and an
import-graph test pins that.

ROUND_HALF_UP, per R11:

    30 seconds -> 1 minute
    29 seconds -> 0 minutes
    90 seconds -> 2 minutes

Implemented as integer arithmetic, ``(seconds + 30) // 60``, deliberately:

* Python's built-in ``round`` is banker's rounding — ``round(0.5)`` is 0, which
  would make 30 seconds disappear. R11 forbids it explicitly.
* Float division introduces representation error that a wage figure should not
  inherit. ``Decimal`` would be correct but is heavier than needed for a
  non-negative integer count of seconds.

Every value entering here is a duration in whole seconds taken from the sealed
ledger, where a CHECK constraint already guarantees it is non-negative.
"""

from __future__ import annotations


def round_half_up_minutes(seconds: int) -> int:
    """Whole minutes from whole seconds, rounding halves upward.

    Raises on a negative input rather than silently flooring toward zero: a
    negative duration is not a rounding question, it is corrupt evidence, and
    the ledger's own constraints mean it should never reach here.
    """
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise TypeError(
            f"round_half_up_minutes requires whole seconds as an int, got "
            f"{seconds!r}; float arithmetic is not permitted at the rounding site"
        )
    if seconds < 0:
        raise ValueError(
            f"negative duration {seconds}s reached the rounding site; the sealed "
            f"ledger forbids it, so this indicates corrupt evidence upstream"
        )
    return (seconds + 30) // 60


__all__ = ["round_half_up_minutes"]
