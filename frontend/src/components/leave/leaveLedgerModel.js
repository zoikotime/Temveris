/**
 * Pure helpers behind the admin Leave Ledger modal (LeaveLedgerModal.jsx) and
 * its Leave Balance card (LeaveBalanceCard.jsx). No React here, so the view
 * files only export components and the arithmetic is testable on its own.
 */

const num = (n) => Number(n) || 0;

/**
 * The shared-pool view of a pooled leave type, or null when the type has its
 * own balance (pool off, HR override, or an older backend without the pool
 * row). Annual / Casual / Sick / … draw from one balance sized by the
 * employee's Annual entitlement (`balances._shared_pool`), so a pooled type's
 * `remaining` cannot be explained by its own total / used.
 *
 * `used` here excludes pending, so entitlement − used − pending = what is left
 * in the pool. `rows` lists every pooled type that has taken days this year.
 */
export function poolView(balance, balances, leaveType) {
  const pool = balances?._shared_pool;
  if (!balance?.shared_pool || !pool?.enabled || !(num(pool.total) > 0)) return null;
  const memberTypes = [...new Set([pool.source, ...(pool.members || [])].filter(Boolean))];
  const rows = memberTypes
    .map((lt) => ({ type: lt, used: num(balances[lt]?.used), pending: num(balances[lt]?.pending) }))
    .filter((r) => r.used > 0);
  const pending = rows.reduce((s, r) => s + r.pending, 0);
  const others = memberTypes.filter((lt) => lt !== leaveType && balances[lt]);
  return {
    total: num(pool.total),
    used: Math.max(0, num(pool.used) - pending),
    pending,
    poolRemaining: num(pool.remaining),
    source: pool.source,
    rows,
    others,
  };
}

// Audit actions written by the leave router (`_log_audit`) → timeline steps.
const ACTIONS = {
  LeaveRequested:        { kind: "submitted",          tone: "done",     label: (T) => T.wfSubmitted || "Submitted" },
  LeaveUnderReview:      { kind: "under_review",       tone: "done",     label: (T) => T.wfUnderReview || "Sent to HR review" },
  LeaveApproved:         { kind: "approved",           tone: "done",     label: (T) => T.wfApproved || "Approved" },
  LeaveRejected:         { kind: "rejected",           tone: "declined", label: (T) => T.wfDeclined || "Declined" },
  LeaveLockedForPayroll: { kind: "locked_for_payroll", tone: "done",     label: (T) => T.wfLocked || "Locked for payroll" },
  LeaveRequestEdited:    { kind: "edited",             tone: "info",     label: (T) => T.wfEdited || "Edited" },
};
const STATUS_ACTION = {
  submitted: "LeaveRequested", under_review: "LeaveUnderReview", approved: "LeaveApproved",
  rejected: "LeaveRejected", locked_for_payroll: "LeaveLockedForPayroll",
};
const UNDECIDED = ["submitted", "under_review"];

/**
 * The Audit & Workflow timeline for one request, oldest first.
 *
 * Built from GET /leave/{id}/history (the audit log). Two gaps are filled from
 * the request itself so the timeline never misses a step: the submission (its
 * `created_at`) when the history has no LeaveRequested entry, and the current
 * status when its change was not audited (shown without a time, never with a
 * made-up one). An undecided request ends with a pending "Awaiting decision".
 */
export function workflowSteps(request, history, T = {}) {
  if (!request) return [];
  const steps = [];
  for (const h of Array.isArray(history) ? history : []) {
    const action = h?.action === "LeaveStatusChanged"
      ? (STATUS_ACTION[h?.payload?.to_status] || h.action)
      : h?.action;
    const def = ACTIONS[action];
    if (!def) continue;
    const comment = typeof h?.payload?.comment === "string" ? h.payload.comment.trim() : "";
    steps.push({
      key: `${def.kind}-${steps.length}`, kind: def.kind, tone: def.tone,
      label: def.label(T), at: h.created_at || null, comment: comment || null,
    });
  }
  if (!steps.some((s) => s.kind === "submitted")) {
    steps.unshift({
      key: "submitted-request", kind: "submitted", tone: "done",
      label: ACTIONS.LeaveRequested.label(T), at: request.created_at || null, comment: null,
    });
  }
  const status = request.status;
  if (status && status !== "submitted" && STATUS_ACTION[status] && !steps.some((s) => s.kind === status)) {
    const def = ACTIONS[STATUS_ACTION[status]];
    const comment = typeof request.review_comment === "string" ? request.review_comment.trim() : "";
    steps.push({
      key: `${status}-current`, kind: status, tone: def.tone,
      label: def.label(T), at: null, comment: comment || null,
    });
  }
  if (UNDECIDED.includes(status)) {
    steps.push({
      key: "awaiting", kind: "awaiting", tone: "pending",
      label: T.wfAwaitingDecision || "Awaiting decision", at: null, comment: null,
    });
  }
  return steps;
}

/** The leave year the balance figures cover ("2026", or "Apr 2026 – Mar 2027"). */
export function policyYearLabel(leaveYear, request) {
  if (leaveYear?.label) return leaveYear.label;
  const from = String(request?.from_date || "");
  return /^\d{4}/.test(from) ? from.slice(0, 4) : "—";
}
