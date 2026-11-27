# Temveris — Product Requirements Document (PRD)

**Audience:** Product, UX, Sales, Customer Success, Implementation, QA, and enterprise evaluators.
**Purpose:** Defines *what* Temveris is, *who* it's for, *how* customers use it, and *why* each capability exists — from a business/customer lens (the technical "how" lives in the [Master Spec](PLATFORM_MASTER_SPEC.md)).
**Source of truth:** the full documentation set — [Enterprise Audit](ORG_SETTINGS_AUDIT.md), [Roadmap](ORG_SETTINGS_ROADMAP.md), [Architecture Audit](PLATFORM_ARCHITECTURE_AUDIT.md), [Master Spec](PLATFORM_MASTER_SPEC.md), [Execution Guide](PLATFORM_EXECUTION_GUIDE.md), [PI Plan](PLATFORM_PI_PLAN.md), [Engineering Standards](ENGINEERING_STANDARDS.md).
**Status legend:** ✅ Available today · 🔧 Exists but must be hardened/completed · 🆕 New in the roadmap. (Grounded in the audits so sales/CS never over-promise.)

---

## 1. Executive Summary (Section 1)

**Vision.** Make how work actually happens visible, fair, and improvable — so distributed and hybrid teams run with the same clarity as a co-located office, without surveillance overreach.

**Mission.** Give every organization one platform for time, attendance, productivity intelligence, leave, payroll inputs, and workforce compliance — accurate enough to trust for pay and policy, respectful enough that employees accept it.

**Business goals.** Reach enterprise-sellable readiness within ~6 months (secure, multi-tenant, MFA/SSO); open developer/integration and AI revenue by month 12; grow from SMB self-serve into enterprise/regulated segments (finance, healthcare, government via data-residency).

**Target market.** Remote-first and hybrid companies; BPO/call centers; IT services & agencies; and regulated enterprises needing audit-grade workforce evidence. Sizes from 5-seat SMBs to 50,000-seat enterprises; agencies managing multiple client orgs.

**Competitive position.** Temveris combines the **tracking depth** of Time Doctor / Hubstaff / Insightful / ActivTrak with the **org-hierarchy, RBAC, and compliance rigor** of Rippling / BambooHR, plus a **developer platform and AI layer** the pure-monitoring tools lack. Differentiators: a fair, transparent tracking model (privacy blur, URL-off, policy locks employees can see); tamper-evident work evidence (WEC); and a genuine configuration + permission engine for delegated enterprise administration.

---

## 2. Target Customers (Section 2) — Personas

| Persona | Goals | Pains today | What Temveris gives them |
|---|---|---|---|
| **Startup Founder** | Set up fast, see the team is productive, keep cost low | No time for complex HR tools | Self-serve onboarding < 10 min; sensible defaults; simple plan |
| **HR Manager** | Attendance, leave, policy, payroll inputs, fairness | Data scattered across tools/spreadsheets | Unified attendance + leave + holidays + payroll config + audit |
| **Operations Manager** | Team utilization, SLAs, shift coverage | No real-time visibility across teams | Live status, dashboards, activity/productivity analytics, alerts |
| **Team Lead** | Manage *my* team without seeing everything | Over-broad or no admin access | Scoped "Team Admin" role; team-level settings; approvals |
| **Employee** | Be measured fairly; understand what's tracked | Opaque monitoring, no control/visibility | Transparent tracking (blur/URL-off), visible policy locks, personal prefs, self-service leave |
| **Enterprise IT Admin** | SSO, provisioning, security policy, isolation | Manual user mgmt; no SSO/SCIM | 🆕 MFA/SSO/SAML/SCIM, enforced session/IP/password policy, RBAC, tenant isolation |
| **Finance Manager** | Accurate payroll inputs, billing, seat cost | Manual hours→pay; unclear billing | Payroll runs/exports, usage-based billing, seat/quota visibility |
| **Compliance Officer** | Audit trail, retention, DSAR, evidence | No unified, tamper-evident record | 🔧🆕 Unified audit, retention rules, legal hold, DSAR, WEC evidence, residency |

---

## 3. Problem Statements (Section 3)

