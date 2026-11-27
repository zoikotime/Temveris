/**
 * Leave Balance card at the top of the admin Leave Ledger modal.
 *
 * Presentational only: the parent fetches GET /leave/admin/balances/{user_id}
 * and passes the row for the requested leave type (`balance`) plus the whole
 * response map (`balances`). Field meanings (backend `_compute_user_balances`):
 *   total     — annual entitlement for the type
 *   accrued   — allocated so far (equals `total` unless accrual pacing differs)
 *   used      — every non-rejected request this leave year, INCLUDING pending
 *   pending   — the part of `used` still awaiting a decision
 *   remaining — what the employee can still take (pending already deducted)
 *
 * Shared pool: when `balance.shared_pool` is true the type draws from one
 * balance with Annual / Casual / Sick / … (`balances._shared_pool`), sized by
 * the employee's Annual entitlement. `remaining` is then capped by that pool,
 * so the type's own total / used cannot explain it. The card switches to the
 * pool's numbers, so every figure on screen adds up:
 * entitlement − used − pending = available. Which types used the pool is listed
 * here, or by the modal's Balance Breakdown when `showPoolBreakdown` is false.
 */
import React from "react";
import { CalendarDays, Calendar } from "lucide-react";
import { poolView } from "./leaveLedgerModel";

const PENDING_STATES = ["submitted", "under_review"];

const fmt = (n) => (Number.isFinite(n) ? n.toFixed(2) : "—");
const num = (n) => Number(n) || 0;
const pct = (part, whole) => (whole > 0 ? Math.max(0, Math.min(100, (part / whole) * 100)) : 0);

// The value element must stay the label's next sibling (tests read it that way).
function Stat({ label, value, unit, color, highlight, C }) {
  return (
    <div style={{
      flex: "1 1 118px", minWidth: 108, padding: "12px 16px", borderRadius: 12,
      background: highlight ? (C.greenBg || "#ECFDF5") : C.surface,
      border: `1px solid ${highlight ? (C.greenBdr || "#A7F3D0") : C.border}`,
    }}>
      <div style={{ fontSize: 13, color: C.text, fontWeight: 700, marginBottom: 6, lineHeight: 1.25 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 800, color: color || C.text, lineHeight: 1.1 }}>{value}</div>
      <div style={{ fontSize: 13, color: C.sub, marginTop: 2 }}>{unit}</div>
    </div>
  );
}

