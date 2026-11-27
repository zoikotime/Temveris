/**
 * Temveris — Offboarding admin API client.
 *
 * Thin axios wrappers over every endpoint the backend exposes for the
 * admin Offboarding experience (case CRUD + lifecycle moves, the security
 * API, the governance API). Each function returns the parsed body or
 * throws (callers handle with the standard try/catch + toast pattern used
 * elsewhere in the codebase).
 *
 * Tenant isolation is enforced server-side — every request rides the
 * active-worker JWT (axios interceptor in api/axios.js) so the backend
 * scopes every read/write to the caller's tenant. The frontend does NOT
 * pass a tenant id; that would be spoofable.
 */
import API from "./axios";

const BASE = "/api/offboarding";

// ───────────────────────────────────────────────────────────────────────
// CASE CRUD + LIFECYCLE
// ───────────────────────────────────────────────────────────────────────
export const listCases = (params = {}) =>
  API.get(`${BASE}/cases`, { params }).then((r) => r.data);

export const getCase = (caseId) =>
  API.get(`${BASE}/cases/${caseId}`).then((r) => r.data);

export const createCase = (body) =>
  API.post(`${BASE}/cases`, body).then((r) => r.data);

export const updateCase = (caseId, body) =>
  API.patch(`${BASE}/cases/${caseId}`, body).then((r) => r.data);

export const submitCase    = (caseId, body = {}) => API.post(`${BASE}/cases/${caseId}/submit`, body).then((r) => r.data);
export const approveCase   = (caseId, body = {}) => API.post(`${BASE}/cases/${caseId}/approve`, body).then((r) => r.data);
export const cancelCase    = (caseId, reason)    => API.post(`${BASE}/cases/${caseId}/cancel`, { reason }).then((r) => r.data);
export const reverseCase   = (caseId, reason)    => API.post(`${BASE}/cases/${caseId}/reverse`, { reason }).then((r) => r.data);
export const beginCase     = (caseId)            => API.post(`${BASE}/cases/${caseId}/begin`).then((r) => r.data);
export const closeAccess   = (caseId)            => API.post(`${BASE}/cases/${caseId}/close-access`).then((r) => r.data);
export const retryAccess   = (caseId)            => API.post(`${BASE}/cases/${caseId}/retry-access`).then((r) => r.data);
export const finalReview   = (caseId)            => API.post(`${BASE}/cases/${caseId}/final-review`).then((r) => r.data);
export const closePayroll  = (caseId, body = {}) => API.post(`${BASE}/cases/${caseId}/close-payroll`, body).then((r) => r.data);
export const closeBilling  = (caseId, body = {}) => API.post(`${BASE}/cases/${caseId}/close-billing`, body).then((r) => r.data);
export const reviewLegalHold = (caseId)          => API.post(`${BASE}/cases/${caseId}/review-legal-hold`).then((r) => r.data);
export const setupFormerAccess = (caseId)        => API.post(`${BASE}/cases/${caseId}/setup-former-access`).then((r) => r.data);
export const generateCert  = (caseId)            => API.post(`${BASE}/cases/${caseId}/generate-certificate`).then((r) => r.data);
export const completeCase  = (caseId)            => API.post(`${BASE}/cases/${caseId}/complete`).then((r) => r.data);
export const addBlocker    = (caseId, body)      => API.post(`${BASE}/cases/${caseId}/blockers`, body).then((r) => r.data);
export const addWaiver     = (caseId, body)      => API.post(`${BASE}/cases/${caseId}/waivers`, body).then((r) => r.data);
export const unblockCase   = (caseId)            => API.post(`${BASE}/cases/${caseId}/unblock`).then((r) => r.data);

export const getReadiness  = (caseId) => API.get(`${BASE}/cases/${caseId}/readiness`).then((r) => r.data);
export const getTimeline   = (caseId) => API.get(`${BASE}/cases/${caseId}/timeline`).then((r) => r.data);
export const getAudit      = (caseId) => API.get(`${BASE}/cases/${caseId}/audit`).then((r) => r.data);
export const getBlockers   = (caseId) => API.get(`${BASE}/cases/${caseId}/blockers`).then((r) => r.data);

// ───────────────────────────────────────────────────────────────────────
// SECURITY API (Tier-0 enforcement layer)
// ───────────────────────────────────────────────────────────────────────
export const revokeAccess      = (body)   => API.post(`${BASE}/revoke-access`, body).then((r) => r.data);
export const retryRevocation   = (caseId) => API.post(`${BASE}/retry-revocation`, { case_id: caseId }).then((r) => r.data);
export const listReconciliation = (params = {}) => API.get(`${BASE}/reconciliation`, { params }).then((r) => r.data);
export const listSecurityEvents = (params = {}) => API.get(`${BASE}/security-events`, { params }).then((r) => r.data);

// ───────────────────────────────────────────────────────────────────────
// GOVERNANCE API (legal hold, retention, certificate, evidence, deletion)
// ───────────────────────────────────────────────────────────────────────
const GOV = `${BASE}/governance`;

