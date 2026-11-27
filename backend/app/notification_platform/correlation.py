"""
Platform correlation identifiers (owner ruling D-3).
====================================================

FORMAT
------
``ndc_<32 lowercase hex>`` — 36 characters, fixed. ``MAX_CORRELATION_ID_CHARS``
is the bound the storage column and the diagnostic budget are sized against.

The shape follows the repository's existing prefixed-id convention
(``app/commercial/ids.py``: ``f"{prefix}_{secrets.token_hex(16)}"``) so a
correlation id is recognisable at a glance in a log line. The prefix is ``ndc``
— notification dispatch correlation — rather than that module's generic ``cor``,
because these two id spaces answer to different owners and must not be
interchangeable.

WHAT IT IS NOT
--------------
**Not a provider message id.** D-3 forbids it, and the reason is not stylistic:
a provider id belongs to one transport attempt, and D-3 requires one identifier
per HANDOFF that survives every internal retry. The two cannot be the same
value. Resend returns an id of its own; the adapter keeps it below the boundary
and it never becomes this.

**Not derived from anything about the recipient.** It is random, so it cannot
carry Worker PII (D-5.5) and cannot be reversed into an identity.

GENERATION POINT
----------------
At handoff ACCEPTANCE, before channel selection. That ordering matters: a
handoff that never reaches a channel — rejected as invalid, or finding no
eligible channel — still has a correlation id, so every ``DispatchResult`` can
carry one and ECO never has to store a NULL.
"""

from __future__ import annotations

import secrets

#: ``ndc_`` + 32 hex characters.
CORRELATION_ID_PREFIX = "ndc"
MAX_CORRELATION_ID_CHARS = 36


def new_correlation_id() -> str:
    """One correlation id. Platform-generated, opaque, PII-free."""
    return f"{CORRELATION_ID_PREFIX}_{secrets.token_hex(16)}"


def is_platform_correlation_id(value: str) -> bool:
    """Whether ``value`` was minted here.

    Used by the guards that prove a provider id never reaches ECO as a
    correlation id — the check is cheap, and it turns D-3's prohibition into
    something assertable rather than merely documented.
    """
    if not isinstance(value, str) or len(value) != MAX_CORRELATION_ID_CHARS:
        return False
    prefix, _, body = value.partition("_")
    if prefix != CORRELATION_ID_PREFIX or len(body) != 32:
        return False
    return all(c in "0123456789abcdef" for c in body)


__all__ = [
    "CORRELATION_ID_PREFIX",
    "MAX_CORRELATION_ID_CHARS",
    "is_platform_correlation_id",
    "new_correlation_id",
]
