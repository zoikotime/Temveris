# Temveris — Enterprise Security & Compliance Blueprint

**Audience:** Enterprise Security teams, Compliance Officers, CISOs, Procurement, External Auditors, and customers running vendor assessments.
**Source of truth:** the full documentation set, especially the security findings in [ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md) §7 and [PLATFORM_ARCHITECTURE_AUDIT.md](PLATFORM_ARCHITECTURE_AUDIT.md).

> ## ⚠️ Integrity notice — read before using this document in a real assessment
> This blueprint states **two postures for every control: CURRENT (as of 2026‑07) and TARGET (by release)**. It is written honestly against internal audit findings. The current production posture has **known critical gaps under active remediation** (below). **Do not present target-state controls as if they exist today** in any questionnaire, attestation, or contract. Where a control is not yet live, answer with the current state + the committed remediation release. Misrepresenting posture to an auditor or customer is a compliance violation in itself.

**Status legend:** ✅ live & effective · ⚠️ partial / not enforced · ❌ gap (remediation planned) · 🆕 target (release noted). Release map: **R1** security foundation, **R2** identity, **R3** platform/isolation, **R5** HA/compliance depth (see [PI Plan](PLATFORM_PI_PLAN.md)).

---

## 0. Current Security Posture — executive honesty summary

**Genuine strengths today (✅):** enterprise-grade token design (rotating, reuse-detecting, SHA‑256‑hashed refresh tokens with family revocation); per-request `is_active` enforcement; bcrypt passwords + HMAC‑SHA256 OTP (plaintext never stored) + constant-time compares + CSPRNG; Google SSO done well (invite-only, `email_verified`, HMAC-signed state, cross-tenant same-email disambiguation); CORS fails safe (no wildcard-with-credentials); secrets fail-fast on missing `SECRET_KEY`/`DATABASE_URL`; no SQL injection (ORM + bound params); per-query tenant scoping in most modules + a `test_tenant_isolation.py` regression test.

**Known critical/high gaps under remediation (❌/⚠️):**
1. **`backend/.env` committed to git** with live `SECRET_KEY` + `DATABASE_URL` → JWT forgery = full auth bypass. **P0, R1 Sprint 2.**
2. **MFA non-functional** — enforcement model absent; `mfa_required` toggle has no effect. **R2.**
3. **Security policies stored but unenforced** — session idle/absolute timeout, IP allow-list, SSO enforcement, password complexity are configurable but no runtime enforces them. **R1/R2.**
4. **RBAC is hardcoded role strings** (four inconsistent admin sets), advisory only; no persisted permission model outside offboarding. **R1.**
5. **No database RLS** — tenant isolation is app-level only; one forgotten filter = cross-tenant leak. **R3.**
6. **Billing single-tenant** (`"default"` key) + **cross-tenant config bleed** in two engine loaders. **R1.**
7. **No field-level encryption** for PII/payroll/bank; **unvalidated `profile_picture` data URL** (stored-XSS vector). **R1/R3.**
8. **Fragmented audit** (6 logs) with silent security-relevant mutations (role change, settings, team CRUD). **R1.**

**Bottom line for procurement:** Temveris is **not yet enterprise-security-ready in production**; R1–R3 (~6 months) close the criticals and deliver the enforced controls below. This blueprint is the target architecture + the honest current state, not a claim of present compliance.

---

## 1. Security Overview (Section 1)

