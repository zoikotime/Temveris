"""Entitlements resolver (§13) — invariant I-3.

Resolution order per §13.2:
    1. Load tenant
    2. Load commercial state
    3. Load base plan
    4. Apply trial overlay if active
    5. Apply add-ons
    6. Apply enterprise overrides
    7. Apply jurisdiction restrictions
    8. Apply suspension or billing restrictions
    9. Apply beta flags
    10. Return computed entitlement object

App code should always call ``can(org_id, "data_export")`` — never check
plan_code or commercial_state directly. The resolver is the single point
of truth for "is this tenant allowed to do X right now".

Cache lives in commercial_entitlements_cache with a 1h TTL. Invalidate
on plan change, state transition, suspension, add-on change, or
enterprise override update — see ``invalidate(org_id)``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

import hashlib
import json

from app.commercial import constants, manifests
from app.commercial.models import (
    CommercialOrganization,
    CommercialSubscription,
    EntitlementsCache,
)

_log = logging.getLogger("zoiko.commercial.entitlements")

# Entitlement observability (spec §18). A dedicated structured logger, mirroring
# the shadow logger, so snapshot lifecycle events can be filtered and shipped
# without touching the request transaction. Logging-only by design: emission runs
# inside the resolver's hot path, so it must never be able to fail a request or
# poison the session. Persisting these to commercial_events is a clean future
# step (the payload already maps to emit_event's fields) but is deliberately kept
# out of the hot path here — see _emit_snapshot_events.
_events_log = logging.getLogger("zoiko.entitlement.events")

#: The two §18 events this phase implements. The enforcement-dependent ones
#: (feature.access.denied, feature.configuration_required) and the override
#: event are intentionally deferred — they need traffic or a workflow that does
#: not exist yet.
EVENT_SNAPSHOT_CREATED = "entitlements.snapshot.created"
EVENT_CHANGED = "entitlements.changed"


def _persist_snapshot_event(db: Session, event_type: str, *, organization_id: str,
                            state: str, payload: Dict[str, Any]) -> None:
    """Durably record one snapshot event on the existing commercial event stream.

    Isolation is the whole point. The row is written inside a SAVEPOINT
    (``begin_nested``) — the codebase's standard failure-isolation pattern — so a
    persistence failure rolls back only this insert, never the cache row (flushed
    into the outer transaction before this is called) nor the caller's work.
    ``emit_event`` does not commit, so the event shares the caller's transaction:
    it becomes durable exactly when the snapshot it describes does, and on a
    pure-read recompute that the caller never commits, neither persists — no
    orphan events. Wrapped so it can never raise into the resolver.
    """
    try:
        from app.commercial.audit import emit_event   # noqa: PLC0415 — avoid cycle
        with db.begin_nested():
            emit_event(
                db,
                event_type=event_type,
                source_service="entitlement_resolver",
                organization_id=organization_id,
                state_to=state,
                payload=payload,
            )
    except Exception:                       # noqa: BLE001 — persistence is never fatal
        _log.debug("durable entitlement event persist failed for %s (%s)",
                   organization_id, event_type, exc_info=True)


def _emit_snapshot_events(db: Session, *, organization_id: str, plan: str, state: str,
                          manifest_version: Optional[str],
                          old_hash: Optional[str], new_hash: Optional[str]) -> None:
    """Emit the §18 snapshot-lifecycle events for one write-through.

    Each event is both logged (structured, for shipping/metrics) and durably
    recorded on ``commercial_events`` (for the audit trail and the Admin
    Inspector). Semantics, keyed on the content hash (never on timestamps or
    cache metadata):

    * ``old_hash is None`` — no snapshot existed for this tenant, so a genuinely
      new one was just persisted: emit ``entitlements.snapshot.created``. This is
      the first-ever compute AND every post-invalidation recompute (a plan or
      lifecycle change routes through ``invalidate`` first, which drops the row,
      so the change surfaces here as a freshly created snapshot).
    * ``old_hash != new_hash`` with a row present — an existing snapshot was
      overwritten with different effective content: emit both
      ``entitlements.snapshot.created`` (a new effective snapshot was persisted)
      and ``entitlements.changed`` (the hash differs). This is the
      non-invalidating path, e.g. a manifest-version deploy detected on the next
      TTL refresh, since a manifest change deliberately does not invalidate.
    * ``old_hash == new_hash`` — an identical recompute (TTL refresh, repeated
      resolve, unchanged inputs): emit nothing. No false ``changed``.

    Never raises. Neither a logging nor a persistence failure may become the
    reason a request fails or an entitlement decision changes.
    """
    try:
        if old_hash == new_hash and old_hash is not None:
            return                          # identical recompute — no event

        base = {
            "organization_id": organization_id,
            "plan": plan,
            "commercial_state": state,
            "manifest_version": manifest_version,
            "snapshot_hash": new_hash,
            "previous_hash": old_hash,
            "at": _now().isoformat(),
        }
        to_emit = [EVENT_SNAPSHOT_CREATED]
        if old_hash is not None and old_hash != new_hash:
            to_emit.append(EVENT_CHANGED)

        for event_type in to_emit:
            payload = {**base, "event": event_type}
            _events_log.info("%s %s", event_type,
                             json.dumps(payload, sort_keys=True))
            _persist_snapshot_event(db, event_type, organization_id=organization_id,
                                    state=state, payload=payload)
    except Exception:                       # noqa: BLE001 — observability is never fatal
        _log.debug("entitlement event emission failed for %s",
                   organization_id, exc_info=True)


# ──────────────────────────────────────────────────────────────────────────
# Base plan feature matrices.
# Source of truth for which plan grants which features. Pricing lives in
# billing_plans (DB-driven); features live here because they map to code
# paths in the app and we never want a deploy-less code path change.
# ──────────────────────────────────────────────────────────────────────────
_PLAN_FEATURES: Dict[str, Dict[str, bool]] = {
    "verified": {
        "time_tracking": True,
        "dashboards": True,
        "audit_logs": True,
        "advanced_compliance": False,
        "legal_hold": False,
        # SSO is a security baseline on every plan (Product decision, 3.18). This
        # legacy flag is the source the mirror enforces, so the manifest grant is
        # only effective once this is True as well.
        "sso": True,
        "scim": False,
        "api_access": False,
        "data_export": False,
    },
    "governed": {
        "time_tracking": True,
        "dashboards": True,
        "audit_logs": True,
        "advanced_compliance": True,
        "legal_hold": False,
        "sso": True,        # SSO all plans (Product decision, 3.18)
        "scim": False,
        "api_access": True,
        "data_export": True,
    },
    "sovereign": {
        "time_tracking": True,
        "dashboards": True,
        "audit_logs": True,
        "advanced_compliance": True,
        "legal_hold": True,
        "sso": True,
        "scim": True,
        "api_access": True,
        "data_export": True,
    },
    "enterprise": {
        "time_tracking": True,
        "dashboards": True,
        "audit_logs": True,
        "advanced_compliance": True,
        "legal_hold": True,
        "sso": True,
        "scim": True,
        "api_access": True,
        "data_export": True,
    },
}

# `team_limit` is None (unlimited) on every tier: Temveris does not currently
# sell a team cap, and inventing numbers here would be a product decision
# disguised as a config default. The key exists so the onboarding wizard can ask
# the server what the limit is and render it only when one is actually set —
# never a decorative cap — and so app.commercial.enforcement.check_team_limit
# has something real to read. Introducing a cap is then a change here alone.
_PLAN_LIMITS: Dict[str, Dict[str, Optional[int]]] = {
    "verified":  {"seat_limit": 25,  "retention_days": 365,  "api_requests_per_day": 0,    "workspace_count": 1, "team_limit": None},
    "governed":  {"seat_limit": 100, "retention_days": 730,  "api_requests_per_day": 5000, "workspace_count": 1, "team_limit": None},
    "sovereign": {"seat_limit": None, "retention_days": 2555, "api_requests_per_day": 50000, "workspace_count": 3, "team_limit": None},
    "enterprise":{"seat_limit": None, "retention_days": 3650, "api_requests_per_day": None,  "workspace_count": None, "team_limit": None},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalise a stored timestamp to UTC.

    ``computed_at`` and ``expires_at`` are ``DateTime(timezone=True)``. PostgreSQL
    returns them already aware, in the *session's* timezone; SQLite (the test
    harness) has no native zone and returns them naive. The previous check used
    ``.replace(tzinfo=utc)``, which relabels the wall-clock reading instead of
    converting it — so on an Asia/Calcutta session every deadline landed 5h30m
    late and the one-hour TTL behaved like six and a half hours. The size of the
    error was exactly the session's UTC offset, which is why a UTC server showed
    no symptom at all.

    Convert when the value is aware; assume UTC only when it carries no zone.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_fresh(expires_at: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """Whether a cache row is still within its TTL.

    Kept as a pure function with no database involvement so it can be tested
    directly against several UTC offsets. The SQLite test harness cannot
    reproduce the timezone-aware behaviour that made this wrong, so a test that
    round-trips through the database would prove nothing.
    """
    if expires_at is None:
        return False
    return _as_utc(expires_at) > (now or _now())


def _is_internal_org(db: Session, org: CommercialOrganization) -> bool:
    """Whether this commercial identity belongs to Temveris's own tenant.

    Delegates to the existing billing discriminator
    (``billing.service.is_internal_tenant`` reading ``organizations.billing_type``)
    rather than introducing a second notion of "internal".

    The ``operational_org_id`` guard is load-bearing and deliberate:
    ``is_internal_tenant`` fails *closed* for a missing organization, returning
    True so that an unknown tenant is never charged. That default is right for
    billing and exactly wrong here — a legacy commercial organization with no
    operational link would be promoted to internal, and internal is the most
    privileged profile there is. Unlinked means "not provably internal".

    Imported lazily: app.billing imports nothing from app.commercial, and
    app.commercial.entitlements is imported very early.
    """
    if not org.operational_org_id:
        return False
    from app.billing.service import is_internal_tenant  # noqa: PLC0415
    return is_internal_tenant(db, org.operational_org_id)


def _resolve_email_verified(db: Session, org: CommercialOrganization) -> bool:
    """The authoritative email-verification state for an organization.

    Spec I-6 makes this an ORGANIZATION-level expansion gate — it decides whether
    the tenant may invite and export, not what one member may do — so it keys on
    the organization's owner. That is exactly what
    ``routers/entitlements.py`` already derived before calling in; resolving it
    here instead makes every caller agree.

    Why it moved: ``email_verified`` used to be a caller-supplied parameter while
    the cache was keyed on ``organization_id`` alone. Callers passed four
    different things (False during provisioning and reconciliation, a hardcoded
    True in the admin router, the default True elsewhere, and the real value in
    the HTTP router), so whichever caller warmed the cache first decided what
    every other caller saw for the rest of the TTL. A background sweep computing
    with False would serve a verified owner an unverified profile. No
    invalidation hook could fix that, because nothing in the data had changed.

    An organization with no owner recorded cannot be shown to have verified
    anything, so it resolves False — the restrictive answer.
    """
    from app.commercial import identity  # noqa: PLC0415 — keeps the import graph flat
    if not org.owner_user_id:
        return False
    return identity.is_email_verified(db, org.owner_user_id)


def _snapshot_hash(*, plan_code: str, state: str, country: Optional[str],
                   email_verified: bool, enterprise_override: Optional[dict],
                   manifest_version: str, screenshot_config: str = "") -> str:
    """Integrity and cache-invalidation token for one computed profile (spec 3.1).

    Covers every organization-level input the resolver actually reads. Two
    profiles computed from identical inputs hash identically; any change to a
    relevant input changes the hash, so a cached row can be recognised as stale
    even when no invalidation hook fired.

    ``manifest_version`` alone is not sufficient: since capabilities began
    honouring per-organization inputs, two tenants on the same manifest can hold
    legitimately different profiles.

    Determinism matters more than brevity here — ``sort_keys`` plus fixed
    separators means dict ordering, whitespace and Python version cannot change
    the digest. Nothing actor-specific is included: the profile is
    organization-level. Nothing secret or personal is included either; an
    override is configuration, and country and a boolean are not identifying.
    """
    payload = json.dumps(
        {
            "plan_code": plan_code,
            "state": state,
            "country": (country or "").upper() or None,
            "email_verified": bool(email_verified),
            "enterprise_override": enterprise_override or {},
            "manifest_version": manifest_version,
            # Layer 6: the configured screenshot state is a resolution input, so a
            # settings change must produce a different hash and recompute.
            "screenshot_config": screenshot_config or "",
        },
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_for(db: Session, org: CommercialOrganization,
                  sub: Optional[CommercialSubscription]) -> str:
    """Current snapshot for a tenant, from live rows.

    ``sub`` is optional because the internal tenant has an identity but no
    subscription. The derivation must mirror :func:`compute` exactly — if the two
    disagreed about the plan, every read would miss the cache and recompute.
    """
    if sub is None:
        plan_code = manifests.INTERNAL_PLAN
        state = org.commercial_state or "active"
    else:
        plan_code = sub.plan_code or "verified"
        state = org.commercial_state or sub.state or "trial_active"
    return _snapshot_hash(
        plan_code=plan_code,
        state=state,
        country=org.country,
        email_verified=_resolve_email_verified(db, org),
        enterprise_override=sub.enterprise_override if sub is not None else None,
        manifest_version=manifests.manifest_for(plan_code).version,
        # Mirror compute(): the configured screenshot state is a resolution input,
        # so a settings change makes this hash differ and the cached row recompute.
        screenshot_config=_screenshot_fingerprint(_screenshot_config(db, org)),
    )


def _manifest_is_current(row: EntitlementsCache) -> bool:
    """Whether a cached profile was computed against the plan's current manifest.

    Packaging changes ship as a new manifest version, which is a code deploy
    rather than a data event — so nothing about it touches the cache. Without
    this check a warm row keeps serving the previous capability map for the whole
    TTL after a release.

    It also handles the rows that predate the capability layer entirely: they
    carry no ``manifest_version`` at all, so they compare unequal and recompute
    on first read. That matters because ``capability()`` fails *closed* on a
    missing key while ``can()`` fails open — serving one of those rows to a
    capability check would deny everything.

    Compared against ``manifest_for()`` rather than ``CURRENT_VERSIONS`` directly
    so the unknown-plan fallback matches what ``compute()`` writes; comparing
    against the raw map would leave an unrecognised plan permanently stale and
    recomputing on every single read.
    """
    profile = row.profile_json or {}
    return profile.get("manifest_version") == manifests.manifest_for(row.plan_code).version


#: Which row of the legacy nine-flag matrix a plan uses, where that differs from
#: the plan code itself. The internal tenant is Sovereign-equivalent, and
#: ``_PLAN_FEATURES`` has no ``internal`` row — without this it would silently
#: fall through to Verified, and ``_mirror_legacy_restrictions`` would then drag
#: the internal capability map back down to Verified with it.
#:
#: Mapped rather than duplicated so the two never drift: change Sovereign and the
#: vendor tenant follows.
_LEGACY_PLAN_ALIAS: Dict[str, str] = {manifests.INTERNAL_PLAN: "sovereign"}


def _base_profile(plan_code: str) -> Dict[str, Any]:
    """Starting point for one tenant: the plan's raw grants, before any modifier.

    ``capabilities`` joins ``features`` and ``limits`` here rather than being
    attached at the end, so the same modifier chain narrows all three. Before
    this, capabilities were a pure function of plan_code and every layer below
    was invisible to them — a suspended tenant in a sanctioned jurisdiction got
    the same capability map as a healthy one.
    """
    legacy = _LEGACY_PLAN_ALIAS.get(plan_code, plan_code)
    return {
        "features":     dict(_PLAN_FEATURES.get(legacy, _PLAN_FEATURES["verified"])),
        "limits":       dict(_PLAN_LIMITS.get(legacy, _PLAN_LIMITS["verified"])),
        "capabilities": manifests.capabilities_for(plan_code),
    }


def _apply_suspension(profile: Dict[str, Any], state: str) -> Dict[str, Any]:
    """Suspended / past_due / trial_expired tenants lose write features."""
    if state in {"suspended", "trial_expired", "trial_closed", "closed", "cancelled"}:
        for k in ("data_export", "api_access", "scim"):
            profile["features"][k] = False
        # Limits unchanged — historical reads still allowed for retention window.
    # `past_due` deliberately restricts nothing. The specification says a tenant
    # in grace "retains current entitlements ... unless risk policy says
    # otherwise", and Temveris has no documented risk policy, so the previous
    # withdrawal of api_access here had nothing standing behind it. Past-due is
    # not a downgrade; suspension is the state that withdraws capability.
    return profile


def _apply_enterprise_overrides(profile: Dict[str, Any], override: Optional[dict]) -> Dict[str, Any]:
    if not override:
        return profile
    f_over = override.get("features") or {}
    l_over = override.get("limits") or {}
    profile["features"].update({k: bool(v) for k, v in f_over.items()})
    profile["limits"].update(l_over)

    # A contract may name capabilities as well. Same established rule as the two
    # above — an explicit per-tenant value wins — extended to the newer
    # vocabulary. Unknown keys and decisions are ignored rather than trusted, so
    # a malformed overlay cannot widen access or inject a state the manifest
    # validator would have rejected.
    c_over = override.get("capabilities") or {}
    accepted = {k: v for k, v in c_over.items()
                if k in manifests.FEATURES and v in manifests.DECISIONS}
    profile["capabilities"].update(accepted)

    # Remember which keys the contract set explicitly. The overlay is layer 5 in
    # the specification's resolution order, above the derived legacy mirror that
    # runs later — without this the mirror silently reverted an explicit grant
    # for any capability backed by one of the nine legacy flags, which is
    # precisely the set most likely to be negotiated (SSO, legal hold, API,
    # compliance reporting). Rejected keys are deliberately NOT recorded, so a
    # malformed overlay still cannot shield anything from narrowing.
    profile.setdefault("_explicit_capabilities", set()).update(accepted)
    return profile


#: The exact capabilities an existing Verified trial is grandfathered on when the
#: seven of them move to Governed+ (Product decision, Phase 3.18). Fixed in code —
#: never read from tenant data — so a marker can only ever re-grant this precise
#: set, nothing wider. workspace.settings.rbac_advanced is deliberately NOT here:
#: it stays All-plan.
GRANDFATHER_TRIAL_CAPS = frozenset({
    "payroll.calculator",
    "reports.advanced",
    "activity.anomaly",
    "leave.approvals",
    "offboarding.governed",
    "time.attendance.policy",
    "time.playback.advanced",
})

#: The opt-in marker on a subscription's enterprise_override JSON. Presence (truthy)
#: means "this specific existing trial is grandfathered". New tenants never get it.
GRANDFATHER_MARKER = "grandfather_trial_capabilities"


def _apply_trial_grandfather(profile: Dict[str, Any], override: Optional[dict],
                             state: str) -> Dict[str, Any]:
    """Re-grant the seven retiered capabilities to a grandfathered trial (G2).

    Deliberately narrow and state-conditional, and separate from the enterprise
    overlay so contractual overrides keep their unconditional semantics:

    * Only fires when ``state == "trial_active"``. The moment the tenant converts
      (``active``), expires (``trial_expired``) or moves to any other state, this
      does nothing — the grandfather auto-expires with the lifecycle, no cleanup
      job required.
    * Only fires when the marker is present and truthy on the override. Marker-less
      tenants — every new Verified tenant, and every Governed/Sovereign/Enterprise/
      Internal tenant — are untouched.
    * Only ever sets the fixed ``GRANDFATHER_TRIAL_CAPS`` to ALLOW. The capability
      names are a code constant, so a malformed or hostile marker cannot widen
      access to anything else, and privacy/globally-denied keys are never in the
      set (and are skipped defensively).

    Marks the seven explicit so the later legacy mirror does not revert them,
    matching the enterprise-overlay protection.
    """
    if state != "trial_active":
        return profile
    if not (isinstance(override, dict) and override.get(GRANDFATHER_MARKER)):
        return profile

    caps = profile["capabilities"]
    for key in GRANDFATHER_TRIAL_CAPS:
        if key in caps and key not in manifests.GLOBALLY_DENIED:
            caps[key] = manifests.ALLOW
    profile.setdefault("_explicit_capabilities", set()).update(GRANDFATHER_TRIAL_CAPS)
    return profile


# ── Layer 6: workspace configuration (spec §3) ──────────────────────────────
# The screenshot capabilities ship as CONFIGURATION_REQUIRED — the plan makes them
# available but they must not count as active until the workspace has actually set
# them up. Layer 6 reads the EXISTING screenshot settings through the EXISTING
# settings cascade and upgrades CONFIGURATION_REQUIRED to ALLOW where a capability
# is genuinely configured. It never touches DENY, so privacy, jurisdiction and
# lifecycle still win; and it only ever affects these three keys.
#
# policy_controls has no backing setting, so it is deliberately never upgraded
# (Product decision, 3.20) — it stays CONFIGURATION_REQUIRED.
_SCREENSHOT_SETTING_KEYS = ("screenshot_enabled", "screenshot_interval",
                            "screenshot_blur")
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})


def _screenshot_config(db: Session, org: CommercialOrganization) -> Dict[str, bool]:
    """Effective screenshot configuration for a tenant — {capture, masking}.

    * ``capture``  configured when ``screenshot_enabled`` is effectively true AND
      ``screenshot_interval`` is a valid positive integer.
    * ``masking``  configured when ``screenshot_blur`` is effectively true (the
      masking mode is actually turned on).

    Reads the org-tier effective settings via ``settings.scope.org_settings`` — the
    established cascade, no new settings and no new store. Fails closed: anything
    unresolvable (no operational org, missing table, bad value) leaves a capability
    NOT configured, so it stays CONFIGURATION_REQUIRED rather than being granted.
    """
    result = {"capture": False, "masking": False}
    op_id = getattr(org, "operational_org_id", None)
    if not op_id:
        return result
    try:
        from app.settings import scope as settings_scope       # noqa: PLC0415
        from app.settings.router import DEFAULTS as _S          # noqa: PLC0415
        defaults = {k: _S[k] for k in _SCREENSHOT_SETTING_KEYS if k in _S}
        merged = settings_scope.org_settings(db, op_id, defaults)

        enabled = str(merged.get("screenshot_enabled", "")).strip().lower() in _TRUE_STRINGS
        interval_raw = str(merged.get("screenshot_interval", "")).strip()
        interval_ok = interval_raw.isdigit() and int(interval_raw) > 0
        blur = str(merged.get("screenshot_blur", "")).strip().lower() in _TRUE_STRINGS

        result["capture"] = enabled and interval_ok
        result["masking"] = blur
    except Exception:                                           # noqa: BLE001
        _log.debug("screenshot Layer 6 config resolution failed for org=%s",
                   getattr(org, "id", None), exc_info=True)
    return result


def _screenshot_fingerprint(config: Dict[str, bool]) -> str:
    """Canonical cache-hash input for the screenshot configuration.

    Uses the DERIVED configured booleans, not the raw setting values, so an
    interval tweak that does not change validity never churns the cache — only a
    real change to whether capture/masking is configured does.
    """
    return f"cap={bool(config.get('capture'))}:mask={bool(config.get('masking'))}"


def _apply_screenshot_layer6(profile: Dict[str, Any],
                             config: Dict[str, bool]) -> Dict[str, Any]:
    """Upgrade configured screenshot capabilities CONFIGURATION_REQUIRED → ALLOW.

    Only ever promotes a capability that is *currently* CONFIGURATION_REQUIRED, so
    it cannot override a DENY set by the manifest (privacy), an overlay, or a later
    jurisdiction/lifecycle layer. Only the two configurable keys are considered;
    policy_controls is never touched.
    """
    caps = profile["capabilities"]
    if config.get("capture") and \
            caps.get("evidence.screenshots.capture") == manifests.CONFIGURATION_REQUIRED:
        caps["evidence.screenshots.capture"] = manifests.ALLOW
    if config.get("masking") and \
            caps.get("evidence.screenshots.masking") == manifests.CONFIGURATION_REQUIRED:
        caps["evidence.screenshots.masking"] = manifests.ALLOW
    return profile


def _apply_jurisdiction(profile: Dict[str, Any], country: Optional[str]) -> Dict[str, Any]:
    # Sanctioned jurisdictions → all expansion features off. The app should
    # never have a tenant whose org is in this list (the signup router
    # would have routed them to "blocked"), but defense-in-depth.
    if country and country.upper() in constants.SANCTIONED_COUNTRIES:
        for k in profile["features"]:
            profile["features"][k] = False
        # Same established rule, applied to the newer vocabulary: a sanctioned
        # tenant keeps nothing. Denying every capability is the direct analogue
        # of switching off every feature, and errs toward the restrictive side.
        for k in profile["capabilities"]:
            profile["capabilities"][k] = manifests.DENY
    return profile


def _email_unverified_restriction(profile: Dict[str, Any], email_verified: bool) -> Dict[str, Any]:
    """§I-6 — expansion actions are gated on email verification."""
    if not email_verified:
        profile["features"]["data_export"] = False
        # invite endpoint will check restrictions.email_unverified directly
    return profile


#: Lifecycle states that leave capability grants untouched. Trial and active are
#: the full plan profile; past-due/grace retain it; cancellation pending retains
#: it until effective_at, which the existing downgrade scheduler owns.
_LIFECYCLE_FULL = frozenset({
    "trial_active", "trial_ending", "active",
    "past_due", "grace_period", "cancelled_pending_end",
})

#: Premium work stops, history stays readable.
_LIFECYCLE_RESTRICTED = frozenset({"suspended", "cancelled"})

#: No normal workspace access; essential account/billing/recovery routes only.
_LIFECYCLE_CLOSED = frozenset({"closed"})


def _apply_lifecycle(profile: Dict[str, Any], state: str) -> Dict[str, Any]:
    """Narrow capabilities according to the subscription's lifecycle state.

    Operates on ``capabilities`` only. The nine legacy ``features`` keep the
    behaviour they have always had (``_apply_suspension`` above) so nothing that
    reads them changes meaning.

    Three groups, from ``manifests.capability_nature``:

    * essential   — untouched in every state. The specification's essential-route
                    invariant: billing, payment recovery and account security
                    must not disappear because a subscription lapsed. A customer
                    locked out of billing cannot pay their way back in.
    * historical  — READ_ONLY when suspended or cancelled ("provide configured
                    read-only/export/recovery state"). Records already captured
                    stay readable; nothing new is written.
    * operational — DENY. Creating, editing, approving, configuring, collecting
                    and executing all stop.

    Narrowing only: a state can never widen a grant the manifest withheld, so a
    suspended Verified tenant cannot reach a Governed capability by any path.

    States outside the three sets — ``trial_expired``, ``trial_closed``,
    ``demo``, ``sandbox``, ``enterprise_pending``, ``manual_review`` — are left
    alone deliberately. The approved lifecycle policy covers the seven states the
    specification names; inventing capability behaviour for the rest is a product
    decision, and they keep exactly the behaviour they have today. (They are
    still narrowed indirectly wherever ``_apply_suspension`` switches off a
    legacy flag that a capability mirrors.)
    """
    if state in _LIFECYCLE_FULL:
        return profile
    restricted = state in _LIFECYCLE_RESTRICTED
    closed = state in _LIFECYCLE_CLOSED
    if not (restricted or closed):
        return profile

    capabilities = profile["capabilities"]
    for key in list(capabilities):
        if capabilities[key] == manifests.DENY:
            continue
        nature = manifests.capability_nature(key)
        if nature in ("essential", "undecided"):
            continue
        if restricted and nature == "historical":
            capabilities[key] = manifests.READ_ONLY
        else:
            capabilities[key] = manifests.DENY
    return profile


def _mirror_legacy_restrictions(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the two vocabularies from contradicting each other.

    Several capabilities are declared (via ``Feature.legacy_key``) to *be* one of
    the nine older boolean flags under a finer-grained name. Once the layers
    above have switched such a flag off, the capability naming the same thing
    cannot still say ``allow`` — the profile would assert two different answers
    to one question.

    This is a consistency invariant, not a policy: it introduces no rule of its
    own, it only stops the newer vocabulary from disagreeing with a decision an
    established layer already made. It runs last so it observes the final state
    of ``features``.

    It narrows only. A capability whose legacy flag is still true keeps whatever
    the manifest said — importantly including ``configuration_required``, which
    must never be promoted to ``allow`` by a mirroring step.

    Two consequences worth naming, because they fall out of the data rather than
    from any decision made here:

    * Email verification restricts ``data_export``, which no capability names
      (verified against ``Feature.legacy_key``). So that layer has no capability
      effect at all — correctly, not by omission.
    * Suspension restricts ``data_export``, ``api_access`` and ``scim``. Only
      ``api_access`` has a counterpart (``integrations.api``), so only that one
      is narrowed. What suspension should do to the ~30 capabilities with no
      legacy counterpart is an open product question and is deliberately NOT
      answered here — see the Phase 2.7 report.
    """
    features = profile["features"]
    capabilities = profile["capabilities"]
    explicit = profile.get("_explicit_capabilities") or set()
    for key, feature in manifests.FEATURES.items():
        legacy = feature.legacy_key
        if legacy is None or key not in capabilities:
            continue
        if key in explicit:
            # An explicit contractual decision outranks a derived one.
            continue
        if features.get(legacy) is False and capabilities[key] != manifests.DENY:
            capabilities[key] = manifests.DENY
    return profile


def compute(
    db: Session,
    organization_id: str,
    *,
    email_verified: bool = True,
) -> Dict[str, Any]:
    """Compute the full entitlement object for an organization.

    Side effects: writes/refreshes the entitlements cache row. Caller may
    persist or just inspect.

    ``email_verified`` is DEPRECATED and ignored — the value is an
    organization-level fact resolved from the owner, not a caller opinion. It
    remains in the signature only so existing call sites keep working. One
    organization has exactly one authoritative profile.
    """
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    sub = db.query(CommercialSubscription).filter_by(organization_id=organization_id).one_or_none()

    # Temveris's own tenant has a commercial *identity* but deliberately no
    # subscription: it is not a customer, has no Stripe objects and is excluded
    # from every billing path by the existing guards. It still resolves through
    # this function rather than around it, so the manifest validator's global
    # denials (Apps History, Website Categories) apply to it like anyone else.
    internal = org is not None and _is_internal_org(db, org)

    if not org or (not sub and not internal):
        # Fall back to verified/closed so app code stays safe even for an
        # unknown tenant ID. We don't raise — caller may legitimately ask
        # about a tenant that's mid-provisioning.
        return _empty_profile(organization_id, plan="verified", state="manual_review")

    if internal:
        plan_code = manifests.INTERNAL_PLAN
        state = org.commercial_state or "active"
    else:
        plan_code = sub.plan_code or "verified"
        state = org.commercial_state or sub.state or "trial_active"
    override = sub.enterprise_override if sub is not None else None
    trial_overlay = state in {"trial_active", "trial_ending"}

    # The caller's `email_verified` argument is deliberately not used. It is an
    # organization-level fact, and letting each caller supply its own value while
    # the cache is keyed on organization_id alone is what allowed one caller to
    # poison the profile for every other. See _resolve_email_verified.
    email_verified = _resolve_email_verified(db, org)

    # ZT-COM-BILL-001 §K3: a trial grants exactly the selected plan's
    # entitlements — nothing more. Customers switch the trial plan to compare
    # tiers instead. `trial_overlay` below is retained purely as an
    # "is in trial" flag on the response contract; it no longer adds features.
    profile = _base_profile(plan_code)
    profile = _apply_enterprise_overrides(profile, override)
    # G2 grandfather: existing Verified trials keep the seven retiered
    # capabilities, but ONLY while they are still on their trial. This runs after
    # the contractual overlay and before jurisdiction/lifecycle, so a sanctioned
    # or suspended tenant still loses them. See _apply_trial_grandfather.
    profile = _apply_trial_grandfather(profile, override, state)
    # Layer 6 — configured screenshot capabilities become ALLOW. Runs before
    # jurisdiction/suspension/lifecycle so those can still narrow a configured but
    # sanctioned/suspended tenant back down. The fingerprint is folded into the
    # snapshot hash below so a settings change self-invalidates the cache.
    screenshot_config = _screenshot_config(db, org)
    profile = _apply_screenshot_layer6(profile, screenshot_config)
    profile = _apply_jurisdiction(profile, org.country)
    profile = _apply_suspension(profile, state)
    profile = _email_unverified_restriction(profile, email_verified)
    # Last, so it sees the final state of `features` after every layer above.
    profile = _mirror_legacy_restrictions(profile)
    # Then lifecycle, which narrows capabilities only and never widens.
    profile = _apply_lifecycle(profile, state)

    computed_at = _now()
    expires_at = computed_at + timedelta(seconds=constants.ENTITLEMENTS_CACHE_TTL_SECONDS)

    # ── Capability layer (Phase 2) ────────────────────────────────────────────
    # Added ALONGSIDE `features`, never in place of it. `features` remains the 9
    # coarse booleans every existing caller and test depends on; `capabilities`
    # is the fine-grained, namespaced vocabulary from the plan manifest, and
    # carries richer decisions than a bool can (deny / read_only /
    # configuration_required). Nothing enforces on it yet — that is a later
    # phase — so this is a pure addition to the response.
    manifest = manifests.manifest_for(plan_code)

    # Internal bookkeeping from the overlay layer; never part of the response or
    # the cached document.
    profile.pop("_explicit_capabilities", None)

    obj = {
        "tenant_id": organization_id,
        "plan": plan_code,
        "commercial_state": state,
        "trial_overlay": trial_overlay,
        "features": profile["features"],
        "limits": profile["limits"],
        "capabilities": profile["capabilities"],
        "manifest_version": manifest.version,
        "snapshot_hash": _snapshot_hash(
            plan_code=plan_code, state=state, country=org.country,
            email_verified=email_verified,
            enterprise_override=override,
            manifest_version=manifest.version,
            screenshot_config=_screenshot_fingerprint(screenshot_config)),
        "restrictions": {
            "email_unverified": not email_verified,
            "billing_required": state in {"past_due", "grace_period"},
            "manual_review_required": state == "manual_review",
        },
        "computed_at": computed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    # Write-through cache.
    row = db.query(EntitlementsCache).filter_by(organization_id=organization_id).one_or_none()
    # Capture the prior hash BEFORE overwriting the row — it is the only evidence
    # of what the tenant resolved to a moment ago, and the §18 events compare it
    # against the freshly computed hash. Timestamps are never consulted.
    old_hash = (row.profile_json or {}).get("snapshot_hash") if row is not None else None
    if row is None:
        row = EntitlementsCache(
            organization_id=organization_id,
            computed_at=computed_at,
            expires_at=expires_at,
            profile_json=obj,
            plan_code=plan_code,
            commercial_state=state,
        )
        db.add(row)
    else:
        row.computed_at = computed_at
        row.expires_at = expires_at
        row.profile_json = obj
        row.plan_code = plan_code
        row.commercial_state = state
    db.flush()

    # Observability — after the flush so the snapshot is already materialised,
    # and savepoint-isolated so a logging or persistence failure can never affect
    # the value returned or the caller's transaction.
    _emit_snapshot_events(
        db, organization_id=organization_id, plan=plan_code, state=state,
        manifest_version=obj.get("manifest_version"),
        old_hash=old_hash, new_hash=obj.get("snapshot_hash"))

    return obj


def cache_status(db: Session, organization_id: str) -> Dict[str, Any]:
    """Read-only diagnostic view of the entitlement cache for one tenant (§19).

    Reports what a read WOULD encounter, without recomputing or writing anything.
    Reuses the resolver's own freshness helpers so it can never disagree with
    ``get``. ``last_invalidation`` is deliberately ``None``: invalidation deletes
    the row and stores no timestamp, so that history is genuinely unavailable
    rather than fabricated.
    """
    row = (db.query(EntitlementsCache)
             .filter_by(organization_id=organization_id).one_or_none())
    if row is None:
        return {
            "status": "NOT_AVAILABLE",
            "present": False,
            "computed_at": None,
            "expires_at": None,
            "snapshot_hash": None,
            "snapshot_age_seconds": None,
            "ttl_remaining_seconds": None,
            "is_fresh": False,
            "manifest_current": False,
            "last_invalidation": None,
        }
    now = _now()
    computed = _as_utc(row.computed_at) if row.computed_at else None
    expires = _as_utc(row.expires_at) if row.expires_at else None
    fresh = _is_fresh(row.expires_at, now=now)
    manifest_ok = _manifest_is_current(row)
    return {
        # HIT = a read would serve this row; STALE = present but a read recomputes.
        "status": "HIT" if (fresh and manifest_ok) else "STALE",
        "present": True,
        "computed_at": computed.isoformat() if computed else None,
        "expires_at": expires.isoformat() if expires else None,
        "snapshot_hash": (row.profile_json or {}).get("snapshot_hash"),
        "snapshot_age_seconds": (now - computed).total_seconds() if computed else None,
        "ttl_remaining_seconds": (expires - now).total_seconds() if expires else None,
        "is_fresh": fresh,
        "manifest_current": manifest_ok,
        "last_invalidation": None,          # not stored — invalidate() drops the row
    }


def get(db: Session, organization_id: str, *, email_verified: bool = True) -> Dict[str, Any]:
    """Read-through cache lookup. Returns the cached profile when it is still
    valid, else recomputes.

    A cached row is only trusted when it passes all three checks: within TTL,
    computed against the plan's current manifest, and matching the tenant's
    current input snapshot.

    ``email_verified`` is DEPRECATED and ignored. It is retained so existing
    call sites keep working; the value is resolved authoritatively from the
    organization's owner inside :func:`compute`. See
    :func:`_resolve_email_verified` for why it could not stay caller-supplied.
    """
    row = db.query(EntitlementsCache).filter_by(organization_id=organization_id).one_or_none()
    if row and row.profile_json and _is_fresh(row.expires_at) and _manifest_is_current(row):
        # Integrity check (spec 3.1). The row survives TTL and manifest checks;
        # confirm it was also computed from the tenant's *current* inputs before
        # trusting it. This costs two primary-key lookups on a cache hit, which
        # is a deliberate trade: it makes the cache self-validating, so a missed
        # invalidation call site can no longer produce a wrong answer. A miss
        # costs nothing extra — compute() reads the same two rows anyway.
        org = (db.query(CommercialOrganization)
                 .filter_by(id=organization_id).one_or_none())
        sub = (db.query(CommercialSubscription)
                 .filter_by(organization_id=organization_id).one_or_none())
        # `sub` may legitimately be absent for the internal tenant; `org` may not.
        if org is not None and (sub is not None or _is_internal_org(db, org)):
            if row.profile_json.get("snapshot_hash") == _snapshot_for(db, org, sub):
                return row.profile_json
    return compute(db, organization_id, email_verified=email_verified)


def invalidate(db: Session, organization_id: str) -> None:
    """Drop the cache so the next lookup recomputes.

    Call this on: plan change, state transition, suspension change,
    enterprise override update, add-on change, email verification flip.
    """
    row = db.query(EntitlementsCache).filter_by(organization_id=organization_id).one_or_none()
    if row is not None:
        db.delete(row)
        db.flush()


def can(db: Session, organization_id: str, feature: str, *, email_verified: bool = True) -> bool:
    """Application-facing predicate. App code uses this; never plan_code."""
    ent = get(db, organization_id, email_verified=email_verified)
    return bool(ent.get("features", {}).get(feature, False))


def capability(db: Session, organization_id: str, feature_key: str,
               *, email_verified: bool = True) -> str:
    """The decision this tenant's plan makes about a canonical capability.

    Returns one of ``manifests.ALLOW / DENY / READ_ONLY / CONFIGURATION_REQUIRED``.
    This is the manifest-backed counterpart to :func:`can`, which stays as-is for
    the 9 coarse legacy flags.

    Nothing enforces on this yet. It exists so a later phase can move a call site
    from a plan-name check to a capability check without also having to build the
    resolution path at the same time.
    """
    ent = get(db, organization_id, email_verified=email_verified)
    caps = ent.get("capabilities") or {}
    # An unknown key denies rather than defaulting open: a typo in a capability
    # name must never widen access.
    return caps.get(feature_key, manifests.DENY)


def permits(db: Session, organization_id: str, feature_key: str,
            *, email_verified: bool = True) -> bool:
    """Boolean form of :func:`capability`.

    CONFIGURATION_REQUIRED is deliberately falsy: the tenant is entitled to the
    capability but it is not switched on, and treating "entitled" as "enabled" is
    exactly the mistake this vocabulary exists to prevent.
    """
    return capability(db, organization_id, feature_key,
                      email_verified=email_verified) in manifests.PERMISSIVE


def within_limit(db: Session, organization_id: str, limit: str, current_value: int) -> bool:
    """Returns True if current_value is within the tenant's limit. None = unlimited."""
    ent = get(db, organization_id)
    cap = ent.get("limits", {}).get(limit)
    if cap is None:
        return True
    return current_value <= cap


def _essential_only_capabilities(plan: str) -> Dict[str, str]:
    """Everything denied except the essentials the plan already grants.

    An unresolvable tenant is not entitled to anything it would have to pay
    for. The essential set is the exception, and it is an exception about
    lifecycle rather than about generosity: billing, payment recovery and
    account security are the routes a tenant uses to *leave* an impaired
    state. An organization parked in ``manual_review`` that cannot reach the
    billing portal has no way to fix itself, which is the opposite of what
    fail-closed is for.

    Essential means lifecycle-invariant, not plan-universal, so each essential
    key keeps whatever decision its plan manifest already made. This never
    grants a capability the plan does not include - ``identity.sso`` stays
    Sovereign+ packaging here exactly as it does everywhere else - and the
    globally denied privacy keys are untouched, both because they are not
    essential and because the manifest validator denies them for every plan.
    """
    granted = manifests.capabilities_for(plan)
    return {
        key: (granted[key] if key in manifests.ESSENTIAL_CAPABILITIES
              else manifests.DENY)
        for key in manifests.FEATURES
    }


def _empty_profile(organization_id: str, plan: str, state: str) -> Dict[str, Any]:
    now = _now()
    return {
        "tenant_id": organization_id,
        "plan": plan,
        "commercial_state": state,
        "trial_overlay": False,
        "features": {f: False for f in constants.ENTITLEMENT_FEATURES},
        "limits": {l: 0 for l in constants.ENTITLEMENT_LIMITS},
        # Shape-compatible with a real profile so callers need no special
        # case. Grants nothing except the plan's own essential capabilities,
        # so an impaired tenant keeps the routes it needs to recover.
        "capabilities": _essential_only_capabilities(plan),
        "manifest_version": None,
        "snapshot_hash": None,
        "restrictions": {
            "email_unverified": True,
            "billing_required": False,
            "manual_review_required": True,
        },
        "computed_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=60)).isoformat(),
    }
