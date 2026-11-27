"""Canonical feature dictionary and versioned plan manifests.

WHAT THIS IS
------------
Two pieces of pure data plus the accessors and validation that keep them honest:

  FEATURES   the canonical dictionary of capability keys. A key names a
             *capability* ("reports.advanced"), never a plan ("is_governed").
  MANIFESTS  one versioned manifest per plan, naming the decision this plan
             makes about every key in the dictionary.

WHAT THIS IS NOT
----------------
It is NOT a second entitlement system. ``app.commercial.entitlements`` remains
the single resolver and the single decision point; this module only supplies the
plan-to-capability table it reads. There is deliberately no DB access, no
caching, no request handling and no side effects here — the same discipline
``app.commercial.state_machines`` follows, and for the same reason: a pure table
can be reviewed by Product as a document rather than read as code.

RELATIONSHIP TO THE EXISTING 9 FEATURE FLAGS
--------------------------------------------
``entitlements._PLAN_FEATURES`` holds 9 coarse booleans that predate this
dictionary. They are still the resolver's ``features`` block and are NOT changed
by this module — existing callers and tests keep the exact behaviour they had.
Where one of those 9 already expresses a capability, the dictionary records it as
``legacy_key`` so the two vocabularies stay traceable to each other instead of
drifting into two competing sets of truth.

PROVENANCE — WHY EVERY GRANT SAYS WHERE IT CAME FROM
-----------------------------------------------------
Deciding that (say) advanced reporting is a Governed feature is a commercial
decision, not an engineering one. So every grant records where it came from, and
nothing here silently invents packaging:

  SHIPPED    mirrors a plan distinction that already exists in
             ``entitlements._PLAN_FEATURES`` today. Encoding it changes nothing.
  SPECIFICATION  stated by the Plan-to-Feature Entitlement Specification.
  POLICY     a standing platform rule (privacy denial, entitled-is-not-enabled).
  UNBUILT    the capability does not exist in this codebase yet, so no plan can
             be given it.
  STATUS_QUO the capability ships today with NO plan gate of any kind, so every
             plan is granted it. This preserves current behaviour exactly. It is
             explicitly NOT a claim that the capability should stay untiered —
             see ``provisional_keys()``, which lists every one of these for
             Product to rule on before any enforcement phase.

ENTITLED IS NOT ENABLED
-----------------------
CONFIGURATION_REQUIRED is a first-class decision, not a synonym for allow.
Buying a plan that includes screenshot capture makes the capability *available*;
it must not start capturing. Anything privacy-impacting resolves to
CONFIGURATION_REQUIRED so that turning it on stays a separate, deliberate act.
"""
from __future__ import annotations

from typing import Dict, Iterable, Mapping, NamedTuple, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════════════
# Decisions
# ══════════════════════════════════════════════════════════════════════════════
ALLOW = "allow"
DENY = "deny"
READ_ONLY = "read_only"
CONFIGURATION_REQUIRED = "configuration_required"

DECISIONS = frozenset({ALLOW, DENY, READ_ONLY, CONFIGURATION_REQUIRED})

#: Decisions under which a capability may actually be exercised. READ_ONLY is
#: included because reading is still exercising it; CONFIGURATION_REQUIRED is
#: not, because nothing may happen until it is configured.
PERMISSIVE = frozenset({ALLOW, READ_ONLY})

# Provenance of a grant — see the module docstring.
SHIPPED = "shipped"
SPECIFICATION = "specification"
POLICY = "policy"
UNBUILT = "unbuilt"
STATUS_QUO = "status_quo"

PROVENANCES = frozenset({SHIPPED, SPECIFICATION, POLICY, UNBUILT, STATUS_QUO})

# Implementation state in THIS codebase, established by direct inspection.
# Kept separate from the grant: what a plan is entitled to and what the product
# has actually built are different questions, and conflating them is how a plan
# ends up advertising a capability that does not exist.
IMPLEMENTED = "implemented"
PARTIAL = "partial"
ABSENT = "absent"

IMPLEMENTATION_STATES = frozenset({IMPLEMENTED, PARTIAL, ABSENT})

PLANS: Tuple[str, ...] = ("verified", "governed", "sovereign", "enterprise")

