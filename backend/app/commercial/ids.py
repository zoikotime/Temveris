"""Stable, prefixed entity IDs.

We use prefixed UUIDv4 strings (e.g. "org_<hex>") so logs, audit records,
support tickets, and admin URLs can always tell what kind of object an ID
refers to without doing a DB lookup. Length kept under 40 chars so the
String(40) PKs in models.py fit.
"""

from __future__ import annotations

import secrets


def _new(prefix: str) -> str:
    # 16 random bytes → 32 hex chars; total length len(prefix)+1+32 ≤ 40.
    return f"{prefix}_{secrets.token_hex(16)}"


def org_id() -> str:        return _new("org")
def workspace_id() -> str:  return _new("wsp")
def signup_id() -> str:     return _new("sgn")
def event_id() -> str:      return _new("evt")
def subscription_id() -> str: return _new("sub")
def invoice_id() -> str:    return _new("inv")
def ledger_entry_id() -> str: return _new("led")
def checkout_id() -> str:   return _new("chk")
def quote_id() -> str:      return _new("qot")
def correlation_id() -> str: return _new("cor")
def case_id() -> str:       return _new("case")
def lead_id() -> str:       return _new("lead")
def trace_id() -> str:      return _new("trc")