| Problem | Who feels it | How Temveris solves it |
|---|---|---|
| **Remote/hybrid work is invisible** — is work actually happening? | Ops, founders | Desktop + browser tracking, activity/active-time scoring, live status |
| **Attendance is manual and disputed** | HR, employees | Clock sessions, breaks, attendance policies, controlled clock-out, alerts |
| **Productivity is guessed, not measured** | Ops, leads | Activity/app/URL analytics, focus/workflow/burnout intelligence, integrity scoring |
| **Compliance is a fire drill** | Compliance, IT | Unified audit trail, retention, legal hold, DSAR, data residency, tamper-evident evidence |
| **Payroll inputs are error-prone** | Finance, HR | Hours→payroll config/runs/exports tied to verified time |
| **Leaders lack actionable insight** | Ops, execs | Reports, rollups, AI insights, risk/burnout narratives |
| **Reporting is exports-and-spreadsheets** | HR, finance | Built-in reports + scheduled exports + API |
| **Repetitive workforce tasks aren't automated** | Ops, HR | 🆕 Automation rules (event → action), notifications engine |
| **Monitoring feels like surveillance** | Employees, works councils | Privacy blur, URL-off (domain-only), visible policy locks, scoped access, consent/residency |

---

## 4. Product Modules (Section 4)

| Module | What it does | Status |
|---|---|---|
| **Organizations** | The tenant: profile, branding, timezone, currency, locale, residency, ownership | 🔧 (fragmented today → 🆕 canonical model) |
| **Departments** | Org hierarchy above teams; delegated department administration | 🆕 (no department entity today) |
| **Teams** | Groups under departments; membership; team leads; team-scoped settings | 🔧 (FK-based membership 🆕) |
| **Employees** | Roster, employment profile, live status, invitations | ✅ |
| **Attendance** | Clock in/out, breaks, attendance & controlled-clock-out policies, late/absent alerts | ✅🔧 |
| **Time Tracking** | Activity capture, active-time %, integrity/anti-fake scoring | ✅ |
| **Desktop Tracker** | Electron app: screenshots, app/window, input activity; pulls policy from server | ✅ |
| **Browser Tracking** | Extension: URL/domain/tab activity, idle split | ✅ |
| **Screenshots** | Interval capture with privacy blur; admin review | ✅🔧 (→ async pipeline + object storage) |
| **Leave** | Requests, approvals, balances, policies, shared pools, holidays | ✅ |
| **Payroll** | Per-employee config, runs, items, exports | ✅ |
| **Reports** | Dashboards, timelines, exports, analytics rollups | ✅🔧 |
| **Notifications** | Alerts and preferences across channels (email/in-app → Slack/Teams/webhook) | 🔧🆕 |
| **Settings** | Configuration engine: org/department/team/employee cascade, overrides, templates | 🔧🆕 (add department tier + templates) |
| **Permissions** | Custom roles, scoped/delegated/temporary admin, permission matrix | 🔧🆕 (persisted RBAC/ABAC) |
| **Billing/Subscription** | Plans, subscriptions, invoices, seats, quotas, trials, usage | 🔧🆕 (single-tenant today → per-tenant) |
| **Security & Identity** | MFA, SSO, SAML, SCIM, session/IP/password policy | 🆕 (stored-but-unenforced today) |
| **Developer Platform** | Public API, API keys, webhooks, OAuth apps, portal | 🆕 |
| **Integrations** | HRIS/payroll/SSO/comms/PM connectors | 🆕 (stub today) |
| **AI Assistant** | KAIROS: assistant, knowledge/RAG, insights, risk, automation | 🔧🆕 |
| **Compliance** | Audit, retention, legal hold, DSAR, residency, evidence | 🔧🆕 |

---

## 5. User Journeys (Section 5)

1. **Organization onboarding.** Sign up → verify email → create org (name, timezone, currency) → choose plan/trial → land on an empty dashboard with a guided checklist (invite team, install tracker, set policies). *Target: < 10 min to first value.*
2. **Employee invitation.** Admin invites by email (or bulk/CSV, or 🆕 SCIM auto-provision) → invitee gets a link → sets password (or SSO) → joins their team.
3. **First login.** Employee signs in (password or 🆕 SSO; 🆕 MFA if enforced) → sees their dashboard, what's tracked, and personal preferences; policy-locked settings shown as read-only with explanation.
4. **Tracker installation.** Download desktop tracker → login → grant OS permissions (screen recording) → tracker pulls org policy (interval, blur, tracking flags) and begins capture; degraded state surfaced if input hook missing.
5. **Clock in.** Employee clocks in (web/tracker) → live status broadcasts to managers → activity/active-time scoring begins.
6. **Daily work.** Tracker captures screenshots (with blur if configured), app/browser activity; breaks tracked; idle detected; controlled clock-out at end of day; integrity scoring flags anomalies.
7. **Leave request.** Employee requests leave → balance checked against policy → routed to approver.
8. **Manager approval.** Manager reviews (scoped to their team/department) → approves/rejects → balance adjusted, calendar updated, requester notified, audit recorded.
9. **Payroll export.** Finance configures pay rules → runs payroll for a period → verified hours + leave feed items → exports to file/payroll system.
10. **Subscription upgrade.** Admin views plan/usage/seats → upgrades (or requests enterprise quote) → seats/entitlements update → invoice generated. *(🔧 must be per-tenant — single-tenant billing is a P0 fix.)*