#: Temveris's own vendor tenant. Deliberately NOT a member of ``PLANS``: that
#: tuple is the commercial catalogue, and several tests and reconciliation
#: checks iterate it against ``entitlements._PLAN_FEATURES`` and the billing
#: plan seed, neither of which has — or should have — an internal entry.
#:
#: The internal tenant is not a customer. It has no price, no Stripe objects and
#: no subscription; it needs an entitlement identity and nothing else. Giving it
#: its own manifest rather than aliasing Sovereign keeps it distinguishable in
#: audit events and admin views from a tenant that actually pays.
INTERNAL_PLAN = "internal"


# ══════════════════════════════════════════════════════════════════════════════
# Feature dictionary
# ══════════════════════════════════════════════════════════════════════════════
class Feature(NamedTuple):
    key: str
    display_name: str
    category: str
    description: str
    #: One of the 9 pre-existing ``entitlements._PLAN_FEATURES`` keys that already
    #: expresses this capability, or None. Several fine-grained keys can share a
    #: coarse legacy key — that coarseness is the reason this dictionary exists.
    legacy_key: Optional[str]
    #: Whether Temveris has actually built it. Not a permission.
    implementation: str
    #: Privacy weight. "critical" means it observes a person's activity in a way
    #: that needs an explicit enabling decision, never a plan purchase alone.
    privacy: str
    #: True when being entitled must still not switch the capability on.
    configuration_required: bool


def _f(key, display_name, category, description, *, legacy_key=None,
       implementation=IMPLEMENTED, privacy="standard",
       configuration_required=False) -> Feature:
    return Feature(key, display_name, category, description, legacy_key,
                   implementation, privacy, configuration_required)


