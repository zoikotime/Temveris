"""
Channel selection, and the notification class each ECO type carries.
====================================================================

§34: channel selection is the platform's concern and MUST NOT be reimplemented
outside it. This module is where it happens, and ECO imports none of it.

WHY THERE IS EXACTLY ONE CHANNEL
--------------------------------
Because the repository has exactly one working transport. ``in_app``, ``push``
and ``sms`` appear in ``app/notifications/router.py`` as per-user preference
booleans with nothing behind them: no provider client, no send path, no
dependency. Returning any of them would be claiming a delivery that cannot
happen, so the selector cannot produce one.

Resend and SMTP are two PROVIDERS of the one EMAIL channel. Falling back from
the first to the second does not change the channel that was selected, and the
result still reports ``EMAIL``.

Adding a channel is a platform change confined to this module and an adapter.
"""

from __future__ import annotations

from typing import Optional

from app.notifications import classification as _classes

from .contracts import Channel, NotificationHandoff

#: ECO notification types this platform will dispatch, mapped to the
#: notification CLASS that governs their suppression.
#:
#: The administrator entries carry ``ECO_ADMIN_OPERATIONAL`` (architecture
#: decision D-B): §33.1 marks the PRIMARY ADMIN audience "Always", and that
#: class is absent from ``SUPPRESSIBLE`` and ``QUIET_HOURS_ELIGIBLE``, so it
#: cannot be dropped by preference policy.
#:
#: Worker entries carry the ordinary suppressible ``OPERATIONAL`` class,
#: because §34 is explicit that a Worker may have notifications disabled and
#: still hold the record: *"A Worker who has notifications disabled still has
#: the record, still has the explanation surface, still has the dispute path."*
_WORKER_AUDIENCE = "WORKER"

_ADMIN_AUDIENCES = frozenset({
    "PRIMARY_ADMIN",
    "DIRECT_MANAGER",
    "HR_ADMIN",
})


def notification_class_for(handoff: NotificationHandoff) -> Optional[str]:
    """The class governing this handoff, or None when the audience is unknown.

    None is a refusal, not a default: an unrecognised audience must fail the
    handoff rather than fall through to whichever class happens to be listed
    first.
    """
    audience = str(handoff.audience or "").upper()
    if audience == _WORKER_AUDIENCE:
        return _classes.OPERATIONAL
    if audience in _ADMIN_AUDIENCES:
        return _classes.ECO_ADMIN_OPERATIONAL
    return None


def select_channel(handoff: NotificationHandoff) -> Optional[Channel]:
    """Choose the channel, BEFORE any preference or suppression evaluation.

    D-2 fixes that order: channel selection -> preference/suppression ->
    transport -> DispatchResult. It matters beyond tidiness — it is what
    guarantees a channel exists by the time a suppressed result is built, which
    is in turn what lets ECO form its three-part record identity
    ``(threshold_event_id, recipient_id, channel)`` for a suppressed
    notification as well as a transmitted one.

    Returns None when nothing is eligible; the caller maps that to
    ``NO_ELIGIBLE_CHANNEL``, which is a FAILED outcome rather than suppression,
    because no policy declined anything — there was nothing to decline.
    """
    if notification_class_for(handoff) is None:
        return None
    # One channel today. The selection is trivial; the OWNERSHIP is the point.
    return Channel.EMAIL


__all__ = ["notification_class_for", "select_channel"]
