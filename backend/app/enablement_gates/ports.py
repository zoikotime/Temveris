"""
The one thing this package needs from outside, stated as a value.
==================================================================

WHY A PORT AND NOT AN IMPORT
----------------------------
The checker used to ``from app.rollout import get_status``. That made the edge
point the wrong way: ``rollout`` is the thing being GATED, and a gate that
imports what it gates cannot then be composed by it without a cycle. Owner
ruling D6 puts the gate in front of the transition, so the dependency has to
run ``rollout -> enablement_gates``, one way.

Inverting it costs one dataclass. The checker declares the shape of the fact it
needs -- when a shadow period started, and how long one has to be -- and
whoever holds that fact supplies it. ``app.rollout`` builds one from its
control row; a test builds one directly; nothing else can reach in.

WHY IT IS NOT A PROTOCOL
------------------------
A ``Protocol`` would accept a live ``RolloutStatus`` and let the checker walk
whatever else that object carries. A frozen dataclass accepts exactly four
facts and nothing more, so the coupling cannot widen by accident later.

WHAT IS DELIBERATELY ABSENT
---------------------------
No way to CHANGE a rollout state. This package reads and reports; it does not
enable anything, and a port that could write would make it able to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ShadowObservation:
    """What §44.2's "two complete workweeks" is measured from.

    ``exists`` is False when the Tenant has no rollout control at all, which is
    a different fact from "has one, still OFF" and is reported as such.

    ``min_observation_days`` travels WITH the observation rather than being
    restated here, so the gate and the transition rule enforce one number. A
    checker with its own hard-coded 14 would drift silently the day the control
    row's value changed.
    """

    exists: bool
    state: str
    min_observation_days: float
    shadow_entered_at: Optional[datetime] = None

    def days_elapsed(self, *, now: datetime) -> Optional[float]:
        """Calendar days observed, or None when observation never started."""
        if self.shadow_entered_at is None:
            return None
        started = self.shadow_entered_at
        if started.tzinfo is None:
            # SQLite drops tzinfo on the way back out. A naive timestamp here
            # is a UTC one that lost its label, never a local one.
            started = started.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - started).total_seconds() / 86400.0

    @classmethod
    def absent(cls) -> "ShadowObservation":
        """No rollout control exists. Named, so a caller cannot express it by
        passing zeroes that read like a real observation of length nothing."""
        return cls(exists=False, state="OFF", min_observation_days=0.0)


__all__ = ["ShadowObservation"]