#: The canonical dictionary. Scope note: these are the capabilities the
#: specification names, and they map almost one-to-one onto the shipped admin
#: navigation (frontend/src/components/AdminSidebar.jsx). Internal subsystems
#: that are not sold as plan features (kairos, risk, wec, proof_of_work, breaks,
#: schedule) deliberately have no key — inventing packaging for them would be a
#: product decision, and they are listed in the Phase 2 report instead.
FEATURES: Dict[str, Feature] = {f.key: f for f in (
    # ── Dashboard ────────────────────────────────────────────────────────────
    _f("dashboard.overview.view", "Overview dashboard", "dashboard",
       "The admin Overview tab and its analytics.", legacy_key="dashboards"),

    # ── Team ─────────────────────────────────────────────────────────────────
    _f("team.directory.view", "Team directory", "team",
       "View the people in the workspace."),
    _f("team.directory.manage", "Manage team directory", "team",
       "Invite, edit and deactivate people. Seat limits apply separately."),

    # ── Evidence ─────────────────────────────────────────────────────────────
    _f("evidence.screenshots.view", "View screenshots", "evidence",
       "Review captured screenshot evidence.", privacy="sensitive"),
    _f("evidence.screenshots.capture", "Capture screenshots", "evidence",
       "Collect screenshot evidence from the desktop agent.",
       privacy="critical", configuration_required=True),
    _f("evidence.screenshots.policy_controls", "Screenshot policy controls", "evidence",
       "Configure capture intervals, scope and retention of screenshots.",
       implementation=PARTIAL, privacy="critical", configuration_required=True),
    _f("evidence.screenshots.masking", "Screenshot masking", "evidence",
       "Redact or blur sensitive regions of captured screenshots.",
       implementation=PARTIAL, privacy="critical", configuration_required=True),
    _f("evidence.screenshots.jurisdiction", "Jurisdictional capture rules", "evidence",
       "Vary screenshot capture by the worker's jurisdiction.",
       implementation=ABSENT, privacy="critical", configuration_required=True),
    _f("evidence.legal_hold", "Legal hold", "evidence",
       "Preserve records against deletion for litigation or investigation.",
       legacy_key="legal_hold", implementation=PARTIAL, privacy="sensitive"),

    # ── Governance ───────────────────────────────────────────────────────────
    _f("governance.chain_of_custody", "Chain of custody", "governance",
       "Tamper-evident provenance for evidence records.",
       legacy_key="advanced_compliance", implementation=PARTIAL),
    _f("governance.ediscovery", "e-Discovery export", "governance",
       "Structured export of records for legal discovery.", implementation=ABSENT),

    # ── Time ─────────────────────────────────────────────────────────────────
    _f("time.attendance.view", "Attendance and timesheets", "time",
       "View attendance records and timesheets.", legacy_key="time_tracking"),
    _f("time.attendance.policy", "Attendance policy", "time",
       "Configure attendance rules, cutoffs and absence handling."),
    _f("time.playback.basic", "Time playback", "time",
       "Replay a worker's day at standard fidelity.", privacy="sensitive"),
    _f("time.playback.advanced", "Advanced time playback", "time",
       "Higher-fidelity playback with richer per-interval detail.",
       implementation=PARTIAL, privacy="sensitive"),

    # ── Activity ─────────────────────────────────────────────────────────────
    _f("activity.log.view", "Activity log", "activity",
       "The per-worker activity timeline.", legacy_key="audit_logs",
       privacy="sensitive"),
    _f("activity.anomaly", "Activity anomaly detection", "activity",
       "Flag unusual activity patterns for review.",
       implementation=PARTIAL, privacy="sensitive"),

    # ── Reports ──────────────────────────────────────────────────────────────
    _f("reports.standard", "Standard reports", "reports",
       "The core reporting set."),
    _f("reports.advanced", "Advanced reports", "reports",
       "Deeper analytical reporting beyond the core set."),
    _f("reports.compliance", "Compliance reports", "reports",
       "Reporting intended to evidence regulatory compliance.",
       legacy_key="advanced_compliance"),

    # ── Offboarding ──────────────────────────────────────────────────────────
    _f("offboarding.standard", "Offboarding", "offboarding",
       "Standard leaver workflow and access revocation."),
    _f("offboarding.governed", "Governed offboarding", "offboarding",
       "Offboarding with reconciliation, attestation and audit evidence."),
    _f("offboarding.legal_hold", "Offboarding legal hold", "offboarding",
       "Retain a leaver's records under legal hold.", legacy_key="legal_hold"),

    # ── Leave ────────────────────────────────────────────────────────────────
    _f("leave.manage", "Leave management", "leave",
       "Configure leave types, balances and policies."),
    _f("leave.approvals", "Leave approvals", "leave",
       "Review and decide leave requests."),
    _f("leave.jurisdiction", "Jurisdictional leave rules", "leave",
       "Leave entitlements that vary by country or region.", implementation=ABSENT),

    # ── Holidays ─────────────────────────────────────────────────────────────
    _f("holidays.primary", "Public holidays", "holidays",
       "A single public-holiday calendar for the workspace."),
    _f("holidays.multicountry", "Multi-country holidays", "holidays",
       "Separate holiday calendars per country.", implementation=PARTIAL),

    # ── Finance ──────────────────────────────────────────────────────────────
    _f("payroll.calculator", "Payroll calculator", "finance",
       "Derive payable hours and cost from tracked time."),
    _f("billing.portal", "Billing portal", "finance",
       "Manage the subscription, invoices and payment method."),

    # ── Workspace ────────────────────────────────────────────────────────────
    _f("workspace.settings.core", "Workspace settings", "workspace",
       "Company identity, localisation, branding and teams."),
    _f("workspace.settings.rbac_advanced", "Advanced access control", "workspace",
       "Custom roles, permission matrix and access governance.",
       implementation=PARTIAL),

    # ── Identity ─────────────────────────────────────────────────────────────
    _f("identity.mfa", "Multi-factor authentication", "identity",
       "Second-factor authentication for workspace accounts.",
       implementation=PARTIAL),
    _f("identity.sso", "Single sign-on", "identity",
       "Federated sign-in through the customer's identity provider.",
       legacy_key="sso", implementation=PARTIAL),

    # ── Integrations ─────────────────────────────────────────────────────────
    _f("integrations.api", "Customer API", "integrations",
       "Programmatic access to workspace data.",
       legacy_key="api_access", implementation=ABSENT),
    _f("integrations.webhooks", "Outbound webhooks", "integrations",
       "Push workspace events to a customer endpoint.", implementation=ABSENT),

    # ── Productivity (globally denied — see GLOBALLY_DENIED) ────────────────
    _f("productivity.apps_history", "Apps History", "productivity",
       "Per-worker history of applications used.",
       implementation=IMPLEMENTED, privacy="critical", configuration_required=True),
    _f("productivity.website_categories", "Website Categories", "productivity",
       "Categorised history of websites visited.",
       implementation=IMPLEMENTED, privacy="critical", configuration_required=True),

)}


