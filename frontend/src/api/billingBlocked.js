/**
 * HTTP 402 — the organization's payment is overdue and writes are blocked.
 *
 * WHY THIS EXISTS
 * ---------------
 * The backend suspends a non-paying organization to read-only after the 14-day dunning
 * grace period (app/billing/suspension.py) and answers restricted writes with 402. The
 * frontend had **no** handling for it: `grep 402` returned nothing. A paying customer
 * whose card had failed saw their saves fail with a generic error, no explanation, and
 * no route to fix it — which is the one failure mode a billing system must never have,
 * because the customer cannot pay their way out of a problem they cannot see.
 *
 * WHY AN EVENT RATHER THAN A REDIRECT
 * -----------------------------------
 * 402 is deliberately *not* the 403 organization-suspended case, which routes through
 * logout. A billing-suspended tenant still has a working account: everyone can sign in,
 * read, and clock in and out, and the billing pages themselves stay writable so the
 * customer can pay. Logging them out would remove the only route to recovery.
 *
 * So this broadcasts rather than navigates. Any surface can listen; nothing is forced.
 * The promise is still rejected by the interceptor, so the calling code's own error
 * handling is untouched — this adds an explanation, it does not swallow the failure.
 */

export const BILLING_BLOCKED_EVENT = "zoiko:billing-blocked";

/** Where the customer goes to fix it. The billing pages are exempt from the 402 gate. */
export const BILLING_RECOVERY_PATH = "/settings/billing-subscription";

/**
 * Announce that a write was refused for billing reasons.
 *
 * Never throws: a browser without CustomEvent, or a non-browser test environment, must
 * not turn a billing notice into a crash on top of the original failed request.
 */
export function announceBillingBlocked(detail = {}) {
  try {
    window.dispatchEvent(
      new CustomEvent(BILLING_BLOCKED_EVENT, {
        detail: {
          message: detail.message || null,
          path: detail.path || null,
          at: detail.at || Date.now(),
        },
      })
    );
    return true;
  } catch {
    return false;
  }
}

/** Subscribe; returns an unsubscribe function. */
export function onBillingBlocked(handler) {
  if (typeof window === "undefined" || !window.addEventListener) return () => {};
  window.addEventListener(BILLING_BLOCKED_EVENT, handler);
  return () => window.removeEventListener(BILLING_BLOCKED_EVENT, handler);
}

/**
 * Is this response a billing block?
 *
 * Kept as a predicate so both fetch paths (api/axios.js and api/authFetch.js) decide
 * the same way, and so the rule is unit-testable without a live request.
 */
export function isBillingBlocked(status) {
  return status === 402;
}
