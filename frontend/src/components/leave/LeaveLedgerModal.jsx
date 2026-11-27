/**
 * Leave Ledger: the admin review modal for one leave request (Leave
 * Management → View / Review).
 *
 * Presentational. AdminLeave owns the data and the actions: the request row,
 * the employee's balances (GET /leave/admin/balances/{user_id}), the audit
 * history (GET /leave/{id}/history), the comp-leave worked-date evidence, and
 * updateStatus. Layout: Leave Balance across the top; Leave Details and
 * Description on the left; Audit & Workflow, HR / Payroll Actions and Balance
 * Breakdown on the right.
 */
import React, { useEffect } from "react";
import {
  Info, FileText, Layers, Briefcase, ChartColumn, Flag, Lock, Check, X, Pencil,
  Clock, Paperclip, Search,
} from "lucide-react";
import TemverisLogo from "../../assets/Temveris_Logo.svg";
import TemverisLogoWhite from "../../assets/Temveris_Logo_White.svg";
import { fmtDateTime } from "../../lib/orgTime";
import LeaveBalanceCard from "./LeaveBalanceCard";
import WorkedDateChips from "./WorkedDateChips";
import { poolView, workflowSteps, policyYearLabel } from "./leaveLedgerModel";

const UNDECIDED = ["submitted", "under_review"];

// "11/11/2026, 5:33:59 am" on the organization's clock.
const stamp = (v) => fmtDateTime(v, {
  day: "2-digit", month: "2-digit", year: "numeric",
  hour: "numeric", minute: "2-digit", second: "2-digit",
});
const fmt2 = (n) => (Number.isFinite(Number(n)) ? Number(n).toFixed(2) : "—");

const LEDGER_CSS = `
  .zt-ll-grid { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr); gap: 16px; align-items: start; }
  .zt-ll-col  { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .zt-ll-btn  { transition: filter .15s ease, transform .15s ease; }
  .zt-ll-btn:not(:disabled):hover { filter: brightness(1.06); transform: translateY(-1px); }
  .zt-ll-btn:disabled { opacity: .6; cursor: default; }
  .zt-ll-btn:focus-visible, .zt-ll-close:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }
  @media (max-width: 860px) { .zt-ll-grid { grid-template-columns: minmax(0, 1fr); } }
`;

function LedgerCard(props) {
  const { C, icon: Icon, title, children, testId } = props;
  return (
    <section data-testid={testId} style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: 16,
      padding: "16px 20px 18px", boxShadow: "0 1px 2px rgba(16,24,40,.05)", minWidth: 0,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, paddingBottom: 12, marginBottom: 4, borderBottom: `1px solid ${C.borderLight || C.border}` }}>
        <span aria-hidden style={{ width: 36, height: 36, borderRadius: 10, background: C.greenBg || "#E8F7EE", display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <Icon size={20} color={C.green || "#2F8F5C"} />
        </span>
        <h3 style={{ margin: 0, fontSize: 18, fontWeight: 800, color: C.text, letterSpacing: "-.2px" }}>{title}</h3>
      </div>
      {children}
    </section>
  );
}

function DetailRow({ C, label, children, last = false }) {
  return (
    <div style={{
      display: "grid", gridTemplateColumns: "minmax(108px, 38%) minmax(0, 1fr)", gap: 12,
      padding: "10px 0", alignItems: "center",
      borderBottom: last ? "none" : `1px solid ${C.borderLight || C.border}`,
    }}>
      <span style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{label}</span>
      <span style={{ fontSize: 14, color: C.text, minWidth: 0, overflowWrap: "anywhere" }}>{children}</span>
    </div>
  );
}

const TONES = {
  done:     { color: "#16A34A", Icon: Check },
  declined: { color: "#DC2626", Icon: X },
  info:     { color: "#2563EB", Icon: Pencil },
  pending:  { color: null,      Icon: Clock },
};

