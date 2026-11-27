# Temveris — Operations & Customer Success Playbook

**Audience:** Customer Success, Professional Services, DevOps, Support Engineers, Enterprise IT, Implementation Partners.
**Purpose:** How Temveris is deployed, operated, monitored, supported, and adopted *after* the product is built — the runbook layer beneath the [PRD](PRODUCT_REQUIREMENTS.md) and [Master Spec](PLATFORM_MASTER_SPEC.md).
**Source of truth:** the full documentation set (audits → spec → execution guide → PI plan → standards → PRD).
**Status legend:** ✅ Available today · 🔧 Hardening in progress · 🆕 Roadmap (release noted). CS/Support **must not** promise 🆕/🔧 items before their release — this playbook flags them so nobody over-commits.
**Grounding:** the platform today is a single Cloud Run container + Supabase (Postgres + Storage), an Electron desktop tracker that polls `GET /settings` and uploads to `/tracking/screenshot`, and a Chromium MV3 extension posting to `/browser/*`. Redis/workers/object-storage and SSO/MFA/SCIM are roadmap (R2–R3).

---

## 1. Deployment Models (Section 1)

| Model | Status | Requirements | Pros | Cons | Supported features |
|---|---|---|---|---|---|
| **SaaS Cloud (multi-tenant)** | ✅ default | None for customer (browser + tracker install) | Fastest onboarding; auto-updates; lowest cost | Shared infrastructure; region fixed at sign-up | All features per release |
| **Dedicated Cloud (single-tenant instance)** | 🆕 R3+ | Enterprise contract; region choice | Isolation, data residency, custom retention | Higher cost; slower release cadence | Full feature set + residency controls |
| **Hybrid (SaaS control plane + regional data)** | 🆕 R5+ | Enterprise; regional object storage/DB | Data residency without full self-host | Operational complexity | Residency for screenshots/PII; central admin |
| **Self-Hosted / Private Cloud** | 🆕 post-12-mo | Customer-run Postgres, object storage, Redis, container platform | Full control for regulated buyers | Customer owns ops, upgrades, DR | Feature parity lags SaaS by ≥1 release |

**Today's reality (for honest scoping):** only **SaaS Cloud multi-tenant** is production. Dedicated/hybrid/self-host depend on the R3 platform foundation (canonical tenancy + object storage + Redis) landing first. Data residency is currently display-only until R3.

---

## 2. Customer Onboarding (Section 2)

**Standard onboarding sequence (target < 1 day to first value; < 10 min to org creation):**