# ══════════════════════════════════════════════════════════════════════════════
# Standing platform policy
# ══════════════════════════════════════════════════════════════════════════════
#: Capabilities denied to EVERY plan regardless of what a manifest says, until an
#: explicit Product/Privacy decision changes the policy. These two observe an
#: individual's application and browsing activity, which is a different class of
#: thing from a premium feature — no amount of money buys them.
#:
#: This is enforced structurally: ``_validate`` rejects any manifest that grants
#: one, so the denial cannot be lost to a careless edit.
#:
#: NOTE: this governs ENTITLEMENT only. The /browser/* endpoints and the
#: collection path remain exactly as they are; switching them off is enforcement
#: work for a later phase, not a manifest change.
GLOBALLY_DENIED = frozenset({
    "productivity.apps_history",
    "productivity.website_categories",
})


# ══════════════════════════════════════════════════════════════════════════════
# Capability nature — the input to lifecycle resolution
# ══════════════════════════════════════════════════════════════════════════════
# What a capability *is* determines what a non-active subscription state does to
# it. Held here as data, next to the dictionary it describes, and consumed by the
# one resolver; this is not a second entitlement system.
#
# Three natures, and everything is exactly one of them:

#: Survive every lifecycle state. The specification's essential-route invariant:
#: "Billing, payment recovery, account security, support and legally required
#: export/recovery routes must not disappear merely because the commercial
#: subscription is suspended or canceled." A customer who cannot reach billing
#: cannot pay their way out of suspension.
ESSENTIAL_CAPABILITIES = frozenset({
    "billing.portal",
    "identity.mfa",
    "identity.sso",
})

#: Genuinely historical viewing — records that already exist and stay readable
#: while the subscription is impaired. These become READ_ONLY under suspension
#: and cancellation, per "provide configured read-only/export/recovery state".
#:
#: Membership is deliberately narrow. READ_ONLY is not a softer DENY: a
#: capability belongs here only if exercising it reads history and writes
#: nothing.
HISTORICAL_CAPABILITIES = frozenset({
    "dashboard.overview.view",
    "team.directory.view",
    "evidence.screenshots.view",
    "time.attendance.view",
    "time.playback.basic",
    "time.playback.advanced",
    "activity.log.view",
    "reports.standard",
    "reports.advanced",
    "reports.compliance",
    "governance.chain_of_custody",
})

#: Capabilities whose lifecycle behaviour is NOT settled and which must not be
#: given one by inference. Left at whatever the manifest says, and reported
#: rather than guessed. Empty since the separate messaging-product entitlement
#: was removed; kept so a future undecided capability has a home.
LIFECYCLE_UNDECIDED: frozenset = frozenset()


def capability_nature(key: str) -> str:
    """``essential`` | ``historical`` | ``undecided`` | ``operational``.

    Everything not named above is operational — creating, editing, approving,
    configuring, collecting or executing — and is denied when the subscription
    cannot support premium work.
    """
    if key in ESSENTIAL_CAPABILITIES:
        return "essential"
    if key in HISTORICAL_CAPABILITIES:
        return "historical"
    if key in LIFECYCLE_UNDECIDED:
        return "undecided"
    return "operational"


# ══════════════════════════════════════════════════════════════════════════════
# Manifests
# ══════════════════════════════════════════════════════════════════════════════
class Grant(NamedTuple):
    decision: str
    provenance: str


class Manifest(NamedTuple):
    version: str
    plan_code: str
    effective_from: str
    grants: Mapping[str, Grant]

    def decision(self, feature_key: str) -> str:
        g = self.grants.get(feature_key)
        return g.decision if g else DENY

    def allows(self, feature_key: str) -> bool:
        return self.decision(feature_key) in PERMISSIVE


