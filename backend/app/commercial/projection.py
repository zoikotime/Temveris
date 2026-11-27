"""Actor entitlement projection (spec §7 and §17).

This module turns an already-computed workspace entitlement profile into an
actor-aware *presentation* projection. It is read-only and derives everything
from data that already exists — the resolver's decision, the capability nature,
and whether any plan sells the capability.

Two hard rules, both tested:

1. **Presentation is not authorization.** The entitlement decision stays the
   source of truth. A denied capability can never project as SHOW. The actor's
   role can only ever *narrow* what is shown (LOCK → HIDE for a worker), never
   widen it.

2. **No second resolver.** This consumes the output of
   :func:`app.commercial.entitlements.get`; it never recomputes entitlements and
   never touches the cache. Presentation depends on the actor's role, and the
   entitlement cache is keyed on organization alone, so presentation is derived
   per request and never persisted — otherwise one actor's view could be served
   to another.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.commercial import manifests

#: Roles that are ordinary workers for presentation purposes. A worker never
#: sees a commercial upsell (§7): a locked capability is hidden from them, not
#: dangled. Everyone else (admin, owner, hr, manager, super_admin, ...) may see
#: LOCK. These names are read from the existing operational role vocabulary; no
#: new role is invented here. ``employee`` is the model's default role.
WORKER_ROLES = frozenset({"employee", "user", "worker"})


def is_worker(role: Optional[str]) -> bool:
    """Whether this actor is an ordinary worker (so must not see upsell)."""
    return (role or "employee").lower() in WORKER_ROLES


def _reason(decision: str, feature_key: str, presentation: str) -> str:
    """A short, transparently derived explanation of the presentation.

    The resolver does not persist a per-capability reason, so this is derived
    from the decision, the capability nature and plan availability — not read
    from a resolver internal. It is labelled ``derived`` for exactly that
    reason and must never be presented as an authoritative resolver trace.
    """
    if decision == manifests.ALLOW:
        return "granted_by_plan"
    if decision == manifests.READ_ONLY:
        return "historical_read_only"
    if decision == manifests.CONFIGURATION_REQUIRED:
        return "requires_configuration"
    # denied
    if feature_key in manifests.GLOBALLY_DENIED:
        return "globally_denied"
    if not manifests.is_upsellable(feature_key):
        return "not_available_on_any_plan"
    return "upgrade_required" if presentation == manifests.LOCK else "not_entitled"


def project_capability(feature_key: str, decision: str, *, worker: bool) -> Dict[str, Any]:
    """One capability's projection row."""
    presentation = manifests.presentation_for(decision, feature_key, is_worker=worker)
    return {
        "capability": feature_key,
        "decision": decision,               # authoritative — the source of truth
        "presentation": presentation,       # derived hint only
        "nature": manifests.capability_nature(feature_key),
        "upsellable": manifests.is_upsellable(feature_key),
        "reason": _reason(decision, feature_key, presentation),
    }


def build_actor_projection(profile: Dict[str, Any], *, role: Optional[str]) -> Dict[str, Any]:
    """Project a computed entitlement ``profile`` for an actor with ``role``.

    Returns the whole 39-key vocabulary. Presentation is role-aware; decisions
    are copied through unchanged. If a capability is somehow absent from the
    profile it is treated as DENY — a projection must never invent access for a
    key the resolver did not decide.
    """
    worker = is_worker(role)
    caps: Dict[str, str] = profile.get("capabilities") or {}
    rows: List[Dict[str, Any]] = [
        project_capability(key, caps.get(key, manifests.DENY), worker=worker)
        for key in sorted(manifests.FEATURES)
    ]
    return {
        "tenant_id": profile.get("tenant_id"),
        "plan": profile.get("plan"),
        "commercial_state": profile.get("commercial_state"),
        "manifest_version": profile.get("manifest_version"),
        "snapshot_hash": profile.get("snapshot_hash"),
        "actor_role": role,
        "actor_is_worker": worker,
        "restrictions": profile.get("restrictions"),
        "capabilities": rows,
    }


# The resolver pipeline, in the order compute() actually applies it. Used by the
# inspector's decision trace. Kept here next to the projection so the two stay in
# step, and deliberately honest about what is NOT evaluated: there is no
# workspace-configuration layer (spec §3 layer 6 is unbuilt), and actor RBAC is a
# separate authorization concern that the entitlement resolver does not apply.
_TRACE_LAYERS = (
    ("global",                "evaluated",
     "Kill switch and globally denied privacy keys, baked into the plan manifest."),
    ("plan",                  "evaluated",
     "Base manifest for the tenant's plan."),
    ("enterprise_overlay",    "evaluated",
     "Enterprise override applied when present."),
    ("jurisdiction",          "evaluated",
     "Country/sanctions narrowing."),
    ("lifecycle",             "evaluated",
     "Suspension and lifecycle state narrowing (trial/past_due/suspended/...)."),
    ("workspace_configuration", "not_evaluated",
     "Spec §3 layer 6 is not built. configuration_required decisions are therefore "
     "not yet resolvable to allow; nothing here fabricates that layer."),
    ("actor_rbac",            "separate_concern",
     "Entitlement is workspace-level. Actor authorization (User.role) is enforced "
     "separately and is never part of the entitlement decision."),
)


def decision_trace() -> List[Dict[str, str]]:
    """The resolver's actual layer order, honestly annotated (spec §19)."""
    return [
        {"layer": name, "status": status, "note": note}
        for name, status, note in _TRACE_LAYERS
    ]