---

## 6. Functional Requirements (Section 6) — representative per module

*(Feature · description · acceptance criteria · key business rules · priority.)*

**Organizations**
- *Org profile & branding* — set identity, tz, currency, locale, logo, colors. **AC:** changes validated, audited, reflected across UI. **Rules:** one owner; only admin/owner edits; residency set at creation. **P0.**

**Departments / Teams**
- *Department hierarchy* 🆕 — create departments, assign teams. **AC:** teams roll up to departments; settings cascade through them. **Rules:** unique name per org; a team belongs to one department. **P1.**
- *Team membership by FK* 🔧 — assign users to teams. **AC:** membership is referential (rename doesn't orphan). **Rules:** a user has one primary team; leads scoped to their team. **P1.**

**Attendance / Tracking**
- *Clock & breaks* — start/stop work and breaks. **AC:** durations accurate to the second; controlled clock-out enforced per policy. **Rules:** policy-driven expected durations; auto-close safety net. **P0.**
- *Activity & integrity* — capture and score activity. **AC:** active-time % reproducible; fake-activity flagged. **Rules:** engine tuning is org-scoped. **P1.**

**Screenshots / Privacy**
- *Interval capture with blur* — configurable interval + privacy blur + URL-off. **AC:** interval/blur honored by tracker within one poll cycle; URL-off keeps domain only. **Rules:** overridable at org/dept/team/employee; employees can see what's active. **P0.**

**Leave / Payroll**
- *Leave lifecycle* — request→approve→balance. **AC:** balance can't go negative beyond policy; approver scoped. **P0.**
- *Payroll run/export* — compute + export. **AC:** items reconcile to verified hours+leave. **Rules:** admin/finance only; period locked after run. **P1.**

**Settings / Permissions**
- *Config cascade + reset* — override at any tier, reset to inherit. **AC:** effective value resolves correctly; override/inherited/locked shown. **P0.**
- *Custom scoped roles* 🆕 — define roles, scope to dept/team, delegate, expire. **AC:** holder acts only within scope; can't grant what you lack (SoD); all grants audited. **P1.**

**Security & Identity** 🆕 — MFA (enforced when required), SSO/SAML login, SCIM provisioning, enforced session/IP/password policy. **AC:** each actually enforced (not stored-and-ignored); recovery + break-glass exist. **P0–P1.**

**Billing** 🔧 — per-tenant subscription, seats, quotas, usage, invoices, trials. **AC:** each org sees only its billing; seat limits enforced at invite. **Rules:** entitlements gate features. **P0 (tenancy fix), P1 (usage).**

**Developer Platform** 🆕 — API keys (scoped, rotatable), webhooks (signed, retried), OAuth apps, portal, rate limits. **AC:** keys hashed; webhooks delivered with backoff + dead-letter. **P2.**

**AI Assistant** 🔧🆕 — assistant, RAG over org knowledge, insights, risk narratives, automation rules. **AC:** answers permission-scoped; AI never mutates data without an explicit rule/approval. **P2.**

---

## 7. Non-Functional Requirements (Section 7)

| Area | Requirement |
|---|---|
| **Performance** | API p95 < 300 ms; dashboard < 2 s; screenshot ingest lag < 30 s; config resolve < 10 ms cached |
| **Availability** | 99.9% monthly; RTO ≤ 1h, RPO ≤ 5m; zero-downtime deploys |
| **Scalability** | 10,000 orgs · 1,000,000 employees · 100,000 concurrent trackers · millions of activities/screenshots per day, at linear cost |
| **Security** | MFA/SSO/SAML/SCIM; RBAC/ABAC; encryption in transit + at rest + field-level for PII/payroll; tenant isolation (RLS); secrets in a manager; audit of all security mutations |
| **Accessibility** | WCAG 2.1 AA across the admin console and employee surfaces |
| **Localization** | Multi-language UI, per-org timezone/currency/locale; date/number formatting |
| **Compliance** | SOC 2 Type II, GDPR/CCPA (consent, retention, DSAR), ISO 27001, HIPAA-capable; regional data residency |

---

## 8. UX Requirements (Section 8)

- **Navigation.** Consistent shell; discoverable settings (search + logical grouping); breadcrumbs; role-appropriate menus (a Team Lead doesn't see org-wide admin).
- **Dashboards.** Role-based: employee (my day), lead (my team), ops/HR (org), finance (billing/payroll), compliance (audit). Real-time where it matters (live status).
- **Desktop.** Admin console is desktop-first, information-dense, keyboard-friendly; no horizontal body scroll; dark + light.
- **Mobile.** Responsive employee surfaces (clock, leave, status, notifications); admin console usable on tablet.
- **Accessibility.** Every interactive control keyboard-operable and screen-reader labeled; visible focus; ARIA on custom controls (a hard gap today — mandated by the Engineering Standards).
- **Search.** Global + in-context (settings, employees, audit, reports) with debounce and filters.
- **Bulk actions.** Bulk invite, bulk role assignment, bulk settings apply (templates), bulk leave/holiday import; clear per-item success/failure.
- **Transparency (differentiator).** Employees can always see what's tracked and which settings are workspace-managed, with the governing policy name/version.

---

## 9. Success Metrics (Section 9)

| Metric | Target |
|---|---|
| **Customer adoption** | Onboarding-to-first-value < 10 min; tracker install rate per seat; feature adoption per release |
| **Retention** | Logo + net-revenue retention ↑; churn ↓; trial→paid conversion tracked |
| **Usage** | DAU/MAU of admins; active trackers; screenshots + activities per day; reports run |
| **Performance** | Meets §7 NFR budgets continuously (p95, ingest lag, availability) |
| **Support tickets** | Tickets per feature/release ↓; time-to-resolution ↓; self-serve deflection ↑ |
| **Revenue** | MRR/ARR growth; seat expansion; usage-based/add-on revenue; enterprise deal count (gated by MFA/SSO/compliance milestones) |
| **Satisfaction** | CSAT/NPS per release; employee-trust sentiment (fairness/transparency) |

---

## 10. Release Scope (Section 10)

| Version | Customer-facing theme | Ships (customer value) |
|---|---|---|
| **V1 — Stable & Secure** | "Trustworthy foundation" | Existing tracking/attendance/leave/payroll **hardened**; no cross-tenant data exposure; **per-tenant billing**; enforced roles/permissions; complete audit trail. *Sales: safe to deploy; data isolation you can attest to.* |
| **V2 — Enterprise Identity** | "IT will approve it" | **MFA, SSO (Google/Azure), SAML, SCIM**, enforced session/IP/password policy. *Sales: passes enterprise IT security review.* |
| **V3 — Platform Foundation** | "Structured for your org" | **Departments + team hierarchy**, delegated **scoped admin**, the full **settings cascade with templates**, faster/more reliable at scale. *Sales: model your real org chart; delegate admin safely.* |
| **V4 — Developer & Extensibility** | "Fits your stack" | **Public API, API keys, webhooks, OAuth apps, integrations** (HRIS/payroll/comms), **automation rules**, richer **reports/notifications**, **branding**. *Sales: integrate and automate; white-label.* |
| **V5 — Commercial, AI & Scale** | "Insight + scale" | **Usage-based billing, seats/quotas**, **AI assistant + insights + risk/automation**, HA and target-scale performance, marketplace (beyond 12 mo). *Sales: intelligence and enterprise scale.* |

**Honesty guardrail for Sales/CS (from the audits):** features marked 🔧/🆕 above are **not** enterprise-ready today — MFA, SSO/SAML/SCIM, per-tenant billing, departments, developer platform, and AI insights land in the versions shown. Do not represent them as available before their release. The transparent-tracking, cascade-override UX, and refresh-token security *are* real strengths available now.

---

### How this PRD fits the document set
This is the **business/customer** view. It maps 1:1 to the technical plan: PRD modules → Master Spec bounded contexts; PRD releases V1–V5 → PI Plan releases R1–R5 → Roadmap phases. Requirements here become epics/features/stories in the [PI Plan](PLATFORM_PI_PLAN.md), are built to the [Engineering Standards](ENGINEERING_STANDARDS.md), and are justified by the [audits](ORG_SETTINGS_AUDIT.md). Product and engineering read the same story from two ends.