1. **Organization creation** ✅ — sign up → verify email → set org name, timezone, currency (locale/residency 🔧). Owner assigned. Guided checklist appears.
2. **Billing** 🔧 — choose plan or start trial; enter payment method. *(Per-tenant billing is a P0 R1 fix — until then, verify the org sees only its own subscription.)*
3. **Inviting users** ✅ — invite by email (single or CSV bulk); 🆕 SCIM auto-provisioning in R2. Invitees set password (or SSO in R2).
4. **Department setup** 🆕 R3 — create departments; assign teams. *(No department entity pre-R3 — use teams only.)*
5. **Team setup** ✅🔧 — create teams, assign members, name team leads. *(Membership becomes FK-based in R3; pre-R3 it's a name string — avoid renaming teams to prevent orphaned overrides.)*
6. **SSO setup** 🆕 R2 — OIDC (Google/Azure) then SAML2; enforce-for-admins. *(Pre-R2: password auth only.)*
7. **Desktop Tracker rollout** ✅ — distribute installer (§5 silent install for enterprise); users log in; tracker pulls org policy within one 60s poll.
8. **Browser Extension rollout** ✅ — install from store/enterprise policy; user pastes API token in popup (🆕 OAuth device-flow auth in R3).
9. **Holiday setup** ✅ — seed the regional calendar (auto-seeded per org); import CSV; mark applicability.
10. **Working hours** ✅ — set daily work-hours threshold and expected durations (org → department → team → employee override cascade).
11. **Attendance policies** ✅🔧 — idle threshold, controlled clock-out, break rules; overridable per scope.

**CS tip:** drive the guided checklist to completion in the kickoff call — orgs that complete tracker install + policy setup + first invites in week 1 have materially higher retention (see §11).

---

## 3. Administrator Guide (Section 3)

| Cadence | Tasks |
|---|---|
| **Daily** | Review live status / attendance exceptions; action late/absent alerts; approve pending leave; triage risk/integrity flags |
| **Weekly** | Review team utilization & productivity reports; check tracker health (offline/degraded devices); process new-hire invites & offboarding; review flagged screenshots |
| **Monthly** | Reconcile attendance → payroll inputs; run payroll export; review seat usage vs plan; review audit highlights; update holiday calendar |
| **Quarterly** | Access review (who has admin/which scope); rotate API keys 🆕; review retention/compliance posture; plan seat/plan changes; review policy effectiveness and adjust cascade defaults/templates 🆕 |

**Golden rules for admins:** change settings at the **highest scope that's correct** (org > department > team > employee) so overrides stay minimal; use **templates** 🆕 for repeatable configs; never share admin accounts — use **scoped roles** 🆕 (Team/Department Admin) instead of blanket admin.

---

## 4. Employee Guide (Section 4)

- **Clock in/out** ✅ — from web or tracker; status broadcasts to your manager. Controlled clock-out may apply per policy.
- **Breaks** ✅ — start/end breaks; overruns may alert your manager per policy.
- **Leave** ✅ — request leave; see your balance; track approval status; attach documents if required.
- **Tracker** ✅ — install, log in, grant OS permissions (screen recording on macOS). It runs in the background and pulls your org's policy automatically.
- **Privacy (what's tracked & your visibility)** ✅ — you can see what's active for you: screenshots (with blur if enabled), app/browser activity (URL-off = domain only if configured), and any **workspace-managed (locked) settings** with the governing policy name. Personal preferences you *can* change are clearly editable.
- **Reports** ✅ — view your own hours, activity, and attendance history.
- **Notifications** ✅🔧 — manage which alerts you receive (channels expand to Slack/Teams in R4).

---

## 5. Enterprise IT Guide (Section 5)

**Network / connectivity**
- **Firewall/allowlist:** outbound HTTPS (443) from tracker/extension/browsers to the Temveris API + Supabase Storage domains and the desktop-update bucket. Provide the exact hostnames per environment during implementation.
- **Proxy:** tracker respects system proxy; if TLS-inspecting, allow the API + storage hosts (pinning not used, but validate cert chains).
- **Certificates:** standard public CAs; no custom cert required for SaaS.
- **Allowed domains:** whitelist Temveris web app + API + storage; for SSO, the IdP domains.

**Identity (🆕 R2)**
- **SSO:** OIDC (Google/Azure) then SAML2; invite-only provisioning, `email_verified` required; enforce-for-admins or all.
- **MFA:** TOTP/WebAuthn enforced per org policy; recovery codes + break-glass documented.
- **SCIM:** 2.0 provisioning/deprovisioning from the IdP.
- *(Pre-R2: password auth + manual/CSV invites only — set expectations accordingly.)*

**Desktop deployment**
- **Silent installation:** distribute the signed installer via MDM/Intune/SCCM/Jantsen; supports unattended install; auto-update via the update channel (can be pinned/staged for enterprise).
- **OS permissions:** macOS Screen Recording must be pre-granted via MDM PPPC profile for zero-touch; Windows needs no special grant beyond install.
- **Capability negotiation** 🆕 R3 — new config keys default safely for older tracker versions; no forced mid-session breakage.

---

## 6. Operations Guide (Section 6) — DevOps

- **Monitoring** — dashboards for API latency (p95/p99), error rate, queue depth 🆕, ingest lag, DB connections/replication lag, Redis 🆕, active trackers, screenshots/day. Correlation-id tracing api→worker→realtime 🆕.
- **Backups** — automated Postgres PITR (Supabase-managed today); object-storage versioning 🆕; **tested restores** on a schedule (a backup you haven't restored is a hope, not a backup).
- **Restores** — documented restore-to-point-in-time runbook; validate row counts + app health post-restore; rehearse quarterly.
- **Incident response** — on-call rotation; alerts link runbooks (`runbooks/`); severity per §8; post-incident review with a note added to `runbooks/`.
- **Scaling** — today the app is a single Cloud Run container: **real-time (WebSocket) and scheduled jobs must run single-instance** until the R3 Redis backplane + worker fleet land (in-memory WS rooms and per-worker duplicated schedulers otherwise misbehave). Post-R3: scale api/worker/realtime independently.
- **Maintenance windows** — schedule for secret rotation (invalidates sessions), major migrations, and infra cutovers; communicate to customers per §8; use expand/contract so most changes need no window.
- **Disaster recovery** — multi-AZ primary + cross-region replica (🆕 as infra matures); target RTO ≤ 1h, RPO ≤ 5m; DR runbook tested; document current actual RTO/RPO honestly to enterprise buyers (interim is higher until HA lands in R5).

---

## 7. Support Playbooks (Section 7)

| Issue | First checks | Likely cause / resolution |
|---|---|---|
| **Tracker offline** | Tracker logged in? Network/proxy to API + storage? Version current? | Re-login (token/refresh); allowlist hosts; update tracker; check `hook_active` (missing input hook → `LIMITED_TRACKING`, reinstall native dep) |
| **Screenshots missing** | `screenshot_enabled` at the effective scope? Interval? OS screen-recording permission? Storage reachable? | Enable at correct scope; grant macOS permission; verify storage host allowlisted; check upload retries in tracker logs |
| **Attendance mismatch** | Timezone at org/user? Clock events present? Controlled clock-out policy? | Correct org timezone; check for missed clock-out (auto-close policy); reconcile break overlaps; review time adjustments |
| **Billing problems** | Which org's subscription is shown? Seat count vs plan? | 🔧 verify per-tenant billing (pre-R1 fix, orgs could see the shared `"default"` record — escalate if seen); reconcile seats; check invoice/payment status |
| **SSO failures** 🆕 R2 | IdP config, `email_verified`, invite exists, clock skew on SAML | Fix IdP mapping; ensure user invited (invite-only); check SAML assertion clock skew; confirm enforce settings |
| **Performance issues** | Which endpoint? Cache warm? DB slow query? Large report? | Check dashboards for p95/slow queries; warm settings cache 🆕; paginate/limit large reports; scale up (single-instance caveat pre-R3) |

**Escalation matrix:** L1 Support (triage, known issues, docs) → L2 Support Engineering (logs, config, reproduce) → L3 Engineering domain owner (bug/defect) → Incident Commander (Sev-1/2, customer-impacting outage). Security issues route immediately to the Security owner regardless of level.

---

## 8. SLAs & Support (Section 8)

| Priority | Definition | Response | Resolution target |
|---|---|---|---|
| **P1 / Sev-1** | Production down / data-exposure / all users blocked | 15–30 min (24×7) | Continuous until mitigated; RCA in 5 business days |
| **P2 / Sev-2** | Major feature broken / large subset blocked | 2 business hours | 1–2 business days |
| **P3** | Minor feature / workaround exists | 1 business day | Next release/patch |
| **P4** | Question / cosmetic / enhancement | 2 business days | Backlog |

- **Maintenance windows:** announced ≥ 72h ahead (except emergency security); expand/contract keeps most deploys zero-downtime.
- **Support channels:** in-app + email (all tiers); dedicated Slack/CSM + phone (enterprise); status page for incidents.
- *(SLA tiers are commercial commitments — confirm the specific contract's numbers; the above are the standard framework.)*

---

## 9. Compliance Operations (Section 9)

*(Capabilities land across R1–R5; today's audit trail exists but is fragmented — unified in R1, retention/DSAR hardened in R3+.)*

- **Audit requests** — export the unified `audit_events` for a period/entity/actor with correlation ids; tamper-evident work evidence (WEC) available for disputed activity.
- **DSAR (data subject access request)** 🆕 — locate all personal data for a subject; export or provide; SLA per regulation (GDPR 30 days). Track each request to closure.
- **Data deletion** — soft-delete + scheduled purge (🆕 R3 — replaces today's `SET NULL` orphaning); honor erasure requests subject to legal hold.
- **Legal hold** — place a hold that suspends deletion/retention for named subjects/data; release when lifted; both actions audited.
- **Retention** — per-org retention rules (audit retention exists; general retention 🆕 R3); object-storage lifecycle for screenshots/exports.
- **Compliance reporting** — evidence packages for SOC 2 / ISO / GDPR; residency attestation (🆕 with dedicated/hybrid).

**CS/Compliance rule:** for regulated buyers, state the *current* capability honestly (what's live vs R-x) and map their requirement to the release that delivers it — don't attest to controls not yet enforced (MFA/session/retention enforcement are R1–R3).

---

## 10. Upgrade Guide (Section 10)

- **Version upgrades** — SaaS is continuously delivered (canary → full); customers need no action. Release notes list user-visible + breaking changes.
- **Database migrations** — expand → backfill → dual-write → contract; run as gated jobs, **never at app boot**; forward-compatible with the running version; tested on prod-sized data. Big migrations (org-model, R3) are canaried **per-org** and reversible.
- **Desktop tracker compatibility** — the tracker updates on its own channel; **old versions keep working** (server stays backward-compatible until telemetry shows zero old-version traffic). New config keys default safely for older trackers. Enterprises can pin/stage tracker updates.
- **Rollback procedures** — three layers, in order: (1) **feature flag off** (instant); (2) **redeploy previous image** (minutes; safe because expand/contract keeps old code working); (3) DB rollback (last resort, only if no destructive contraction ran). Every deploy is reversible within minutes.

---

## 11. Customer Health (Section 11)

| Signal | Healthy | At-risk |
|---|---|---|
| **Adoption** | Tracker installed on ≥ 90% of seats; policies configured; departments/teams modeled | Low install rate; default-only config; flat org |
| **Usage** | Steady DAU admins; consistent activity/screenshot volume; reports run; leave/approvals flowing | Declining active trackers; no report usage; dormant admins |
| **Risk indicators** | — | Support-ticket spikes; repeated SSO/tracker failures; billing disputes; low employee-trust sentiment; champion left |
| **Renewal indicators** | Seat expansion; feature adoption per release; integrations connected 🆕; exec engagement | Flat/declining seats; unused entitlements; no exec sponsor |

**Success plans:** every enterprise account gets a plan with defined success criteria (e.g. "attendance accuracy in payroll within 30 days," "SSO live for all admins by day 45"), a named champion + exec sponsor, a quarterly business review, and a feature-adoption roadmap aligned to releases. CSMs track health scores monthly and trigger a save-play on any two at-risk signals.

---

## 12. Implementation Checklists (Section 12)

**Common baseline (all segments):** org created (tz/currency/locale) · billing/trial active & per-tenant verified · owner + admins set (scoped roles) · teams (and departments 🆕) modeled · working hours + attendance policies configured · holidays seeded · tracker + extension rolled out · first reports validated · privacy settings reviewed with employees.

- **Startup (self-serve):** baseline only; guided checklist; default policies; email invites; monthly plan. *Fast, low-touch.*
- **SMB:** baseline + CSV bulk invite + team leads assigned + payroll config + a QBR cadence. *Light-touch CS.*
- **Enterprise:** baseline + **SSO/MFA/SCIM** 🆕 + silent tracker deployment via MDM + department hierarchy + scoped/delegated admin + integrations 🆕 + success plan + security review + DPA/residency discussion + dedicated CSM.
- **Government:** enterprise + **data residency** 🆕 + retention/legal-hold config + audit-export validation + heightened access review + (potentially) dedicated/hybrid deployment + compliance attestation mapping.
- **BPO / Call center (high-seat, high-tracking):** enterprise + scale validation (concurrent trackers, screenshots/day) + strict attendance/controlled-clock-out policies + shift schedules + team-lead dashboards + integrity/anti-fake configuration + load-tested rollout.
- **Healthcare:** enterprise + **HIPAA mode** 🆕 (BAA, PHI handling) + field-level encryption 🆕 + strict retention/DSAR + minimal-necessary access (scoped roles) + audit rigor + residency.

**Implementation rule:** for Enterprise/Government/Healthcare, confirm which 🆕 items are in the customer's contracted release before committing go-live dates — sequence the rollout to the release that delivers their must-haves (SSO → R2, departments/residency → R3, HIPAA/AI → R5).

---

### How this playbook fits the document set
This is the **operate-and-adopt** layer. It consumes the [PRD](PRODUCT_REQUIREMENTS.md) (what customers get), the [Execution Guide](PLATFORM_EXECUTION_GUIDE.md) (deploy/rollback mechanics), the [PI Plan](PLATFORM_PI_PLAN.md) (which release delivers what — so CS scopes honestly), and the [Master Spec](PLATFORM_MASTER_SPEC.md) (the system being operated). Its recurring discipline: **operate to the release that's actually shipped, and never promise a 🆕/🔧 capability before its release** — the same honesty guardrail that runs through the PRD.