**Principles the platform is being built to:**
- **Zero Trust** — no implicit trust by network location; every request authenticated + authorized; per-request `is_active` (✅ today) extended to per-request permission + policy checks (🆕 R1–R2). Device-bound tracker tokens.
- **Defense in Depth** — multiple independent layers: edge (rate limit/WAF), app (authZ, validation), data (RLS 🆕 R3 + app-level scoping ✅), encryption (transit ✅ + at-rest + field-level 🆕), audit (unified 🆕 R1). No single control is the only thing standing.
- **Least Privilege** — scoped RBAC/ABAC (🆕 R1), delegated/temporary admin, separation of duties; service credentials minimal-scope.
- **Secure by Default** — deny-by-default authorization; safe defaults for new config keys; CORS/cookies fail safe (✅); no feature ships with security off. (Contra today's "all tracking on by default" — privacy defaults reviewed per §8 GDPR.)

---

## 2. Identity (Section 2)

| Control | Current | Target |
|---|---|---|
| **Authentication** | ✅ JWT access (30 min) + rotating, reuse-detecting, hashed refresh tokens w/ family revocation; per-request `is_active`; bcrypt | Keep; add device binding for tracker/extension |
| **MFA** | ❌ non-functional (`UserMfaFactor` absent; toggle has no effect) | 🆕 R2: TOTP + WebAuthn/passkeys; enforced per org policy; recovery codes + break-glass |
| **SSO** | ⚠️ Google OIDC only (well-implemented); Microsoft/Okta are config placeholders with no handshake | 🆕 R2: OIDC (Google/Azure) enforced; enforce-for-admins actually enforced |
| **SAML** | ❌ not implemented (selectable in UI, no ACS) | 🆕 R2: SAML 2.0 SP |
| **SCIM** | ❌ none | 🆕 R2: SCIM 2.0 provisioning/deprovisioning |
| **Password policies** | ⚠️ hard-coded validator; org-configured min-length/breach rules **not enforced** | 🆕 R1/R2: org-config-driven complexity + breach check enforced at set/reset |
| **Session management** | ✅ expiry + rotation + reuse detection + logout/logout-all/reset revocation. ⚠️ idle/absolute-timeout + revoke-on-role-change **stored, not enforced**; identity cache 5-min stale on role change/deactivation | 🆕 R1: enforce session policies via middleware; invalidate identity cache on user mutation |

---

## 3. Authorization (Section 3)

| Control | Current | Target |
|---|---|---|
| **RBAC** | ❌ hardcoded `role in (...)` strings; four inconsistent admin sets; `User.role` free-text; real matrix only in `offboarding/rbac.py` (enums, unpersisted) | 🆕 R1: persisted `roles`/`permissions`/`role_permissions`/`user_roles`; one `require_permission()` enforcement point; promote the offboarding matrix platform-wide |
| **ABAC** | ❌ none | 🆕 R1: resource scope `own/team/department/org/platform` on every permission |
| **Permission matrix** | ⚠️ exists (offboarding only) | 🆕 R1: platform-wide, ~13 roles, granular verbs, customer-visible |
| **Delegated admin** | ❌ none (only global admin/hr) | 🆕 R1/R3: department/team admins scoped to their subtree |
| **Temporary access** | ❌ none | 🆕 R1: `permission_grants` with `expires_at`; auto-expiry emits audit event |
| **Separation of duties** | ⚠️ partial (super_admin/owner elevation gated; but admin/hr can promote to admin freely) | 🆕 R1: cannot grant a permission/role you don't hold; sensitive grants dual-control |

---

## 4. Data Protection (Section 4)

| Control | Current | Target |
|---|---|---|
| **Encryption in transit** | ✅ TLS (deployment); ⚠️ `COOKIE_SECURE` defaults 0 (must be set in prod) | 🆕 enforce `Secure` cookies in prod; HSTS |
| **Encryption at rest** | ⚠️ infra-level only (Supabase/disk) | 🆕 R3: KMS-managed; documented |
| **Field-level encryption** | ❌ none — payroll/salary/bank + PII in plaintext columns | 🆕 R1/R3: field-level encryption for PII, payroll, bank, sensitive tokens |
| **Secrets** | ❌ `backend/.env` committed (SECRET_KEY, DATABASE_URL); otherwise env-var + fail-fast, no insecure fallbacks | 🆕 R1: purge history, rotate, move to secret manager; CI secret scanner |
| **PII** | ⚠️ collected (name, email, activity, screenshots); no classification/labeling | 🆕 data classification + minimization + retention (§8) |
| **PHI** | ❌ no HIPAA handling | 🆕 R5: HIPAA mode (BAA, PHI segregation, field encryption, minimal-necessary access) |
| **Data classification** | ❌ none formal | 🆕 R3: Public/Internal/Confidential/Restricted labels driving handling |
| **Key management** | ⚠️ single `SECRET_KEY` signs all JWT/HMAC | 🆕 R1/R3: KMS, key rotation, separate signing keys per purpose |
| **Blob handling** | ⚠️ base64 in-row (`Activity.thumbnail`, `profile_picture`); `profile_picture` **unvalidated** (XSS vector) | 🆕 R1/R3: object storage + signed URLs; validate data-URL scheme/content |

---

## 5. Tenant Isolation (Section 5)

| Layer | Current | Target |
|---|---|---|
| **Database** | ⚠️ app-level `organization_id` filtering (mostly consistent) + `test_tenant_isolation.py`; **no RLS**; `SET NULL` on tenant FKs orphans rows to `org_id=NULL` | 🆕 R3: Postgres RLS + soft-delete/purge; centralized org-scoped query dependency |
| **Storage** | ⚠️ shared buckets; access by app logic | 🆕 R3: per-tenant prefixes + signed URLs; residency buckets (dedicated/hybrid) |
| **Cache** | n/a (no cache today) | 🆕 R3: Redis keys namespaced by org; no cross-tenant key reuse |
| **Logs** | ⚠️ org context in audit; app logs not tenant-partitioned | 🆕 correlation id + org context on every log; access-controlled |
| **Backups** | ⚠️ full-DB (Supabase); not per-tenant | 🆕 tenant-scoped export/restore for dedicated; documented |
| **API** | ⚠️ per-endpoint org scoping; **billing globally shared (`"default"`)**; two engine loaders ignore org id | 🆕 R1: fix billing tenancy + engine org-scoping; R3: enforce via RLS so a missed filter can't leak |

**Known cross-tenant risks today (remediation R1/R3):** shared `"default"` billing record; `integrity`/`active_time` `load_config` read any org's row; `super_admin` audit/evidence feeds intentionally cross-tenant (by design, for platform ops — documented).

---

## 6. Infrastructure Security (Section 6)

| Control | Current | Target |
|---|---|---|
| **Cloud** | Cloud Run + Supabase (managed Postgres + Storage) | 🆕 hardened: private networking, least-priv service accounts, multi-AZ |
| **Containers** | single image; migrations best-effort at boot | 🆕 R3: separate api/worker/realtime images; CVE scanning; migrations as gated jobs (not at boot) |
| **CI/CD** | GitHub Actions | 🆕 R1: SAST, dependency scan, **secret scanning** (would have caught `.env`), signed images, quality gates |
| **Secrets** | ❌ committed `.env` | 🆕 R1: secret manager; rotation; no secrets in repo/images |
| **Networking** | public HTTPS ingress | 🆕 private DB/Redis; egress controls; WAF at edge |
| **Firewalls** | Cloud Run defaults | 🆕 documented allowlists (see Ops Playbook §5); WAF rules |
| **Monitoring** | ⚠️ DB-error handling + timing middleware; no full observability | 🆕 R3: metrics/logs/traces, anomaly alerting, security event monitoring |

---

## 7. Application Security (Section 7) — OWASP Top 10

| Risk | Current | Target |
|---|---|---|
| **A01 Broken Access Control** | ⚠️ hardcoded roles, no RLS, cross-tenant billing/config gaps | 🆕 R1/R3: `require_permission` + RLS |
| **A02 Cryptographic Failures** | ⚠️ no field encryption; single signing key; committed secret | 🆕 R1/R3: KMS, field encryption, key rotation |
| **A03 Injection** | ✅ ORM + bound params; no SQLi found; one `text()` uses an int constant | Keep; lint rule against string-built SQL |
| **A04 Insecure Design** | ⚠️ security theater (stored-unenforced controls); fragmented tenancy | 🆕 R1–R3 foundation redesign |
| **A05 Security Misconfiguration** | ⚠️ `COOKIE_SECURE=0` default; committed `.env` | 🆕 R1: secure defaults enforced in prod |
| **A06 Vulnerable Components** | ⚠️ no dependency scanning gate | 🆕 R1: dependency scan in CI |
| **A07 Auth Failures** | ✅ strong tokens/bcrypt/OTP; ❌ no MFA; ⚠️ 200/min login limit (generous) | 🆕 R2 MFA; tighten limits |
| **A08 Integrity Failures** | ⚠️ best-effort migrations at boot; no image signing | 🆕 R1/R3: gated migrations, signed images |
| **A09 Logging/Monitoring Failures** | ⚠️ 6 fragmented audit logs; silent security mutations; audit writes swallowed | 🆕 R1: unified audit, all security mutations logged, no swallowing |
| **A10 SSRF** | ⚠️ limited outbound (SSO, storage, email); no user-controlled fetch surface identified | 🆕 validate/allowlist any outbound-by-URL (webhooks, integrations) in R4 |
| **CSRF** | ✅ state-changing endpoints use Bearer header (not cookie); refresh cookie is HttpOnly, `path=/auth`, SameSite=lax | 🆕 enforce `Secure`; consider CSRF token if cookie-auth expands |
| **XSS** | ⚠️ JSON API (no server HTML); `profile_picture` data URL unvalidated | 🆕 R1: validate uploads; SPA output-encoding standard |
| **Rate limiting** | ⚠️ auth routes only (slowapi) | 🆕 R1: platform-wide, per-key/app/org (Redis) |

---

## 8. Compliance (Section 8)

| Framework | Current | Target |
|---|---|---|
| **SOC 2 Type II** | ❌ not certified; several common controls not yet met (access control, encryption, monitoring) | 🆕 controls sequenced R1–R3; audit readiness targeted post‑R3; Type II window after controls operate ~6 mo |
| **ISO 27001** | ❌ not certified | 🆕 ISMS + controls mapped alongside SOC 2; certification post-foundation |
| **GDPR / UK GDPR** | ⚠️ partial — data collected; DSAR/retention/consent not systematized; residency display-only | 🆕 R1 audit, R3 retention+DSAR+deletion+residency; DPA available; lawful-basis + minimization review of default-on tracking |
| **CCPA/CPRA** | ⚠️ same gaps as GDPR | 🆕 R3: access/delete/opt-out workflows |
| **HIPAA** | ❌ not supported | 🆕 R5: HIPAA mode (BAA, PHI handling, field encryption, access controls, audit) |
| **PCI DSS** | N/A — no card data stored; payments via provider (Stripe) tokenization | 🆕 maintain SAQ-A scope (never store PAN); document |

**Auditor note:** the platform's tamper-evident **Work Evidence Chain (WEC)** and per-org audit retention rules are real building blocks; the gap is *unification and enforcement*, delivered R1–R3.

---

## 9. Incident Response (Section 9)

- **Detection** — alerting on auth anomalies, error spikes, failed migrations, tenant-isolation test failures, secret-scan hits, unusual data export. (🆕 full coverage R3; interim: DB-error + timing alerts.)
- **Escalation** — L1→L2→L3→Incident Commander; **security incidents route immediately to the Security owner**; severity per Ops Playbook §8 (Sev-1 = outage/data exposure, 15–30 min response 24×7).
- **Containment** — flag-off the affected feature; revoke sessions/tokens/keys; isolate tenant if scoped; rotate secrets if credential exposure (rehearsed via the `.env` remediation).
- **Recovery** — restore from PITR if needed; verify integrity; staged re-enable; customer comms per contract/regulatory breach-notification timelines (GDPR 72h).
- **Postmortems** — blameless RCA within 5 business days for Sev-1/2; action items tracked; runbook updated in `runbooks/`.

---

## 10. Business Continuity (Section 10)

| Control | Current | Target |
|---|---|---|
| **Backups** | ⚠️ Supabase-managed PITR; restores not routinely tested | 🆕 automated PITR + object-storage versioning + **tested restores** (quarterly) |
| **Disaster Recovery** | ⚠️ single-region reliance | 🆕 R5: multi-AZ + cross-region replica; DR runbook rehearsed |
| **RTO** | ⚠️ undefined/higher today | 🆕 target ≤ 1 hour (state current honestly to buyers until met) |
| **RPO** | ⚠️ bounded by managed backups | 🆕 target ≤ 5 minutes |
| **High Availability** | ❌ single-container; single-instance for realtime/jobs | 🆕 R3/R5: stateless tiers, 99.9% monthly, zero-downtime deploys |

---

## 11. Vendor Assessment (Section 11) — honest questionnaire answers

*Answer format: **Current** → **Remediation (release)**. Give the current answer in real assessments; cite the roadmap for planned controls. Never mark "Yes" for a target-only control.*

| Question | Answer |
|---|---|
| Is data encrypted in transit? | **Yes** (TLS). Ensure `Secure` cookies enforced in prod (R1). |
| Is data encrypted at rest? | **Infra-level yes** (managed DB/storage); **field-level: not yet** → R1/R3. |
| Do you support SSO? | **Google OIDC yes**; Azure/SAML → R2. |
| Do you support MFA? | **Not yet enforced** → R2. Do not claim MFA today. |
| Do you support SCIM provisioning? | **No** → R2. |
| Is there RBAC with least privilege? | **Basic role-based today; granular/enforced RBAC** → R1. |
| Is tenant data isolated? | **App-level isolation yes** (+ regression test); **DB RLS** → R3; known billing/config isolation fixes → R1. |
| Are secrets managed securely? | **Remediating** — a committed secret is being purged/rotated and moved to a secret manager (R1). Disclose if asked; do not conceal. |
| Do you have an audit trail? | **Yes, but fragmented**; unified + all security mutations logged → R1. |
| Are you SOC 2 / ISO 27001 certified? | **Not yet**; controls sequenced R1–R3, certification thereafter. State target dates, not certification. |
| GDPR: DSAR, deletion, retention? | **Partial**; systematized → R3. DPA available. |
| Do you have DR with defined RTO/RPO? | **Backups yes; formal RTO/RPO + multi-region** → R5. Give current actuals honestly. |
| Do you do dependency/secret scanning + pen testing? | **CI scanning** → R1; **pen test** → R2/R5. |
| Is there rate limiting / DDoS protection? | **Auth routes today**; platform-wide + edge → R1. |
| Can we get a security whitepaper / SIG / CAIQ? | Provide this blueprint + a completed CAIQ reflecting **current** posture with remediation dates. |

---

## 12. Security Roadmap (Section 12) — controls by version

| Version | Security deliverables |
|---|---|
| **V1 / R1 — Security Foundation** | Purge + rotate secrets, secret manager, CI secret/dependency/SAST scanning; persisted **RBAC + ABAC + `require_permission`**; fix billing + engine tenant isolation; **unified audit**, all security mutations logged; enforce session/password policies + secure cookies; platform-wide rate limiting; validate uploads |
| **V2 / R2 — Enterprise Identity** | **MFA** (TOTP/WebAuthn) enforced; **OIDC (Google/Azure) + SAML2 + SCIM**; enforce-for-admins; trusted devices; DAST + pen test |
| **V3 / R3 — Isolation & Data Protection** | **Postgres RLS**; soft-delete/purge (end `SET NULL` orphaning); field-level encryption; KMS + key rotation; object storage + signed URLs; data classification; retention + DSAR + deletion; SOC 2 / ISO control operation begins |
| **V4 / R4 — Developer & Supply-Chain** | API-key scoping + rotation; signed webhooks + SSRF-safe outbound; OAuth apps + consent; per-key rate tiers; SBOM + image signing |
| **V5 / R5 — Compliance Depth & Resilience** | HIPAA mode (BAA/PHI/field-encryption); multi-region HA + tested DR (RTO ≤1h/RPO ≤5m); SOC 2 Type II + ISO 27001 certification; residency (dedicated/hybrid); continuous security monitoring |

---

### How this blueprint fits the document set
This is the **security & compliance** lens over the same plan: controls here map to the [Master Spec](PLATFORM_MASTER_SPEC.md) §11, are delivered on the [PI Plan](PLATFORM_PI_PLAN.md) schedule, enforced by the [Engineering Standards](ENGINEERING_STANDARDS.md) §8, and operated per the [Ops Playbook](OPERATIONS_CS_PLAYBOOK.md) §9. Its non-negotiable discipline — the same honesty guardrail as the PRD and Ops Playbook, applied where it matters most: **state current posture truthfully, cite remediation by release, and never attest to a control that is not yet live.**