def _grants(**by_provenance: Iterable[Tuple[str, str]]) -> Dict[str, Grant]:
    """Build a grant table grouped by provenance, so every entry is attributed.

    Usage mirrors how the table reads on the page: each keyword is a provenance,
    each value a sequence of ``(feature_key, decision)``.
    """
    out: Dict[str, Grant] = {}
    for provenance, entries in by_provenance.items():
        for key, decision in entries:
            out[key] = Grant(decision, provenance)
    return out


# Capabilities that ship today with no plan gate whatsoever. Granting them to
# every plan is what preserves current behaviour; it is not a claim that they
# should remain untiered. Product decides that before any enforcement phase.
_UNGATED_TODAY = (
    "dashboard.overview.view",
    "team.directory.view",
    "team.directory.manage",
    "evidence.screenshots.view",
    "time.attendance.view",
    "time.playback.basic",
    "activity.log.view",
    "reports.standard",
    "offboarding.standard",
    "leave.manage",
    "holidays.primary",
    "billing.portal",
    "workspace.settings.core",
    "workspace.settings.rbac_advanced",
    "identity.mfa",
)

# Privacy-impacting capabilities that a plan may include but must never switch
# on. Available on every plan today, so the availability itself is status quo;
# the CONFIGURATION_REQUIRED decision is the standing policy.
_CONFIG_REQUIRED_EVERYWHERE = (
    ("evidence.screenshots.capture", CONFIGURATION_REQUIRED),
    ("evidence.screenshots.policy_controls", CONFIGURATION_REQUIRED),
    ("evidence.screenshots.masking", CONFIGURATION_REQUIRED),
)

# Not built in this codebase. A plan cannot be entitled to something that does
# not exist, so these are denied everywhere until they are built and packaged.
_NOT_BUILT = (
    ("evidence.screenshots.jurisdiction", DENY),
    ("governance.ediscovery", DENY),
    ("leave.jurisdiction", DENY),
    ("integrations.webhooks", DENY),
)

_PRIVACY_DENIED = tuple((k, DENY) for k in sorted(GLOBALLY_DENIED))


def _plan_manifest(version: str, plan_code: str, *, tiered) -> Manifest:
    """Assemble one plan's manifest from the shared blocks plus its tiered grants.

    ``tiered`` carries only the capabilities where plans actually differ. Every
    other key comes from the shared blocks, which is what keeps the four
    manifests from drifting apart on capabilities nobody intended to tier.
    """
    grants = _grants(
        status_quo=tuple((k, ALLOW) for k in _UNGATED_TODAY),
        policy=_CONFIG_REQUIRED_EVERYWHERE + _PRIVACY_DENIED,
        unbuilt=_NOT_BUILT,
    )
    grants.update(tiered)
    return Manifest(version=version, plan_code=plan_code,
                    effective_from="2026-08", grants=grants)