export default function LeaveBalanceCard({
  balance, balances, leaveType, request, loading, error, asOf = new Date(),
  showPoolBreakdown = true, C, T = {},
}) {
  const box = {
    border: `1px solid ${C.border}`, borderRadius: 16, padding: "18px 20px",
    background: C.surface, boxShadow: "0 1px 2px rgba(16,24,40,.05)",
  };
  const title = (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, gap: 12, flexWrap: "wrap" }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 12 }}>
        <span aria-hidden style={{ width: 40, height: 40, borderRadius: 10, background: C.greenBg || "#E8F7EE", display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <CalendarDays size={22} color={C.green || "#2F8F5C"} />
        </span>
        <span style={{ fontSize: 20, fontWeight: 800, color: C.text, letterSpacing: "-.2px" }}>{T.leaveBalance || "Leave Balance"}</span>
      </span>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 13.5, color: C.text, border: `1px solid ${C.border}`, borderRadius: 10, padding: "6px 12px" }}>
        <Calendar size={15} color={C.sub} aria-hidden />
        <span>{(T.asOf || "As of")} {asOf.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}</span>
      </span>
    </div>
  );

  if (loading) {
    return <div style={box} data-testid="leave-balance-card">{title}
      <div style={{ fontSize: 13, color: C.sub }}>{T.loadingBalance || "Loading balance…"}</div></div>;
  }
  if (error || !balance) {
    return <div style={box} data-testid="leave-balance-card">{title}
      <div style={{ fontSize: 13, color: C.sub }}>{T.balanceUnavailable || "Balance unavailable for this leave type."}</div></div>;
  }
  // No annual limit (Unpaid, Company, Compensatory): allowance-minus-used is not
  // a real balance, so show what was taken instead. Same rule as the backend's
  // `uncapped` (unlimited / no_annual_limit / earned).
  if (balance.is_unlimited || balance.no_annual_limit || balance.is_earned) {
    const p = num(balance.pending);
    const u = Math.max(0, num(balance.used) - p);
    return <div style={box} data-testid="leave-balance-card">{title}
      <div style={{ fontSize: 13, color: C.sub }}>{T.unlimitedLeaveType || "This leave type has no annual limit."}</div>
      <div style={{ fontSize: 13, color: C.text, marginTop: 6 }}>
        {(T.leaveCardTakenThisYear || "This year")}: <strong>{fmt(u)}</strong> {T.daysUsed || "days used"} · <strong>{fmt(p)}</strong> {T.pendingLower || "pending"}
      </div></div>;
  }

  const pool      = poolView(balance, balances, leaveType);
  const typeLabel = leaveType || (T.thisLeaveType || "This leave type");
  const total     = pool ? pool.total : num(balance.total);
  const accrued   = num(balance.accrued);
  const pending   = pool ? pool.pending : num(balance.pending);
  const decided   = pool ? pool.used : Math.max(0, num(balance.used) - pending);
  const available = num(balance.remaining);
  // Accrued only says something when it differs from the entitlement; the
  // backend reports them equal unless accrual pacing applies, and a duplicate
  // box is what made the old card's numbers look like they didn't add up.
  const showAccrued = !pool && Number.isFinite(Number(balance.accrued)) && Math.abs(accrued - total) >= 0.05;
  // The type's own cap can bind before the pool does (e.g. Emergency 6 days).
  const cappedByType = pool && available + 0.05 < pool.poolRemaining;
  const thisRequestPending = request && PENDING_STATES.includes(request.status) && num(request.days) > 0;
  const usedPct = pct(decided, total), pendPct = pct(pending, total), availPct = pct(available, total);
  // Card-specific keys: the shared `used` / `days` strings are sentence
  // fragments elsewhere ("used", "Days") and read wrong as a heading / unit.
  const unit = T.leaveCardDaysUnit || "days";
  const note = { fontSize: 12.5, color: C.sub, marginTop: 8, lineHeight: 1.5 };

  return (
    <div style={box} data-testid="leave-balance-card">
      {title}
      {pool && (
        <div style={{ fontSize: 12.5, color: C.text, background: C.brandBg || "#EFF6FF", border: `1px solid ${C.brandBdr || "#BFDBFE"}`,
          borderRadius: 10, padding: "8px 12px", marginBottom: 12, lineHeight: 1.5 }} data-testid="leave-balance-pool-note">
          <strong>{typeLabel}</strong> {T.leaveCardSharesBalance || "shares one balance with"}{" "}
          {pool.others.length ? pool.others.join(", ") : (T.otherLeaveTypes || "other leave types")}.{" "}
          {(T.leaveCardPoolSized || "The shared balance is the employee's {source} entitlement for this leave year.")
            .replace("{source}", pool.source || "Annual Leave")}
        </div>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        <Stat C={C} label={pool ? (T.sharedEntitlement || "Shared Entitlement") : (T.annualEntitlement || "Annual Entitlement")}
          value={fmt(total)} unit={unit} />
        {showAccrued && <Stat C={C} label={T.accrued || "Accrued"} value={fmt(accrued)} unit={unit} />}
        <Stat C={C} label={T.leaveCardUsed || "Used"} value={fmt(decided)} unit={unit} />
        <Stat C={C} label={T.pending || "Pending"} value={fmt(pending)} unit={unit} color={C.red || "#DC2626"} />
        <Stat C={C} label={T.availableBalance || "Available Balance"} value={fmt(available)} unit={unit} color={C.green || "#047857"} highlight />
      </div>
      <div style={{ display: "flex", height: 10, borderRadius: 999, overflow: "hidden", background: C.borderLight || C.border, marginTop: 16 }}
        role="img" aria-label={`${fmt(decided)} used, ${fmt(pending)} pending, ${fmt(available)} available of ${fmt(total)}`}>
        <div style={{ width: `${usedPct}%`, background: "#3B82F6" }} />
        <div style={{ width: `${pendPct}%`, background: "#FDBA74" }} />
        <div style={{ width: `${availPct}%`, background: "#86EFAC" }} />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13.5, color: C.text, marginTop: 8, gap: 8, flexWrap: "wrap" }}>
        <span>{fmt(decided)} {T.daysUsed || "days used"} ({Math.round(usedPct)}%)</span>
        <span>{fmt(available)} {T.daysAvailable || "days available"} ({Math.round(availPct)}%)</span>
      </div>
      {showPoolBreakdown && pool && pool.rows.length > 0 && (
        <div style={note} data-testid="leave-balance-pool-breakdown">
          {(T.leaveCardTakenFromShared || "Taken from the shared balance this year")}:{" "}
          {pool.rows.map((r, i) => (
            <span key={r.type}>
              {i > 0 && " · "}
              {r.type} <strong>{fmt(r.used)}</strong>
              {r.pending > 0 && ` (${fmt(r.pending)} ${T.pendingLower || "pending"})`}
            </span>
          ))}
        </div>
      )}
      {cappedByType && (
        <div style={note}>
          {(T.leaveCardCappedByType || "{type} has its own limit of {cap} days a year, so only {available} days of the shared balance can be used for it.")
            .replace("{type}", typeLabel).replace("{cap}", fmt(num(balance.total))).replace("{available}", fmt(available))}
        </div>
      )}
      {thisRequestPending && (
        <div style={note}>
          {(T.leaveCardIncludesThisRequest || "Pending includes this request ({days} days). Available is what remains if it is approved.")
            .replace("{days}", fmt(num(request.days)))}
        </div>
      )}
    </div>
  );
}
