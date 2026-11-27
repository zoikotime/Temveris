"""Opaque, prefixed identifiers for the offboarding domain.

Every primary key is a UUID rendered as a prefixed hex string (e.g.
``case_3f9c...``) so logs, audit rows, evidence packages and admin URLs can tell
what kind of object an ID refers to without a DB lookup — and so an ID leaking
into a client never reveals an enumerable integer (the "opaque IDs" requirement
in §7). The same convention is used by ``app.commercial.ids``; we keep a parallel
copy here so the offboarding module has no import-time dependency on the
commercial layer.

Lengths stay ≤ 40 chars so they fit the ``String(40)`` primary keys in
``models.py`` (``len(prefix) + 1 + 32``).
"""
from __future__ import annotations

import secrets
import uuid


def _new(prefix: str) -> str:
    # 16 random bytes → 32 hex chars; prefix kept short so total ≤ 40.
    return f"{prefix}_{secrets.token_hex(16)}"


def case_id() -> str:            return _new("case")
def task_id() -> str:            return _new("task")
def revocation_id() -> str:      return _new("rev")
def former_access_id() -> str:   return _new("fwa")
def retention_id() -> str:       return _new("ret")
def legal_hold_id() -> str:      return _new("hold")
def certificate_id() -> str:     return _new("cert")
def evidence_id() -> str:        return _new("evd")
def audit_id() -> str:           return _new("aud")
def event_id() -> str:           return _new("evt")
def blocker_id() -> str:         return _new("blk")
def waiver_id() -> str:          return _new("wvr")
def reconciliation_id() -> str:  return _new("rcn")
def incident_id() -> str:        return _new("inc")
def quarantine_id() -> str:      return _new("tqr")
def portal_session_id() -> str:  return _new("psn")
def mfa_id() -> str:             return _new("mfa")
def portal_log_id() -> str:      return _new("pal")
def policy_id() -> str:          return _new("pol")
def deletion_review_id() -> str: return _new("der")
def support_ticket_id() -> str:  return _new("tic")
def dsar_id() -> str:            return _new("drq")


def incident_reference() -> str:
    """Short, human-quotable incident reference (e.g. 'INC-9F3A2B'). Distinct
    from the opaque primary key so on-call can read it over the phone."""
    return f"INC-{secrets.token_hex(4).upper()}"


# Correlation / causation identifiers used by the event envelope. These are not
# persisted as primary keys, so they use a full uuid4 hex (still < 40 chars).
def correlation_id() -> str:     return _new("cor")
def causation_id() -> str:       return _new("cau")
def idempotency_key() -> str:    return uuid.uuid4().hex