# The capabilities that genuinely differ by plan today. Each mirrors a
# distinction already present in ``entitlements._PLAN_FEATURES`` — encoding them
# here changes no behaviour, it only writes the existing packaging down in the
# capability vocabulary.
#
# holidays.multicountry is the one SPECIFICATION-sourced tiering: the spec places
# multi-country calendars at Sovereign and above.
_TIERED = {
    "verified": _grants(
        shipped=(("integrations.api", DENY),
                 ("evidence.legal_hold", DENY), ("offboarding.legal_hold", DENY),
                 ("reports.compliance", DENY), ("governance.chain_of_custody", DENY)),
        # SSO is a security baseline on every plan (Product decision, 3.18).
        # The seven below moved from All-plan (status quo) to Governed+, so
        # Verified now denies them; existing trials keep them via the G2
        # grandfather in the resolver, not via the manifest.
        specification=(("holidays.multicountry", DENY),
                       ("identity.sso", ALLOW),
                      ("payroll.calculator", DENY),
                      ("reports.advanced", DENY),
                      ("activity.anomaly", DENY),
                      ("leave.approvals", DENY),
                      ("offboarding.governed", DENY),
                      ("time.attendance.policy", DENY),
                      ("time.playback.advanced", DENY)),
    ),
    "governed": _grants(
        shipped=(("integrations.api", ALLOW),
                 ("evidence.legal_hold", DENY), ("offboarding.legal_hold", DENY)),
        # SSO to all plans; compliance + chain-of-custody move to Sovereign+;
        # the seven remain granted at Governed and above.
        specification=(("holidays.multicountry", DENY),
                       ("identity.sso", ALLOW),
                       ("reports.compliance", DENY),
                       ("governance.chain_of_custody", DENY),
                      ("payroll.calculator", ALLOW),
                      ("reports.advanced", ALLOW),
                      ("activity.anomaly", ALLOW),
                      ("leave.approvals", ALLOW),
                      ("offboarding.governed", ALLOW),
                      ("time.attendance.policy", ALLOW),
                      ("time.playback.advanced", ALLOW)),
    ),
    "sovereign": _grants(
        shipped=(("identity.sso", ALLOW), ("integrations.api", ALLOW),
                 ("evidence.legal_hold", ALLOW), ("offboarding.legal_hold", ALLOW),
                 ("reports.compliance", ALLOW), ("governance.chain_of_custody", ALLOW)),
        specification=(("holidays.multicountry", ALLOW),
                      ("payroll.calculator", ALLOW),
                      ("reports.advanced", ALLOW),
                      ("activity.anomaly", ALLOW),
                      ("leave.approvals", ALLOW),
                      ("offboarding.governed", ALLOW),
                      ("time.attendance.policy", ALLOW),
                      ("time.playback.advanced", ALLOW)),
    ),
    # Enterprise is contract-led. Its manifest is Sovereign's — the per-tenant
    # variation that actually distinguishes an Enterprise agreement is carried by
    # ``CommercialSubscription.enterprise_override``, which the resolver already
    # applies on top. Duplicating that negotiation into a static manifest would
    # put two sources of truth on the same question.
    "enterprise": _grants(
        shipped=(("identity.sso", ALLOW), ("integrations.api", ALLOW),
                 ("evidence.legal_hold", ALLOW), ("offboarding.legal_hold", ALLOW),
                 ("reports.compliance", ALLOW), ("governance.chain_of_custody", ALLOW)),
        specification=(("holidays.multicountry", ALLOW),
                      ("payroll.calculator", ALLOW),
                      ("reports.advanced", ALLOW),
                      ("activity.anomaly", ALLOW),
                      ("leave.approvals", ALLOW),
                      ("offboarding.governed", ALLOW),
                      ("time.attendance.policy", ALLOW),
                      ("time.playback.advanced", ALLOW)),
    ),
}

#: The manifest version in force per plan. ``verified`` and ``governed`` were
#: repackaged in the 2026-08 commercial retier (SSO to all plans; seven
#: capabilities to Governed+; compliance and chain-of-custody to Sovereign+), so
#: they carry a NEW version. ``sovereign`` and ``enterprise`` are byte-identical
#: to _2026_08 (the seven keys they already granted only moved between provenance
#: blocks) and keep it. The new version is what lets ``_manifest_is_current`` see
#: every warm cache row from the old packaging as stale on its next read, with no
#: explicit invalidation — exactly the mechanism the spec's versioning intends.
_PLAN_VERSION: Dict[str, str] = {
    "verified":   "verified_2026_09",
    "governed":   "governed_2026_09",
    "sovereign":  "sovereign_2026_08",
    "enterprise": "enterprise_2026_08",
}

#: Every manifest ever published, keyed by version. A change to packaging is a
#: NEW version, never an edit to an existing one — that is what makes it possible
#: to answer "what was this tenant entitled to in September" after the fact.
MANIFESTS: Dict[str, Manifest] = {
    version: _plan_manifest(version, plan, tiered=_TIERED[plan])
    for plan, version in _PLAN_VERSION.items()
}

# The vendor tenant runs the product it sells, so it needs the full capability
# set — Sovereign-equivalent, built from the *same* shared blocks as every other
# manifest. That sameness is the point: ``_validate`` applies GLOBALLY_DENIED to
# this manifest exactly as it does to the commercial ones, so Apps History and
# Website Categories stay denied for Temveris's own workspace too. Bypassing
# the resolver for internal tenants would have lost precisely that.
MANIFESTS[f"{INTERNAL_PLAN}_2026_08"] = _plan_manifest(
    f"{INTERNAL_PLAN}_2026_08", INTERNAL_PLAN, tiered=_TIERED["sovereign"])

