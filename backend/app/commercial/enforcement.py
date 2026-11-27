"""Server-side entitlement enforcement at the boundaries that need it.

The entitlements resolver (``app.commercial.entitlements``) has always computed
a correct profile, but nothing called ``can()`` or ``within_limit()`` — the
limits were advisory. This module is the enforcement layer, kept deliberately
small: it covers the two boundaries that grow a tenant (adding a member, adding
a team) rather than attempting a platform-wide rewrite.

BACKWARD COMPATIBILITY — THE IMPORTANT PART
-------------------------------------------
Enforcement **fails open when the entitlement cannot be resolved**, and this is
a correctness requirement, not laziness.

Super-admin-provisioned organizations deliberately have no
``CommercialSubscription``: they are contract customers, not self-serve trials,
and their ``commercial_state`` is set to ``"active"`` directly.
``entitlements.compute()`` takes its ``not org or not sub`` branch for them and
returns ``_empty_profile``, in which **every limit is 0**. Naive enforcement
would therefore have told every existing tenant that they had run out of seats
and blocked all invitations.

So a limit is only applied when there is a real resolved subscription to apply
it from. An unresolvable tenant is never blocked — it is logged instead. That
keeps existing customers working while making the limits real for tenants that
have a subscription.

TENANT SAFETY
-------------
The organization is always derived from the authenticated user's server-side
state via ``app.commercial.tenant_resolver``. No caller may pass an
organization id in.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import models
from database.database import get_db
from app.auth.service import get_current_user
from app.commercial import constants, entitlements
from app.commercial.models import CommercialSubscription
from app.commercial.tenant_resolver import resolve_commercial_org

_log = logging.getLogger("zoiko.commercial.enforcement")


def _resolvable_limits(db: Session, user: models.User) -> Optional[dict]:
    """Return the tenant's limits dict, or None when not safely resolvable.

    None means "do not enforce" — see the module docstring. Two cases produce
    it: the user maps to no commercial organization at all, or that organization
    has no subscription row for a limit to come from.
    """
    org = resolve_commercial_org(db, user)
    if org is None:
        return None

    sub = (
        db.query(CommercialSubscription)
          .filter(CommercialSubscription.organization_id == org.id)
          .first()
    )
    if sub is None:
        # Super-admin-provisioned tenant. Not a limit of zero — an absence of
        # information about limits.
        _log.debug(
            "Skipping limit enforcement for org %s: no subscription row", org.id
        )
        return None

    profile = entitlements.get(db, org.id)
    limits = profile.get("limits") or {}
    if profile.get("commercial_state") == "manual_review" and not profile.get("plan"):
        return None
    return limits


def _seat_usage(db: Session, organization_id: int) -> int:
    """Active members of an operational organization."""
    return (
        db.query(models.User)
          .filter(
              models.User.organization_id == organization_id,
              models.User.is_active == True,  # noqa: E712 - SQL predicate
          )
          .count()
    )


def seat_limit_status(
    db: Session, user: models.User
) -> Tuple[Optional[int], int]:
    """Return ``(seat_limit, seats_used)``.

    ``seat_limit`` is None for unlimited *or* not-enforceable; callers that need
    to distinguish should use :func:`check_seat_limit`.
    """
    org_id = getattr(user, "organization_id", None)
    used = _seat_usage(db, org_id) if org_id is not None else 0
    limits = _resolvable_limits(db, user)
    cap = limits.get("seat_limit") if limits else None
    return cap, used


def check_seat_limit(db: Session, user: models.User, *, adding: int = 1) -> None:
    """Raise 403 if adding ``adding`` member(s) would exceed the seat limit.

    No-op when the tenant's entitlement is not resolvable, or when the plan's
    seat limit is unlimited (None).
    """
    org_id = getattr(user, "organization_id", None)
    if org_id is None:
        # An org-less caller has no tenant to size. Tenant-scoping guards
        # elsewhere are responsible for rejecting them; this is not that check.
        return

    limits = _resolvable_limits(db, user)
    if not limits:
        return

    cap = limits.get("seat_limit")
    if cap is None:
        return

    used = _seat_usage(db, org_id)
    if used + adding > int(cap):
        raise HTTPException(
            status_code=403,
            detail={
                "code": constants.ERR_SEAT_OVER_ALLOCATION,
                "message": (
                    f"Your plan includes {cap} seats and {used} are in use. "
                    "Upgrade your plan to add more people."
                ),
                "seat_limit": int(cap),
                "seats_used": used,
            },
        )


def check_team_limit(db: Session, user: models.User, *, adding: int = 1) -> None:
    """Raise 403 if adding ``adding`` team(s) would exceed the team limit.

    Temveris's plans currently set ``team_limit`` to None (unlimited) for every
    tier, so this is a no-op in practice today. It exists — and is tested — so
    that introducing a cap is a configuration change in
    ``entitlements._PLAN_LIMITS`` rather than new enforcement code, and so the
    onboarding wizard can render a real server-provided limit instead of a
    decorative one.
    """
    org_id = getattr(user, "organization_id", None)
    if org_id is None:
        return

    limits = _resolvable_limits(db, user)
    if not limits:
        return

    cap = limits.get("team_limit")
    if cap is None:
        return

    used = (
        db.query(models.Team)
          .filter(models.Team.organization_id == org_id)
          .count()
    )
    if used + adding > int(cap):
        raise HTTPException(
            status_code=403,
            detail={
                "code": constants.ERR_TEAM_LIMIT_REACHED,
                "message": (
                    f"Your plan includes {cap} teams and {used} already exist. "
                    "Upgrade your plan to add more."
                ),
                "team_limit": int(cap),
                "teams_used": used,
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
# Capability enforcement
# ══════════════════════════════════════════════════════════════════════════════
# ENTITLEMENT IS NOT AUTHORIZATION. Two different questions, and a request needs
# both answered yes:
#
#   entitlement  — organization-level. "Does this workspace's plan include
#                  advanced reporting?"  Answered here, from the resolver.
#   authorization — actor-level. "May *this user* run a report?"  Answered by the
#                  existing role checks, which are untouched.
#
# Conflating them is how an entitled workspace lets an intern run payroll, or how
# an admin is refused something their plan includes. `require_capability` answers
# only the first; every route keeps whatever role check it already had.

#: Shadow mode. While true, a denial is logged and the request proceeds, so the
#: real traffic shape can be observed before anything is actually blocked.
#: Enforcement is enabled per capability by removing keys from this set — never
#: by a global flag day.
SHADOW_CAPABILITIES: set = set()


# ══════════════════════════════════════════════════════════════════════════════
# Enforcement rollout control (Phase 3.21) — a SAFE control surface, OFF by
# default, NOT wired to any route. Nothing in the request path consults it yet;
# it exists so a future enforcement phase can turn 403s on progressively and kill
# them instantly. Every layer below fails to OFF, so a missing or malformed
# config, a restart, or an unrelated setting change can never enable enforcement.
# ══════════════════════════════════════════════════════════════════════════════
_rollout_log = logging.getLogger("zoiko.entitlement.rollout")

#: Deployment master switch. Declared, not sniffed, and default OFF: an unset or
#: malformed value means "do not enforce", never "enforce". Mirrors the billing
#: environment guard's discipline.
_MASTER_ENV = "ENTITLEMENT_ENFORCEMENT"
_TRUE_ENV = frozenset({"1", "true", "yes", "on"})

#: The phase5 feature flag that gates enforcement per organization (default False,
#: in-memory, resets to OFF on restart — reusing the existing rollout system, not
#: a second one).
ENFORCEMENT_FLAG = "entitlement_enforcement"

#: Per-capability enforce scope. Empty = no capability is enforced (shadow only).
#: A capability is enrolled for real enforcement by ADDING it here — the inverse,
#: safe-by-default counterpart to SHADOW_CAPABILITIES.
ENFORCE_CAPABILITIES: set = set()

#: In-process kill-switch. Flipping this True disables all enforcement instantly,
#: without a restart or a deploy — the emergency stop.
_KILL_SWITCH = False

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"


def kill_enforcement() -> None:
    """Emergency stop: force every enforcement evaluation to OFF, immediately."""
    global _KILL_SWITCH
    _KILL_SWITCH = True
    _rollout_log.warning("entitlement enforcement KILL-SWITCH engaged — all OFF")


def arm_enforcement() -> None:
    """Release the in-process kill-switch. Does NOT enable enforcement on its own —
    the env master, the per-org flag and the per-capability scope must still say so."""
    global _KILL_SWITCH
    _KILL_SWITCH = False
    _rollout_log.info("entitlement enforcement kill-switch released")


def _master_enabled() -> bool:
    raw = os.getenv(_MASTER_ENV)
    if raw is None:
        return False                        # missing -> OFF
    return raw.strip().lower() in _TRUE_ENV  # anything else (incl. malformed) -> OFF


def enforcement_mode(*, capability: Optional[str] = None,
                     organization_id: Optional[str] = None) -> str:
    """Resolve the enforcement mode for a (capability, organization).

    Returns one of ``off`` | ``shadow`` | ``enforce``. Deterministic, read-only,
    and OFF unless every gate deliberately opts in:

      kill-switch engaged            -> off   (emergency stop, in-process)
      master env unset/false/invalid -> off   (deployment gate)
      per-org flag off               -> off   (rollout scope, phase5)
      capability not enrolled        -> shadow (per-capability rollout scope)
      all of the above opt in        -> enforce

    Nothing here grants access or blocks a request — it only reports what a future
    enforcement layer WOULD do. It is never consulted by a live route in 3.21.
    """
    if _KILL_SWITCH:
        return MODE_OFF
    if not _master_enabled():
        return MODE_OFF
    from app.commercial import phase5                         # noqa: PLC0415
    if not phase5.is_enabled(ENFORCEMENT_FLAG, organization_id=organization_id):
        return MODE_OFF
    if capability is not None and capability not in ENFORCE_CAPABILITIES:
        return MODE_SHADOW
    return MODE_ENFORCE


def should_enforce(*, capability: str, organization_id: Optional[str] = None) -> bool:
    """True only when enforcement is fully opted in for this capability/org."""
    return enforcement_mode(capability=capability,
                            organization_id=organization_id) == MODE_ENFORCE


def enforcement_status() -> dict:
    """Diagnostic snapshot of the rollout control, for the admin surface/logs.

    Reports only configuration state — never a tenant's data.
    """
    return {
        "kill_switch": _KILL_SWITCH,
        "master_env_enabled": _master_enabled(),
        "flag": ENFORCEMENT_FLAG,
        "enforce_capabilities": sorted(ENFORCE_CAPABILITIES),
        "shadow_capabilities": sorted(SHADOW_CAPABILITIES),
        "global_mode": enforcement_mode(),   # capability/org-agnostic view
    }


def capability_decision(db: Session, user: models.User, feature_key: str) -> str:
    """The workspace's decision about ``feature_key``. Always a decision.

    A caller with no commercial identity resolves to DENY rather than to "no
    answer". This used to return None, and None meant "skip enforcement" — which
    made a missing row a bypass of everything, including the two globally denied
    privacy capabilities. A provisioning bug or a stray seed row would then have
    become an entitlement bypass, which is precisely the failure mode the global
    denial exists to prevent.

    Every production path creates the identity (``provision_tenant`` Step 7), and
    the organizations that predated it have been backfilled, so this branch now
    means "something is wrong with this tenant" rather than "this tenant is
    normal". Denying is the honest answer to that, and it surfaces the problem
    instead of hiding it.

    Scope note: this changes the CAPABILITY path only. ``_resolvable_limits``
    above — seat and team limits — keeps its documented fail-open, because
    denying there would block invitations for working tenants and seat policy is
    a separate decision.
    """
    org = resolve_commercial_org(db, user)
    if org is None:
        _log.warning(
            "capability %s denied: no commercial identity for user=%s org=%s. "
            "Every provisioning path creates one; investigate this tenant.",
            feature_key, getattr(user, "id", None),
            getattr(user, "organization_id", None))
        return manifests_deny()
    return entitlements.capability(db, org.id, feature_key)


def manifests_deny() -> str:
    """The DENY constant, imported lazily to keep the module import graph flat."""
    from app.commercial import manifests  # noqa: PLC0415
    return manifests.DENY


def require_capability(feature_key: str):
    """FastAPI dependency factory: the workspace must be entitled to ``feature_key``.

    Usage mirrors the auth dependencies already in ``app.auth.service``::

        @router.get("/reports/advanced",
                    dependencies=[Depends(require_capability("reports.advanced"))])

    It deliberately returns nothing useful: it is a gate, not a data source. The
    organization is resolved server-side from the authenticated user, so no
    caller can name a tenant.

    Phase 4.2: the dependency is gated by the rollout control (see
    :func:`_gate_capability`). The failure modes below apply ONLY when
    ``should_enforce()`` is True for this ``(capability, organization)``; while
    enforcement is OFF/shadow the dependency is non-blocking and simply returns.

    Failure modes, all fail-closed except one:

    * DENY                    -> 403, the plan does not include this.
    * CONFIGURATION_REQUIRED  -> 409, the plan includes it but it is not switched
                                 on. A distinct status because the fix is a setup
                                 step, not an upgrade — the UI must show a setup
                                 CTA rather than a pricing page.
    * unknown capability key  -> 403. A typo must never widen access.
    * READ_ONLY / ALLOW       -> permitted. Callers that must distinguish a write
                                 from a read use :func:`require_writable_capability`.

    * unresolvable tenant     -> 403. A caller with no commercial identity is
                                 denied, not skipped. Every provisioning path
                                 creates one and the organizations that predated
                                 it were backfilled, so this now means the tenant
                                 is broken rather than ordinary — and skipping
                                 would have bypassed the global privacy denials.
                                 Seat limits in ``_resolvable_limits`` keep their
                                 separate, documented fail-open.
    """
    def _dependency(
        request: Request,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user),
    ) -> None:
        _gate_capability(db, user, feature_key, writable=False, request=request)

    return _dependency


def require_writable_capability(feature_key: str):
    """As :func:`require_capability`, but READ_ONLY is refused.

    For routes that create, edit, approve, configure or execute. A suspended or
    cancelled workspace keeps historical read access to several capabilities;
    this is what stops that access from being mistaken for permission to write.
    """
    def _dependency(
        request: Request,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user),
    ) -> None:
        _gate_capability(db, user, feature_key, writable=True, request=request)

    return _dependency


def _gate_capability(db: Session, user: models.User, feature_key: str,
                     *, writable: bool, request=None) -> None:
    """Rollout-gated entry point for the FastAPI dependencies (Phase 4.2/4.3).

    This is the layer that makes ``require_capability`` safe to attach to a live
    route while enforcement is OFF. The master safety switch is the existing
    rollout control (``enforcement_mode`` / ``should_enforce`` — the env master,
    the per-org phase5 flag and the per-capability ``ENFORCE_CAPABILITIES``
    scope). Unless enforcement is fully opted in for this ``(capability,
    organization)``, this dependency is non-blocking: it returns and the request
    proceeds exactly as before, and the route's existing shadow hook (if any)
    still provides observation. Only when ``should_enforce()`` is True does it
    consult :func:`_assert_capability`, the unchanged decision unit that applies
    the entitlement verdict (403 / 409 / read-only) and whose contract the direct
    unit tests pin.

    Phase 4.3 — self-observing in SHADOW mode: when the rollout is on but this
    (capability, org) is not yet enrolled for real enforcement, the gate records
    what enforcement WOULD do on THIS route and allows the request. That makes the
    observation surface exactly the enforcement surface — every wired route is
    recorded, with no per-route hook — so a shadow window sizes the real blast
    radius. The observation is fail-open (``shadow_capability`` never raises).

    Fast path: the deployment master switch and the kill-switch are the only
    org-agnostic gates. While either forces OFF, no organization can be in
    enforce mode, so this returns without resolving the org AND without observing
    — no shadow rows while the rollout is completely off, and zero per-request
    cost (the default and current production state). (The per-org phase5 flag is
    deliberately NOT part of this fast path: an org enrolled via a per-org
    override must still be evaluated even when the flag's default is off.)
    """
    if _KILL_SWITCH or not _master_enabled():
        return
    org = resolve_commercial_org(db, user)
    org_id = org.id if org is not None else None
    mode = enforcement_mode(capability=feature_key, organization_id=org_id)
    if mode == MODE_ENFORCE:
        _assert_capability(db, user, feature_key, writable=writable)
        return
    if mode == MODE_SHADOW:
        # Observe (never block). Doubly fail-open: shadow_capability swallows its
        # own errors, and this guard ensures even an unexpected raise cannot break
        # the request the gate is attached to.
        try:
            shadow_capability(db, user, feature_key,
                              route=_route_label(request, feature_key), writable=writable)
        except Exception:                            # noqa: BLE001
            _log.debug("shadow observation failed for %s", feature_key, exc_info=True)
    # MODE_OFF (per-org flag off) or after SHADOW observation: non-blocking.
    return


def _route_label(request, feature_key: str) -> str:
    """A ``"METHOD /path"`` label for the shadow observation, taken from the
    matched route. Falls back to the capability key when no request is available
    (e.g. direct unit-test calls). Never raises — the gate must not break a
    request over a telemetry label."""
    if request is None:
        return f"CAP {feature_key}"
    try:
        matched = request.scope.get("route")
        path = getattr(matched, "path", None) or request.url.path
        return f"{request.method} {path}"
    except Exception:                                    # noqa: BLE001
        return f"CAP {feature_key}"


def _assert_capability(db: Session, user: models.User, feature_key: str,
                       *, writable: bool) -> None:
    """Shared body. Raises, logs in shadow mode, or returns."""
    from app.commercial import manifests            # noqa: PLC0415 — import cycle

    decision = capability_decision(db, user, feature_key)

    permitted = decision in manifests.PERMISSIVE
    if writable and decision == manifests.READ_ONLY:
        permitted = False

    if permitted:
        return

    if feature_key in SHADOW_CAPABILITIES:
        _log.warning("SHADOW capability denial: user=%s feature=%s decision=%s",
                     getattr(user, "id", None), feature_key, decision)
        return

    if decision == manifests.CONFIGURATION_REQUIRED:
        raise HTTPException(
            status_code=409,
            detail={
                "code": constants.ERR_ENTITLEMENT_FAILED,
                "message": "This feature is included in your plan but has not been "
                           "set up yet.",
                "feature": feature_key,
                "decision": decision,
            },
        )

    raise HTTPException(
        status_code=403,
        detail={
            "code": constants.ERR_ENTITLEMENT_FAILED,
            "message": "Your plan does not include this feature.",
            "feature": feature_key,
            "decision": decision,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# Shadow observation
# ══════════════════════════════════════════════════════════════════════════════
# Records what enforcement WOULD do without doing it. The request continues
# exactly as it does today, whatever the decision and whatever goes wrong here.
#
# Why an explicit call rather than a dependency or middleware:
#   * A dependency would need `Depends(get_current_user)`, and attaching that to
#     a router with any public route would start returning 401 — a shadow
#     observer that can reject traffic is not a shadow observer.
#   * Middleware cannot see the resolved user: nothing in this codebase puts it
#     on `request.state`, so it would have to decode the token itself and become
#     a second copy of authentication.
# Calling it from handlers that already injected `current_user` avoids both, and
# reuses the one resolver rather than adding another.

_shadow_log = logging.getLogger("zoiko.entitlement.shadow")


def shadow_capability(db: Session, user: models.User, feature_key: str, *,
                      route: str, writable: bool = False) -> None:
    """Record the decision this capability would produce. Never blocks, never raises.

    Returns None always and swallows every exception, including database errors:
    shadow mode must not become an availability risk, and a logging failure must
    never be the reason a request fails.

    The organization is resolved server-side through the same path
    ``require_capability`` uses. Nothing about the decision comes from the client.

    Emits one structured line per call, carrying only what the rollout question
    needs — organization, plan, lifecycle state, capability, decision, whether it
    would block, and where. No user identifier beyond the organization, no
    request body, no credentials.
    """
    try:
        from app.commercial import manifests  # noqa: PLC0415

        org = resolve_commercial_org(db, user)
        if org is None:
            decision, plan, state = manifests.DENY, None, None
            reason = "no_commercial_identity"
        else:
            profile = entitlements.get(db, org.id)
            decision = (profile.get("capabilities") or {}).get(
                feature_key, manifests.DENY)
            plan = profile.get("plan")
            state = profile.get("commercial_state")
            reason = ("unknown_capability" if feature_key not in manifests.FEATURES
                      else "manifest")

        would_block = decision not in manifests.PERMISSIVE or (
            writable and decision == manifests.READ_ONLY)

        # HTTP method, split from the "GET /path" route string the hook already
        # passes — a separate field so the live-window report can aggregate by
        # method without parsing. No new data is collected.
        method = route.split(" ", 1)[0].upper() if route and " " in route else None
        # The actor's ROLE (admin/employee/...) — low-cardinality and NOT a user
        # identifier (no id, no email), so the "no user identifier beyond the
        # organization" rule holds. Kept so the rollout risk analysis can size the
        # role impact of future enforcement.
        role = getattr(user, "role", None)

        _shadow_log.info(
            "entitlement.shadow %s",
            json.dumps({
                "organization_id": org.id if org is not None else None,
                "plan": plan,
                "lifecycle_state": state,
                "capability": feature_key,
                "decision": decision,
                "would_block": would_block,
                "writable": writable,
                "route": route,
                "method": method,
                "role": role,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            }, sort_keys=True),
        )
    except Exception:                                       # noqa: BLE001
        # Deliberately total. Nothing observed here is worth failing a request.
        _shadow_log.debug("shadow evaluation failed for %s on %s",
                          feature_key, route, exc_info=True)
    return None


def shadow_report(db: Session, *, capabilities: Optional[list] = None) -> dict:
    """Population-level view of what enforcement would do, without traffic.

    Request-level shadowing answers "how often", but it can only see routes that
    are exercised. This walks every commercial organization against the
    capabilities due for enforcement and answers the rollout questions directly:
    which organizations would be denied, which capabilities, which plans and
    which lifecycle states.

    Read-only apart from the entitlement cache the resolver already maintains.
    """
    from app.commercial import manifests                    # noqa: PLC0415
    from app.commercial.models import CommercialOrganization  # noqa: PLC0415

    keys = capabilities or sorted(SHADOW_CAPABILITIES) or sorted(manifests.FEATURES)
    out = {"organizations": 0, "would_block": {}, "by_plan": {}, "by_state": {},
           "denied_organizations": [], "capabilities": keys}

    for org in db.query(CommercialOrganization).all():
        out["organizations"] += 1
        try:
            profile = entitlements.get(db, org.id)
        except Exception:                                   # noqa: BLE001
            continue
        caps = profile.get("capabilities") or {}
        plan = profile.get("plan")
        state = profile.get("commercial_state")
        blocked = [k for k in keys
                   if caps.get(k, manifests.DENY) not in manifests.PERMISSIVE]
        for k in blocked:
            out["would_block"][k] = out["would_block"].get(k, 0) + 1
        if blocked:
            out["by_plan"][plan] = out["by_plan"].get(plan, 0) + 1
            out["by_state"][state] = out["by_state"].get(state, 0) + 1
            out["denied_organizations"].append(
                {"organization_id": org.id, "plan": plan, "state": state,
                 "blocked": blocked})
    return out
