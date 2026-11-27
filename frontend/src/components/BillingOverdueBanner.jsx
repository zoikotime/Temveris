import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  BILLING_RECOVERY_PATH,
  onBillingBlocked,
} from "../api/billingBlocked";

/**
 * Explains an HTTP 402 and offers the way out of it.
 *
 * WHY
 * ---
 * The backend suspends a non-paying organization to read-only after the dunning grace
 * period and answers restricted writes with 402. Before this the frontend had no
 * handling for that status at all, so a customer whose card had failed saw their saves
 * fail with a generic error, no reason given, and nothing pointing at billing. They
 * could not pay their way out of a problem the product never told them about.
 *
 * DESIGN
 * ------
 * A banner, not a redirect. Reads still work and clock-in/out is deliberately exempt
 * from the gate, so yanking the user somewhere else would interrupt work that is still
 * allowed. It appears only after a write is actually refused — no polling, no guessing
 * from subscription state, so it cannot cry wolf.
 *
 * Dismissible, because an employee who cannot fix billing should not be nagged forever;
 * the next refused write brings it back, so the information is never lost.
 */
export default function BillingOverdueBanner() {
  const [blocked, setBlocked] = useState(null);
  const [dismissed, setDismissed] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    // The unsubscribe function is returned so a remount cannot stack listeners.
    return onBillingBlocked((event) => {
      setBlocked(event?.detail || {});
      setDismissed(false);          // a fresh refusal re-opens a dismissed banner
    });
  }, []);

  if (!blocked || dismissed) return null;

  const message =
    blocked.message ||
    "Your workspace is read-only because a payment is overdue. Update your payment method to restore full access.";

  return (
    <div
      role="alert"
      data-testid="billing-overdue-banner"
      style={{
        position: "fixed", top: 0, left: 0, right: 0, zIndex: 9999,
        display: "flex", alignItems: "center", justifyContent: "center",
        gap: 14, flexWrap: "wrap",
        padding: "12px 18px",
        background: "#FEF3F2", borderBottom: "1px solid #FDA29B", color: "#912018",
        fontSize: 13.5, fontWeight: 600,
        fontFamily: "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif",
      }}
    >
      <span style={{ maxWidth: 760 }}>{message}</span>
      <button
        type="button"
        data-testid="billing-overdue-fix"
        onClick={() => {
          setDismissed(true);
          navigate(BILLING_RECOVERY_PATH);
        }}
        style={{
          background: "#B42318", color: "#fff", border: "none",
          borderRadius: 8, padding: "8px 16px", fontWeight: 700, cursor: "pointer",
        }}
      >
        Update payment method
      </button>
      <button
        type="button"
        aria-label="Dismiss"
        data-testid="billing-overdue-dismiss"
        onClick={() => setDismissed(true)}
        style={{
          background: "transparent", border: "none", color: "#912018",
          fontSize: 18, lineHeight: 1, cursor: "pointer", padding: "0 4px",
        }}
      >
        ×
      </button>
    </div>
  );
}