#: The manifest version currently in force for each plan. Moving a plan to new
#: packaging means adding a manifest and repointing this map (see _PLAN_VERSION).
CURRENT_VERSIONS: Dict[str, str] = dict(_PLAN_VERSION)
CURRENT_VERSIONS[INTERNAL_PLAN] = f"{INTERNAL_PLAN}_2026_08"

#: Plan used when a subscription names something unrecognised. Matches the
#: resolver's existing fallback (``_PLAN_FEATURES["verified"]``) so the two
#: layers cannot disagree about what an unknown plan gets.
FALLBACK_PLAN = "verified"


# ══════════════════════════════════════════════════════════════════════════════
# Accessors
# ══════════════════════════════════════════════════════════════════════════════
def manifest_for(plan_code: Optional[str]) -> Manifest:
    """The manifest in force for a plan. Unknown plans fall back to Verified."""
    version = CURRENT_VERSIONS.get((plan_code or "").lower())
    if version is None:
        version = CURRENT_VERSIONS[FALLBACK_PLAN]
    return MANIFESTS[version]


def capabilities_for(plan_code: Optional[str]) -> Dict[str, str]:
    """``{feature_key: decision}`` for a plan — the whole dictionary, no gaps."""
    m = manifest_for(plan_code)
    return {key: m.decision(key) for key in FEATURES}


def decision_for(plan_code: Optional[str], feature_key: str) -> str:
    """The decision a plan makes about one capability.

    Unknown keys resolve to DENY: a typo must never widen access.
    """
    if feature_key not in FEATURES:
        return DENY
    return manifest_for(plan_code).decision(feature_key)


# ══════════════════════════════════════════════════════════════════════════════
# Presentation projection (spec §7)
#
# Presentation is NOT authorization. It is the SHOW / LOCK / HIDE / READ_ONLY /
# CONFIGURATION_REQUIRED hint the frontend uses to render a sidebar item. It is
# derived here from data that already exists — the plan decision, the capability
# nature and whether any plan sells the capability — so there is one source of
# truth for the vocabulary. Nothing here grants access: the decision remains
# authoritative, and a decision of DENY can never project as SHOW.
# ══════════════════════════════════════════════════════════════════════════════
SHOW = "show"
LOCK = "lock"
HIDE = "hide"
#: READ_ONLY and CONFIGURATION_REQUIRED are reused verbatim from the decision
#: vocabulary — the presentation and the decision are the same word there.
PRESENTATIONS = frozenset({SHOW, LOCK, HIDE, READ_ONLY, CONFIGURATION_REQUIRED})


def is_upsellable(feature_key: str) -> bool:
    """Whether *some* plan grants this capability at all.

    A denied capability is only a candidate for a LOCK/upsell if buying a higher
    plan would actually unlock it. Globally denied privacy keys, undecided keys
    and capabilities no plan has been built to grant are not upsellable — there
    is nothing to sell — so they HIDE rather than LOCK. Purely plan data; no
    actor, no lifecycle.
    """
    if feature_key not in FEATURES:
        return False
    return any(decision_for(plan, feature_key) != DENY for plan in PLANS)


def presentation_for(decision: str, feature_key: str, *, is_worker: bool) -> str:
    """Map an authoritative decision to a presentation hint (spec §7).

    Never widens access. The mapping:

    * ``allow``                  → SHOW
    * ``read_only``              → READ_ONLY   (historical access, writes disabled)
    * ``configuration_required`` → CONFIGURATION_REQUIRED  (setup CTA, not upsell)
    * ``deny`` + upsellable + admin/owner → LOCK   (discoverable upgrade)
    * ``deny`` + upsellable + worker      → HIDE   (§7: no upsell to workers)
    * ``deny`` + not upsellable           → HIDE   (nothing to sell)

    ``is_worker`` is the only actor input, and it can only ever *narrow* a LOCK
    to a HIDE — it can never turn a HIDE or DENY into a SHOW.
    """
    if decision == ALLOW:
        return SHOW
    if decision == READ_ONLY:
        return READ_ONLY
    if decision == CONFIGURATION_REQUIRED:
        return CONFIGURATION_REQUIRED
    # decision == DENY (or any non-permissive value): default to hidden.
    if is_upsellable(feature_key) and not is_worker:
        return LOCK
    return HIDE