function Timeline({ C, T, steps }) {
  return (
    <ol data-testid="ledger-timeline" style={{ listStyle: "none", margin: "10px 0 0", padding: 0 }}>
      {steps.map((s, i) => {
        const tone = TONES[s.tone] || TONES.done;
        const ToneIcon = tone.Icon;
        const last = i === steps.length - 1;
        const next = steps[i + 1];
        return (
          <li key={s.key} style={{ display: "flex", gap: 12 }} data-step={s.kind}>
            <span aria-hidden style={{ display: "flex", flexDirection: "column", alignItems: "center", flexShrink: 0 }}>
              <span style={{
                width: 24, height: 24, borderRadius: "50%", display: "inline-flex", alignItems: "center", justifyContent: "center",
                background: tone.color || "transparent", border: tone.color ? "none" : `2px solid ${C.border}`,
                boxSizing: "border-box",
              }}>
                <ToneIcon size={14} color={tone.color ? "#fff" : C.sub} strokeWidth={3} />
              </span>
              {!last && (
                <span style={{
                  flex: 1, width: 2, minHeight: 14, marginTop: 2, marginBottom: 2,
                  background: next?.tone === "pending" ? "transparent" : (tone.color || C.border),
                  borderLeft: next?.tone === "pending" ? `2px dashed ${C.border}` : "none",
                }} />
              )}
            </span>
            <span style={{ paddingBottom: last ? 0 : 14, minWidth: 0 }}>
              <span style={{ display: "block", fontSize: 14.5, fontWeight: 700, color: s.tone === "pending" ? C.sub : C.text, lineHeight: 1.3 }}>{s.label}</span>
              {s.tone !== "pending" && (
                <span style={{ display: "block", fontSize: 13, color: C.sub, marginTop: 2 }}>
                  {s.at ? stamp(s.at) : (T.wfTimeNotRecorded || "Time not recorded")}
                </span>
              )}
              {s.comment && (
                <span style={{ display: "block", fontSize: 13, color: C.text, marginTop: 4, fontStyle: "italic", overflowWrap: "anywhere" }}>“{s.comment}”</span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

const pill = (bg, color, border) => ({
  display: "inline-flex", alignItems: "center", gap: 6, background: bg, color,
  border: `1px solid ${border}`, borderRadius: 10, padding: "5px 12px", fontSize: 13, fontWeight: 600,
});

export default function LeaveLedgerModal({
  C, T = {}, isDark = false, request, typeIcon = "📋", statusBadge,
  balance = {}, history = {}, workedEvidence = {},
  canAct = false, comment = "", commentError = "", onCommentChange,
  actioning = false, onAction, attachmentError, onOpenAttachment, onClose,
}) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!request) return null;
  const undecided = UNDECIDED.includes(request.status);
  const steps = workflowSteps(request, history.items, T);
  const pool = poolView(balance.row, balance.all, request.leave_type);
  const unit = T.leaveCardDaysUnit || "days";
  const days = Number(request.days);
  const hasFlags = request.hr_flag || request.is_lwp || request.has_attachment;

  const actionBtn = (bg, color = "#fff", border = "none") => ({
    flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8,
    padding: "12px 14px", background: bg, color, border, borderRadius: 10,
    fontWeight: 700, fontSize: 14, cursor: "pointer", fontFamily: "inherit",
  });

  return (
    <div role="dialog" aria-modal="true" aria-labelledby="leave-ledger-title" onClick={onClose}
      style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.55)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <style>{LEDGER_CSS}</style>
      <div onClick={(e) => e.stopPropagation()} data-testid="leave-ledger-modal"
        style={{
          background: isDark ? C.bg : "#F3F5F8", color: C.text, borderRadius: 18, width: "100%", maxWidth: 1100,
          maxHeight: "92vh", display: "flex", flexDirection: "column", overflow: "hidden",
          boxShadow: "0 24px 64px rgba(0,0,0,0.28)", border: `1px solid ${C.border}`,
        }}>
        <header style={{ background: C.surface, borderBottom: `1px solid ${C.border}`, padding: "14px 22px", display: "flex", alignItems: "center", gap: 16, flexShrink: 0, flexWrap: "wrap" }}>
          <img src={isDark ? TemverisLogoWhite : TemverisLogo} alt="Temveris" style={{ height: 28, width: "auto", display: "block" }} />
          <span aria-hidden style={{ width: 1, height: 30, background: C.border }} />
          <h2 id="leave-ledger-title" style={{ margin: 0, fontSize: 23, fontWeight: 800, color: C.text, letterSpacing: "-.3px" }}>
            {T.leaveLedger || "Leave Ledger"}
          </h2>
          <span data-testid="ledger-entry-pill" style={{ background: C.brandBg || "#E7F0F6", color: C.brand || "#11496A", borderRadius: 999, padding: "5px 14px", fontSize: 13, fontWeight: 700, whiteSpace: "nowrap" }}>
            {T.ledgerEntry || "Ledger Entry"} #{request.id}
          </span>
          <span style={{ flex: 1 }} />
          <button type="button" className="zt-ll-close" onClick={onClose} aria-label={T.close || "Close"}
            style={{ width: 38, height: 38, borderRadius: 10, border: "none", background: "transparent", color: C.text, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
            <X size={24} aria-hidden />
          </button>
        </header>

        <div style={{ overflowY: "auto", padding: 20, display: "flex", flexDirection: "column", gap: 16 }}>
          <LeaveBalanceCard C={C} T={T} balance={balance.row} balances={balance.all}
            leaveType={request.leave_type} request={request}
            loading={balance.loading} error={balance.error} showPoolBreakdown={false} />

          <div className="zt-ll-grid">
            <div className="zt-ll-col">
              <LedgerCard C={C} icon={Info} title={T.leaveDetails || "Leave Details"} testId="ledger-details">
                <DetailRow C={C} label={T.employee || "Employee"}>{request.name}{request.email ? ` (${request.email})` : ""}</DetailRow>
                <DetailRow C={C} label={T.department || "Department"}>{request.department || "—"}</DetailRow>
                <DetailRow C={C} label={T.leaveType || "Leave Type"}><span aria-hidden>{typeIcon}</span> {request.leave_type}</DetailRow>
                <DetailRow C={C} label={T.fromTo || "From → To"}>{request.from_date} → {request.to_date}</DetailRow>
                <DetailRow C={C} label={T.days || "Days"}>{request.days} {days !== 1 ? (T.days || "Days") : (T.day || "Day")}</DetailRow>
                <DetailRow C={C} label={T.status || "Status"}>{statusBadge}</DetailRow>
                <DetailRow C={C} label={T.appliedOn || "Applied On"} last>{request.created_at ? stamp(request.created_at) : "—"}</DetailRow>
                {/* Compensatory Leave: the dates the employee says they worked,
                    with what Temveris recorded on each. */}
                <WorkedDateChips dates={request.worked_dates} userId={request.user_id} T={T}
                  evidence={workedEvidence.dates} loading={workedEvidence.loading} />
              </LedgerCard>

              <LedgerCard C={C} icon={FileText} title={T.description || "Description"} testId="ledger-description">
                <p style={{ margin: "10px 0 0", fontSize: 14.5, lineHeight: 1.6, color: C.text, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {request.description || "—"}
                </p>
              </LedgerCard>
            </div>

            <div className="zt-ll-col">
              <LedgerCard C={C} icon={Layers} title={T.auditWorkflow || "Audit & Workflow"} testId="ledger-workflow">
                {history.loading ? (
                  <p style={{ margin: "10px 0 0", fontSize: 13, color: C.sub }}>{T.loadingHistory || "Loading history…"}</p>
                ) : (
                  <>
                    <Timeline C={C} T={T} steps={steps} />
                    {history.error && (
                      <p style={{ margin: "10px 0 0", fontSize: 12.5, color: C.sub }}>{T.wfHistoryUnavailable || "Full history couldn't be loaded; showing what the request records."}</p>
                    )}
                  </>
                )}
              </LedgerCard>

              <LedgerCard C={C} icon={Briefcase} title={T.hrPayrollActions || "HR / Payroll Actions"} testId="ledger-actions">
                <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}>
                  {hasFlags && (
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      {request.hr_flag && (
                        <span style={pill(isDark ? "rgba(249,115,22,.14)" : "#FFF7ED", isDark ? "#FDBA74" : "#C2410C", isDark ? "rgba(249,115,22,.35)" : "#FED7AA")}>
                          <Flag size={14} aria-hidden /> {T.hrReviewFlagged || "HR Review Flagged"}
                        </span>
                      )}
                      {request.is_lwp && (
                        <span style={pill(isDark ? "rgba(124,58,237,.16)" : "#F5F3FF", isDark ? "#C4B5FD" : "#7C3AED", isDark ? "rgba(124,58,237,.4)" : "#C4B5FD")}>
                          {T.lwpApplied || "LWP Applied"}
                        </span>
                      )}
                      {/* Opened through the authenticated GET /leave/{id}/attachment
                          (see utils/leaveAttachment); never a direct file link. */}
                      {request.has_attachment && (
                        <button type="button" onClick={onOpenAttachment} className="zt-ll-btn"
                          style={{ ...pill(isDark ? "rgba(37,99,235,.16)" : "#EFF6FF", isDark ? "#93C5FD" : "#1D4ED8", isDark ? "rgba(37,99,235,.4)" : "#93C5FD"), cursor: "pointer", fontFamily: "inherit" }}>
                          <Paperclip size={14} aria-hidden /> {request.attachment_name || (T.viewDocument || "View Document")}
                        </button>
                      )}
                    </div>
                  )}
                  {attachmentError && (
                    <p style={{ margin: 0, fontSize: 12.5, color: C.red || "#B91C1C", fontWeight: 600 }}>{attachmentError}</p>
                  )}

                  {undecided && !canAct && (
                    <div style={{ padding: "12px 14px", borderRadius: 10, border: `1px solid ${C.border}`, background: C.bg || "transparent", color: C.sub, fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
                      <Lock size={15} aria-hidden />
                      {T.leaveApprovalsViewOnly || "View only — approving leave is available on a higher plan."}
                    </div>
                  )}

                  {undecided && canAct && (
                    <>
                      <label htmlFor="ledger-comment" style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                        {T.comment || "Comment"}
                        <span style={{ color: C.sub, fontWeight: 400, marginLeft: 4 }}>({T.commentOptional || "optional"})</span>
                      </label>
                      <textarea id="ledger-comment" value={comment} onChange={(e) => onCommentChange?.(e.target.value)}
                        placeholder={T.commentPlaceholder || "Optional comment for the employee..."} rows={3}
                        style={{
                          width: "100%", padding: "10px 12px", border: `1px solid ${commentError ? "#EF4444" : C.border}`,
                          borderRadius: 10, fontSize: 13, resize: "vertical", boxSizing: "border-box", fontFamily: "inherit",
                          outline: "none", background: C.surface, color: C.text, colorScheme: isDark ? "dark" : "light",
                        }} />
                      {commentError && <p style={{ margin: 0, fontSize: 12, color: C.red || "#B91C1C", fontWeight: 600 }}>{commentError}</p>}
                      <div style={{ display: "flex", gap: 10 }}>
                        <button type="button" className="zt-ll-btn" disabled={actioning} onClick={() => onAction?.("approved", comment)} style={actionBtn("#059669")}>
                          <Check size={16} aria-hidden /> {T.approveBtn || "Approve"}
                        </button>
                        <button type="button" className="zt-ll-btn" disabled={actioning} onClick={() => onAction?.("rejected", comment)} style={actionBtn("#DC2626")}>
                          <X size={16} aria-hidden /> {T.rejectBtn || "Decline"}
                        </button>
                      </div>
                      {request.status === "submitted" && (
                        <button type="button" className="zt-ll-btn" disabled={actioning}
                          onClick={() => onAction?.("under_review", comment || (T.markedForReview || "Marked for review"))}
                          style={actionBtn(isDark ? "rgba(249,115,22,.12)" : "#FFF7ED", isDark ? "#FDBA74" : "#C2410C", `1px solid ${isDark ? "rgba(249,115,22,.35)" : "#FED7AA"}`)}>
                          <Search size={15} aria-hidden /> {T.sendToHrReview || "Send to HR Review"}
                        </button>
                      )}
                    </>
                  )}

                  {request.status === "approved" && (
                    <button type="button" className="zt-ll-btn" disabled={actioning}
                      onClick={() => onAction?.("locked_for_payroll", T.lockedForPayrollProcessing || "Locked for payroll processing")}
                      style={actionBtn(isDark ? "#1F5F86" : "#11496A")}>
                      <Lock size={16} aria-hidden /> {T.lockForPayroll || "Lock for Payroll"}
                    </button>
                  )}
                  {request.status === "locked_for_payroll" && (
                    <p style={{ margin: 0, fontSize: 13, color: C.sub, display: "flex", alignItems: "center", gap: 8 }}>
                      <Lock size={15} aria-hidden /> {T.ledgerLockedNote || "Locked for payroll processing."}
                    </p>
                  )}
                  {request.status === "rejected" && (
                    <p style={{ margin: 0, fontSize: 13, color: C.sub }}>{T.ledgerDeclinedNote || "Declined. No further actions."}</p>
                  )}
                </div>
              </LedgerCard>

              <LedgerCard C={C} icon={ChartColumn} title={T.balanceBreakdown || "Balance Breakdown"} testId="ledger-breakdown">
                <DetailRow C={C} label={T.policyYear || "Policy Year"}>{policyYearLabel(balance.leaveYear, request)}</DetailRow>
                <DetailRow C={C} label={T.ledgerSource || "Source"}>{T.leaveRequestSource || "Leave Request"}</DetailRow>
                <DetailRow C={C} label={T.recordType || "Record Type"}>{T.leaveLedger || "Leave Ledger"}</DetailRow>
                <DetailRow C={C} label={T.ledgerEntry || "Ledger Entry"} last={!(pool && pool.rows.length)}>#{request.id}</DetailRow>
                {pool && pool.rows.length > 0 && (
                  <div data-testid="leave-balance-pool-breakdown" style={{ marginTop: 6 }}>
                    <div style={{ fontSize: 12, fontWeight: 700, color: C.sub, letterSpacing: ".3px", textTransform: "uppercase", padding: "8px 0 2px" }}>
                      {T.leaveCardTakenFromShared || "Taken from the shared balance this year"}
                    </div>
                    {pool.rows.map((r, i) => (
                      <DetailRow key={r.type} C={C} label={r.type} last={i === pool.rows.length - 1}>
                        <strong>{fmt2(r.used)}</strong> {unit}
                        {r.pending > 0 && ` (${fmt2(r.pending)} ${T.pendingLower || "pending"})`}
                      </DetailRow>
                    ))}
                  </div>
                )}
              </LedgerCard>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