export const grantFormerAccess   = (body) => API.post(`${GOV}/former-access`, body).then((r) => r.data);
export const revokeFormerAccess  = (caseId) => API.delete(`${GOV}/former-access`, { params: { case_id: caseId } }).then((r) => r.data);
export const applyLegalHold      = (body) => API.post(`${GOV}/legal-hold`, body).then((r) => r.data);
export const releaseLegalHold    = (body) => API.delete(`${GOV}/legal-hold`, { data: body }).then((r) => r.data);
export const assignRetention     = (body) => API.post(`${GOV}/retention`, body).then((r) => r.data);
export const listPolicies        = ()     => API.get(`${GOV}/retention/policies`).then((r) => r.data);
export const publishPolicy       = (body) => API.post(`${GOV}/retention/policies/version`, body).then((r) => r.data);
export const getCertificate      = (caseId) => API.get(`${GOV}/certificate`, { params: { case_id: caseId } }).then((r) => r.data);
export const listEvidencePackages = (caseId, packageType) => API.get(`${GOV}/evidence-package`, { params: { case_id: caseId, package_type: packageType } }).then((r) => r.data);
export const buildLegalEvidence  = (caseId, manifestExtra) => API.post(`${GOV}/evidence-package/legal`, { case_id: caseId, manifest_extra: manifestExtra }).then((r) => r.data);
export const buildAuditEvidence  = (caseId) => API.post(`${GOV}/evidence-package/audit`, { case_id: caseId }).then((r) => r.data);

export const openDeletionReview    = (body)         => API.post(`${GOV}/deletion-review`, body).then((r) => r.data);
export const approveDeletionReview = (id, body)     => API.post(`${GOV}/deletion-review/${id}/approve`, body).then((r) => r.data);
export const denyDeletionReview    = (id, reason)   => API.post(`${GOV}/deletion-review/${id}/deny`, { reason }).then((r) => r.data);
export const listDeletionReviews   = (params = {})  => API.get(`${GOV}/deletion-review`, { params }).then((r) => r.data);

// ───────────────────────────────────────────────────────────────────────
// HELPERS — UI metadata mapping
// ───────────────────────────────────────────────────────────────────────
export const CASE_STATE_LABEL = {
  draft: "Draft",
  pending_approval: "Pending approval",
  approved: "Approved",
  scheduled: "Scheduled",
  in_progress: "In progress",
  access_closure_pending: "Access closure",
  access_revocation_failed: "Revocation failed",
  final_review_pending: "Final review",
  payroll_closure_pending: "Payroll closure",
  billing_closure_pending: "Billing closure",
  legal_hold_review_pending: "Legal hold review",
  former_access_pending: "Former access",
  blocked: "Blocked",
  completed: "Completed",
  reversed: "Reversed",
  cancelled: "Cancelled",
};

// Status → Tailwind utility classes for the status chip. Kept centralised so
// the same case shows the same colour everywhere it appears.
export const CASE_STATE_COLOR = {
  draft: "bg-slate-100 text-slate-700 ring-slate-200",
  pending_approval: "bg-amber-100 text-amber-800 ring-amber-200",
  approved: "bg-sky-100 text-sky-800 ring-sky-200",
  scheduled: "bg-indigo-100 text-indigo-800 ring-indigo-200",
  in_progress: "bg-blue-100 text-blue-800 ring-blue-200",
  access_closure_pending: "bg-purple-100 text-purple-800 ring-purple-200",
  access_revocation_failed: "bg-red-100 text-red-800 ring-red-200",
  final_review_pending: "bg-violet-100 text-violet-800 ring-violet-200",
  payroll_closure_pending: "bg-cyan-100 text-cyan-800 ring-cyan-200",
  billing_closure_pending: "bg-cyan-100 text-cyan-800 ring-cyan-200",
  legal_hold_review_pending: "bg-amber-100 text-amber-800 ring-amber-200",
  former_access_pending: "bg-teal-100 text-teal-800 ring-teal-200",
  blocked: "bg-rose-100 text-rose-800 ring-rose-200",
  completed: "bg-emerald-100 text-emerald-800 ring-emerald-200",
  reversed: "bg-slate-100 text-slate-700 ring-slate-200",
  cancelled: "bg-slate-100 text-slate-500 ring-slate-200",
};

export const RISK_COLOR = {
  low:      "bg-emerald-100 text-emerald-700 ring-emerald-200",
  standard: "bg-slate-100 text-slate-700 ring-slate-200",
  elevated: "bg-amber-100 text-amber-800 ring-amber-200",
  high:     "bg-orange-100 text-orange-800 ring-orange-200",
  critical: "bg-red-100 text-red-800 ring-red-200",
};

export const REGISTER_FILTERS = [
  { key: "active",         label: "Active",            params: { status: "in_progress" } },
  { key: "scheduled",      label: "Scheduled",         params: { status: "scheduled" } },
  { key: "blocked",        label: "Blocked",           params: { status: "blocked" } },
  { key: "completed",      label: "Completed",         params: { status: "completed" } },
  { key: "legal_hold",     label: "Legal hold",        params: { legal_hold_status: "active" } },
  { key: "payroll",        label: "Payroll pending",   params: { status: "payroll_closure_pending" } },
  { key: "billing",        label: "Billing pending",   params: { status: "billing_closure_pending" } },
  { key: "security_exit",  label: "Security exit",     params: { status: "access_revocation_failed" } },
  { key: "former_access",  label: "Former access",     params: { status: "former_access_pending" } },
];

/** The redaction marker the backend substitutes for sensitive fields the
 * caller may not view (termination_reason, legal_hold_notes, etc.). We
 * compare to this exact string so a real value never accidentally renders
 * as redacted. Keep in sync with `rbac.REDACTED` server-side. */
export const REDACTED_MARKER = "***redacted***";
export const isRedacted = (v) => v === REDACTED_MARKER;