def provisional_keys() -> Tuple[str, ...]:
    """Capabilities granted only because they are ungated today.

    These are the ones Product must rule on before any enforcement phase: the
    manifest currently says "everyone", which is the truth about today's
    behaviour and not a packaging decision.
    """
    m = MANIFESTS[CURRENT_VERSIONS[FALLBACK_PLAN]]
    return tuple(sorted(k for k, g in m.grants.items() if g.provenance == STATUS_QUO))


def features_by_category() -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, list] = {}
    for key, feat in FEATURES.items():
        out.setdefault(feat.category, []).append(key)
    return {cat: tuple(sorted(keys)) for cat, keys in sorted(out.items())}


# ══════════════════════════════════════════════════════════════════════════════
# Import-time validation
# ══════════════════════════════════════════════════════════════════════════════
class ManifestError(ValueError):
    """Raised at import when the dictionary or a manifest is inconsistent."""


def _validate() -> None:
    """Fail the import rather than serve a malformed entitlement table.

    A manifest that is wrong in a way nobody notices is worse than one that
    refuses to load, because it answers authorization questions confidently.
    """
    from app.commercial import constants  # noqa: PLC0415 — avoids an import cycle

    legacy = set(constants.ENTITLEMENT_FEATURES)

    for key, feat in FEATURES.items():
        if key != feat.key:
            raise ManifestError(f"feature registered under the wrong key: {key} != {feat.key}")
        if not key.islower() or " " in key:
            raise ManifestError(f"feature key must be lowercase and space-free: {key!r}")
        if "." not in key:
            raise ManifestError(
                f"feature key must be namespaced as category.capability: {key!r}")
        if feat.implementation not in IMPLEMENTATION_STATES:
            raise ManifestError(f"{key}: unknown implementation state {feat.implementation!r}")
        if feat.legacy_key is not None and feat.legacy_key not in legacy:
            raise ManifestError(
                f"{key}: legacy_key {feat.legacy_key!r} is not one of "
                f"constants.ENTITLEMENT_FEATURES")

    for plan in PLANS:
        if plan not in CURRENT_VERSIONS:
            raise ManifestError(f"plan {plan!r} has no current manifest version")

    for version, m in MANIFESTS.items():
        if m.version != version:
            raise ManifestError(f"manifest registered under the wrong version: {version}")
        if m.plan_code not in PLANS and m.plan_code != INTERNAL_PLAN:
            raise ManifestError(f"{version}: unknown plan_code {m.plan_code!r}")

        unknown = set(m.grants) - set(FEATURES)
        if unknown:
            raise ManifestError(f"{version}: grants for unknown features {sorted(unknown)}")

        # Every capability needs an intentional decision. Silence is not a
        # decision — an omitted key would resolve to DENY by accident rather
        # than on purpose, and nobody would ever see it.
        missing = set(FEATURES) - set(m.grants)
        if missing:
            raise ManifestError(f"{version}: no decision recorded for {sorted(missing)}")

        for key, grant in m.grants.items():
            if grant.decision not in DECISIONS:
                raise ManifestError(f"{version}/{key}: unknown decision {grant.decision!r}")
            if grant.provenance not in PROVENANCES:
                raise ManifestError(f"{version}/{key}: unknown provenance {grant.provenance!r}")
            if key in GLOBALLY_DENIED and grant.decision != DENY:
                raise ManifestError(
                    f"{version}/{key}: globally denied capability may not be granted "
                    f"(got {grant.decision!r}). Changing this needs an explicit "
                    f"Product/Privacy decision, not a manifest edit.")
            if FEATURES[key].configuration_required and grant.decision == ALLOW:
                raise ManifestError(
                    f"{version}/{key}: capability is configuration_required, so it may "
                    f"not be a plain {ALLOW!r} — use {CONFIGURATION_REQUIRED!r}.")


_validate()
