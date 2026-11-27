"""
Leave-type IDENTITY — which of a tenant's types plays each structural role.
==========================================================================
Three leave rules need to know *which* type plays a particular role:

    R1  the SHARED-POOL MASTER  — whose entitlement sizes the pooled balance
    R2  the UNPAID type         — drives LWP payroll deduction and auto-LWP
    R3  the SICK type           — watched by the HR-review frequency heuristic

Before Phase 5 each rule answered by comparing against an English display name
in nine scattered places, which broke for any tenant that renamed or replaced a
type — and the product lets them (``POST /leave/admin/types`` creates custom
types, ``DELETE /leave/admin/types/{name}`` disables shipped ones).

THE DECISION (Phase 5)
----------------------
The role is EXPLICIT POLICY METADATA on the leave type, set by the
organization's own admin:

    pool_master     is_unpaid     is_sick

It is deliberately NOT inferred from the display name, the largest
entitlement, catalogue order, or "the first type with shared_pool" —
inference is what produced the defect. A tenant that calls its vacation bucket
"PTO" says so, and at most one ACTIVE type may claim each role
(``_assert_roles_unambiguous`` in leave/router.py rejects the rest with a 400).

RESOLUTION ORDER
----------------
1. An explicit flag on this organization's effective policy — authoritative.
2. Otherwise the shipped legacy name, so an organization that has not been
   migrated yet keeps behaving exactly as it does today.
   ``scripts/backfill_leave_roles.py`` stamps the flags for existing tenants;
   until it runs — or for a tenant whose catalogue it could not match — nothing
   changes.

The legacy fallback is a MIGRATION BRIDGE, not a second answer: once ANY type
in the catalogue holds a role, the name is never consulted for that role again.
Without that rule a migrated tenant would have two claimants — its own flagged
type and whatever still carries the shipped name.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# The names the shipped catalogue uses. They are the pre-Phase-5 answer, kept
# byte-identical to the comparisons they replaced so an unmigrated tenant is
# unaffected. They are not a claim that these names are universal.
LEGACY_POOL_MASTER = "Annual Leave"
LEGACY_UNPAID = "Unpaid Leave"
LEGACY_SICK = "Sick Leave"

ROLES = ("pool_master", "is_unpaid", "is_sick")


def _flag(policy: Optional[Dict[str, Any]], name: str) -> bool:
    return bool((policy or {}).get(name))


def _role_holder(policies: Optional[Dict[str, Dict[str, Any]]], flag: str):
    """The type flagged for this role in this organization, or None."""
    for name, pol in (policies or {}).items():
        if _flag(pol, flag):
            return name
    return None


def _resolve(leave_type: str, policy, policies, flag: str, legacy: str) -> bool:
    # 1. This type is explicitly flagged.
    if _flag(policy, flag):
        return True
    # 2. Some OTHER type holds the role → this one does not, whatever it is
    #    called. This is what stops a migrated tenant having two claimants.
    if policies is not None and _role_holder(policies, flag) is not None:
        return False
    # 3. Unmigrated tenant: the shipped name still decides.
    return leave_type == legacy


def is_pool_master(leave_type: str, policy=None, policies=None) -> bool:
    """Is this the type whose entitlement SIZES the shared pool? (R1)"""
    return _resolve(leave_type, policy, policies, "pool_master", LEGACY_POOL_MASTER)


def pool_master_name(policies: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """The pool master's type name for this organization's catalogue. (R1)"""
    return _role_holder(policies, "pool_master") or LEGACY_POOL_MASTER


def is_unpaid(leave_type: str, policy=None, policies=None) -> bool:
    """Does this type produce loss of pay? (R2)

    Payroll also honours the per-request ``is_lwp`` flag, which the leave module
    sets on AUTO-converted requests; this answers for a directly applied leave.
    """
    return _resolve(leave_type, policy, policies, "is_unpaid", LEGACY_UNPAID)


def unpaid_name(policies: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """The type name auto-LWP conversion should write. (R2)

    Writing a name absent from the tenant's own catalogue produces a request
    that appears in no balance view — which is what happened before the flag.
    """
    return _role_holder(policies, "is_unpaid") or LEGACY_UNPAID


def is_sick(leave_type: str, policy=None, policies=None) -> bool:
    """Is this the type the HR-review frequency heuristic watches? (R3)"""
    return _resolve(leave_type, policy, policies, "is_sick", LEGACY_SICK)


def sick_name(policies: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """This organization's sick-leave type. (R3)"""
    return _role_holder(policies, "is_sick") or LEGACY_SICK


def unconfigured_roles(policies: Optional[Dict[str, Dict[str, Any]]]) -> list:
    """Roles this organization has not explicitly assigned.

    The backfill reports these rather than inventing an answer: a tenant with
    no unpaid type in its catalogue needs an admin decision, not a guess.
    """
    return [f for f in ROLES if _role_holder(policies, f) is None]
