import React from "react";

/**
 * Early Clock Out modal (Controlled Clock-Out — Phase 2).
 *
 * Shown when the backend returns `EARLY_CLOCK_OUT_REASON_REQUIRED`. The
 * employee must TYPE a reason (no dropdowns, no radios, no predefined reasons).
 * Reason is required, trimmed, 15–1000 chars. The parent owns the state and
 * network call; this component is purely presentational + local validation.
 *
 * Props:
 *   reason        current textarea value (string)
 *   onReasonChange(value)
 *   error         server/validation error to show (string | "")
 *   submitting    disables buttons while the re-submit is in flight
 *   onCancel()    close without clocking out (employee stays clocked in)
 *   onSubmit()    submit the reason and complete the clock-out
 *
 * The modal deliberately shows NO time breakdown (worked / required / short by).
 * The prompt for a reason is the whole point; the numbers made the dialog tall
 * enough to push the textarea and buttons off-screen on short windows.
 */
/**
 * TEMPORARILY DISABLED: the Early Clock Out feature is currently inactive.
 * This component and every caller are intentionally KEPT so the feature can be
 * restored later — the callers just never open the modal while this is false.
 * The backend gate is off too (see EARLY_CLOCK_OUT_FEATURE_ENABLED in
 * backend/app/employees/router.py), so the server no longer returns
 * EARLY_CLOCK_OUT_REASON_REQUIRED at all. Set BOTH back to true/True to
 * re-enable the feature.
 */
export const EARLY_CLOCK_OUT_ENABLED = false;

export const EARLY_REASON_MIN = 15;
export const EARLY_REASON_MAX = 1000;

export default function EarlyClockOutModal({
  reason = "",
  onReasonChange,
  error = "",
  submitting = false,
  onCancel,
  onSubmit,
}) {
  const trimmedLen = (reason || "").trim().length;
  const tooShort = trimmedLen < EARLY_REASON_MIN;
  const canSubmit = !submitting && !tooShort;

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="zt-early-title"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10002,
        background: "rgba(0,0,0,0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxWidth: 460,
          // Never let the dialog grow past the window — on a short Electron
          // window the buttons used to sit below the fold with no way to reach
          // them, trapping the worker in the modal.
          maxHeight: "calc(100vh - 40px)",
          overflowY: "auto",
          background: "#ffffff",
          borderRadius: 16,
          border: "1px solid #FCA5A5",
          padding: 26,
          boxShadow: "0 24px 60px rgba(0,0,0,0.3)",
          color: "#1F2937",
        }}
      >
        <h2
          id="zt-early-title"
          style={{ fontSize: 18, fontWeight: 800, margin: "0 0 8px", color: "#991B1B" }}
        >
          Early Clock Out
        </h2>
        <p style={{ fontSize: 13.5, lineHeight: 1.55, color: "#4B5563", margin: "0 0 12px" }}>
          You have not completed today's required working hours. Please explain
          why you are leaving before completing your required working hours.
        </p>

        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 8,
            background: "#FFFBEB",
            border: "1px solid #FDE68A",
            borderRadius: 8,
            padding: "10px 12px",
            margin: "0 0 16px",
          }}
        >
          <span style={{ fontSize: 14, lineHeight: 1.4, flexShrink: 0 }} aria-hidden="true">⚠️</span>
          <p style={{ fontSize: 12.5, lineHeight: 1.5, color: "#92400E", margin: 0, fontWeight: 600 }}>
            This reason will be reviewed by HR. Please provide a genuine
            explanation.
          </p>
        </div>

        <label
          htmlFor="zt-early-reason"
          style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 6 }}
        >
          Reason for Early Clock Out{" "}
          <span style={{ color: "#DC2626" }}>*</span>
        </label>
        <textarea
          id="zt-early-reason"
          value={reason}
          onChange={(e) => onReasonChange && onReasonChange(e.target.value)}
          maxLength={EARLY_REASON_MAX}
          rows={5}
          disabled={submitting}
          placeholder={`Type your reason (minimum ${EARLY_REASON_MIN} characters)…`}
          style={{
            width: "100%",
            boxSizing: "border-box",
            resize: "vertical",
            padding: "10px 12px",
            fontSize: 14,
            borderRadius: 8,
            border: `1px solid ${error ? "#DC2626" : "#D1D5DB"}`,
            outline: "none",
            fontFamily: "inherit",
          }}
        />
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontSize: 11.5,
            color: "#6B7280",
            margin: "6px 2px 0",
          }}
        >
          <span>{tooShort ? `At least ${EARLY_REASON_MIN} characters` : " "}</span>
          <span>
            {(reason || "").length}/{EARLY_REASON_MAX}
          </span>
        </div>

        {error ? (
          <div
            role="alert"
            style={{
              marginTop: 10,
              background: "#FEF2F2",
              border: "1px solid #FCA5A5",
              borderRadius: 8,
              padding: "8px 12px",
              fontSize: 12.5,
              color: "#991B1B",
            }}
          >
            {error}
          </div>
        ) : null}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 20 }}>
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            style={{
              padding: "9px 18px",
              borderRadius: 8,
              border: "1px solid #D1D5DB",
              background: "#ffffff",
              color: "#374151",
              fontWeight: 600,
              fontSize: 13,
              cursor: submitting ? "not-allowed" : "pointer",
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={!canSubmit}
            style={{
              padding: "9px 18px",
              borderRadius: 8,
              border: "none",
              background: canSubmit ? "#DC2626" : "#FCA5A5",
              color: "#ffffff",
              fontWeight: 700,
              fontSize: 13,
              cursor: canSubmit ? "pointer" : "not-allowed",
            }}
          >
            {submitting ? "Clocking out…" : "Clock Out"}
          </button>
        </div>
      </div>
    </div>
  );
}
