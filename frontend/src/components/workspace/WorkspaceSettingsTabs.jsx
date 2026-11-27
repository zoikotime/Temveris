import React, { useEffect, useMemo, useRef, useState } from "react";

// Delays a value update by `delay` ms after the last change.
// Applied to free-text search inputs so API requests only fire when the
// user pauses typing, not on every keystroke.
function useDebounce(value, delay = 350) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return debounced;
}
import { useNavigate } from "react-router-dom";
import { API_BASE } from "../../config";
import { getBreakThresholds, invalidateBreakThresholds } from "../../api/session";
import { useTheme } from "../../context/ThemeContext";
import { wsGet, wsSend } from "./_api";
import AccessGovernanceCenter from "./AccessGovernanceCenter";
import { SecurityCenter as _SecurityCenter } from "./SecurityCenter";
// The Workspace Settings → Billing & Subscription route now renders the
// full BillingModule (the same 5-tab Figma-aligned page that was previously
// only reachable from AdminDashboard?tab=billing). Importing here so the
// sub-tab IS the module, not a preview that links out.
import BillingModule from "../BillingModule";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

const fmtDateTime = (value) => {
  if (!value) return "-";
  try { return new Date(value).toLocaleString(); }
  catch { return String(value); }
};

const primaryBtn = (C) => ({
  padding: "10px 18px",
  borderRadius: 8,
  border: "none",
  background: C.brand,
  color: "#fff",
  fontWeight: 700,
  fontSize: 13,
  cursor: "pointer",
  fontFamily: "inherit",
});

const ghostBtn = (C) => ({
  padding: "9px 14px",
  borderRadius: 8,
  border: `1px solid ${C.border}`,
  background: C.surface,
  color: C.text,
  fontWeight: 700,
  fontSize: 13,
  cursor: "pointer",
  fontFamily: "inherit",
});

const card = (C) => ({
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 10,
  padding: 18,
  boxSizing: "border-box",
});

function Header({ title, subtitle, cta, C }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 22 }}>
      <div>
        <h2 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0 }}>{title}</h2>
        {subtitle && <p style={{ fontSize: 14, color: C.textSub, margin: "6px 0 0", lineHeight: 1.5 }}>{subtitle}</p>}
      </div>
      {cta && <div style={{ flexShrink: 0 }}>{cta}</div>}
    </div>
  );
}

function Pill({ children, C, tone = "neutral" }) {
  const palette = {
    configured: { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
    active: { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
    trial: { bg: "#FFFBEB", fg: "#B45309", bd: "#FDE68A" },
    incomplete: { bg: "#FFFBEB", fg: "#B45309", bd: "#FDE68A" },
    notset: { bg: C.bg, fg: C.textSub, bd: C.border },
    neutral: { bg: C.bg, fg: C.textSub, bd: C.border },
  }[tone] || { bg: C.bg, fg: C.textSub, bd: C.border };
  return (
    <span style={{ padding: "3px 8px", borderRadius: 999, border: `1px solid ${palette.bd}`, background: palette.bg, color: palette.fg, fontSize: 11, fontWeight: 700 }}>
      {children}
    </span>
  );
}

function Switch({ on, disabled, onChange, C }) {
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      style={{
        width: 44, height: 24, borderRadius: 999, border: "none",
        background: on ? C.brand : C.textLight, position: "relative",
        cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.65 : 1,
      }}
    >
      <span style={{
        position: "absolute", top: 2, left: on ? 22 : 2, width: 20, height: 20,
        borderRadius: "50%", background: "#fff", transition: "left .16s ease",
      }} />
    </button>
  );
}

// Roles & Permissions is the Access Governance Center. The whole module
// lives in AccessGovernanceCenter.jsx so this file stays focused on the
// other Workspace Settings tabs. Re-exported so existing imports work.
export function RolesPermissionsTab(props) {
  return <AccessGovernanceCenter {...props} />;
}

// Standard leave types seeded by Temveris — editable but protected from deletion.
const STANDARD_TYPES = new Set([
  "Annual Leave", "Casual Leave", "Sick Leave", "Maternity Leave",
  "Paternity Leave", "Compensatory Leave", "Adoption Leave",
  "Bereavement Leave", "Child Care Leave", "Parent Care Leave",
  "Medical Escort Leave", "Emergency Leave", "Company Leave",
  "Unpaid Leave", "Half Day Leave",
]);

const BLANK_DRAFT = {
  leave_type: "", annual_limit: 12, monthly_accrual: 1,
  max_carry_forward: 0, notice_days: 0,
  doc_required: false, shared_pool: false,
  hard_notice: false, evidence_gate: false,
};

export function PoliciesTab({ isAdmin, showToast }) {
  const { C } = useTheme();
  const [data, setData] = useState(null);
  const [leavePolicies, setLeavePolicies] = useState([]);
  const [loading, setLoading] = useState(true);

  // Break overtime thresholds
  const [breakThresh, setBreakThresh] = useState({ short_minutes: 15, lunch_minutes: 60 });
  const [breakSaving, setBreakSaving] = useState(false);
  // Paid-leave waiting period
  const [waitingPeriod,       setWaitingPeriod]       = useState(6);
  const [waitingPeriodSaving, setWaitingPeriodSaving] = useState(false);
  // Shared leave pool toggle
  const [sharedPoolEnabled, setSharedPoolEnabled] = useState(false);
  const [sharedPoolSaving,  setSharedPoolSaving]  = useState(false);

  // Inline row editing — one row at a time, all 8 fields
  const [editingRow, setEditingRow] = useState(null); // leave_type string | null
  const [editDraft,  setEditDraft]  = useState({});
  const [rowSaving,  setRowSaving]  = useState(false);

  // Add Custom Leave Type modal
  const [showCustomModal, setShowCustomModal] = useState(false);
  const [customDraft, setCustomDraft] = useState(BLANK_DRAFT);
  const [customSaving, setCustomSaving] = useState(false);

  // Delete confirmation
  const [deletingType, setDeletingType] = useState(null); // leave_type string
  const [deleteConfirm, setDeleteConfirm] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [summary, res, thData, wpRes, spRes] = await Promise.all([
        wsGet("/api/workspace/policies"),
        fetch(`${API_BASE}/leave/policies`,                { headers: { Authorization: `Bearer ${token()}` } }),
        getBreakThresholds({ force: true }),
        fetch(`${API_BASE}/leave/settings/waiting-period`, { headers: { Authorization: `Bearer ${token()}` } }),
        fetch(`${API_BASE}/leave/shared-pool`,             { headers: { Authorization: `Bearer ${token()}` } }),
      ]);
      setData(summary);
      const policies = await res.json();
      setLeavePolicies(Array.isArray(policies) ? policies : []);
      if (thData && (thData.short_minutes || thData.lunch_minutes)) {
        setBreakThresh({
          short_minutes: Number(thData.short_minutes) || 15,
          lunch_minutes: Number(thData.lunch_minutes) || 60,
        });
      }
      if (wpRes.ok) {
        const wp = await wpRes.json();
        if (typeof wp?.months === "number") setWaitingPeriod(wp.months);
      }
      if (spRes.ok) {
        const sp = await spRes.json();
        setSharedPoolEnabled(!!sp?.enabled);
      }
    } catch {
      showToast("Failed to load policies", "error");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const saveBreakThresholds = async () => {
    setBreakSaving(true);
    try {
      const res = await fetch(`${API_BASE}/break/thresholds`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          short_minutes: Number(breakThresh.short_minutes) || 15,
          lunch_minutes: Number(breakThresh.lunch_minutes) || 60,
        }),
      });
      if (!res.ok) throw new Error("Failed");
      const d = await res.json();
      setBreakThresh({
        short_minutes: Number(d.short_minutes) || 15,
        lunch_minutes: Number(d.lunch_minutes) || 60,
      });
      invalidateBreakThresholds(); // worker dashboards re-read the new limits next time
      showToast("Break thresholds saved");
    } catch {
      showToast("Failed to save break thresholds", "error");
    } finally { setBreakSaving(false); }
  };

  const saveWaitingPeriod = async () => {
    setWaitingPeriodSaving(true);
    try {
      // Clamp client-side too so a 60-year typo never leaves the SPA.
      // Server also clamps but the user-facing toast is friendlier this way.
      const months = Math.max(0, Math.min(36, Number(waitingPeriod) || 0));
      const res = await fetch(`${API_BASE}/leave/settings/waiting-period`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify({ months }),
      });
      if (!res.ok) throw new Error("Failed");
      const d = await res.json();
      setWaitingPeriod(Number(d.months) || 6);
      showToast(`Paid-leave eligibility set to ${d.months} month${d.months === 1 ? "" : "s"}`);
    } catch {
      showToast("Failed to save eligibility waiting period", "error");
    } finally { setWaitingPeriodSaving(false); }
  };

  const saveSharedPool = async (val) => {
    setSharedPoolSaving(true);
    try {
      const res = await fetch(`${API_BASE}/leave/shared-pool`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: val }),
      });
      if (!res.ok) throw new Error("Failed");
      const d = await res.json();
      setSharedPoolEnabled(!!d?.enabled);
      showToast(val ? "Shared leave pool enabled" : "Shared leave pool disabled");
    } catch {
      showToast("Failed to update shared leave pool", "error");
    } finally { setSharedPoolSaving(false); }
  };

  const hasNoAnnualCap = (p) => !!(p?.no_annual_limit || (p?.is_earned && Number(p?.annual_limit ?? 0) === 0));

  // ── Inline row edit handlers (all 8 fields) ──────────────────────────────
  const startEdit = (p) => {
    const noAnnualCap = hasNoAnnualCap(p);
    setEditingRow(p.leave_type);
    setEditDraft({
      annual_limit:      noAnnualCap ? 0 : (p.annual_limit ?? 0),
      monthly_accrual:   noAnnualCap ? 0 : (p.monthly_accrual ?? 1),
      max_carry_forward: p.max_carry_forward  ?? 0,
      notice_days:       p.notice_days        ?? 0,
      doc_required:      noAnnualCap ? false : (p.doc_required ?? false),
      shared_pool:       noAnnualCap ? false : (p.shared_pool ?? false),
      hard_notice:       p.hard_notice        ?? false,
      evidence_gate:     noAnnualCap ? false : (p.evidence_gate ?? false),
    });
  };
  const cancelEdit = () => { setEditingRow(null); setEditDraft({}); };

  const saveRow = async (leaveType) => {
    const editingPolicy = leavePolicies.find((p) => p.leave_type === leaveType);
    const noAnnualCap = hasNoAnnualCap(editingPolicy);
    const annual  = Number(editDraft.annual_limit);
    const accrual = Number(editDraft.monthly_accrual);
    const carry   = Number(editDraft.max_carry_forward);
    const notice  = Number(editDraft.notice_days);
    if (annual < 0 || accrual < 0 || carry < 0 || notice < 0) {
      return showToast("Values must be 0 or greater", "error");
    }
    setRowSaving(true);
    try {
      const next = leavePolicies.map((p) =>
        p.leave_type === leaveType
          ? {
              ...p,
              annual_limit:      noAnnualCap ? 0 : annual,
              monthly_accrual:   noAnnualCap ? 0 : accrual,
              max_carry_forward: carry,
              notice_days:       notice,
              doc_required:      noAnnualCap ? false : !!editDraft.doc_required,
              shared_pool:       noAnnualCap ? false : !!editDraft.shared_pool,
              hard_notice:       !!editDraft.hard_notice,
              evidence_gate:     noAnnualCap ? false : !!editDraft.evidence_gate,
            }
          : p
      );
      const res = await fetch(`${API_BASE}/leave/policies`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error("Failed");
      showToast(`${leaveType} updated`);
      cancelEdit();
      load();
    } catch {
      showToast("Failed to save policy", "error");
    } finally { setRowSaving(false); }
  };

  // ── Add custom leave type ─────────────────────────────────────────────────
  const openCustomModal = () => { setCustomDraft(BLANK_DRAFT); setShowCustomModal(true); };
  const closeCustomModal = () => { setShowCustomModal(false); setCustomDraft(BLANK_DRAFT); };

  const saveCustomType = async () => {
    const name = customDraft.leave_type.trim();
    if (!name) return showToast("Leave type name is required", "error");
    if (STANDARD_TYPES.has(name)) {
      return showToast(`"${name}" is a standard leave type and cannot be duplicated`, "error");
    }
    if (leavePolicies.some(p => p.leave_type.toLowerCase() === name.toLowerCase())) {
      return showToast(`"${name}" already exists in your leave policies`, "error");
    }
    setCustomSaving(true);
    try {
      const newItem = {
        leave_type:      name,
        annual_limit:    Number(customDraft.annual_limit)      || 0,
        monthly_accrual: Number(customDraft.monthly_accrual)   || 0,
        max_carry_forward: Number(customDraft.max_carry_forward) || 0,
        notice_days:     Number(customDraft.notice_days)       || 0,
        doc_required:    !!customDraft.doc_required,
        shared_pool:     !!customDraft.shared_pool,
        hard_notice:     false,
        evidence_gate:   false,
      };
      const next = [...leavePolicies, newItem];
      const res = await fetch(`${API_BASE}/leave/policies`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error("Failed");
      showToast(`"${name}" leave type added`);
      closeCustomModal();
      load();
    } catch {
      showToast("Failed to add custom leave type", "error");
    } finally { setCustomSaving(false); }
  };

  // ── Delete custom leave type ──────────────────────────────────────────────
  const confirmDelete = (leaveType) => { setDeletingType(leaveType); setDeleteConfirm(false); };
  const cancelDelete  = () => { setDeletingType(null); setDeleteConfirm(false); };

  const deleteCustomType = async () => {
    if (!deleteConfirm) return showToast("Please tick the confirmation box first", "error");
    const next = leavePolicies.filter(p => p.leave_type !== deletingType);
    try {
      const res = await fetch(`${API_BASE}/leave/policies`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error("Failed");
      showToast(`"${deletingType}" removed`);
      cancelDelete();
      load();
    } catch {
      showToast("Failed to remove leave type", "error");
    }
  };

  return (
    <div style={{ padding: "32px 40px", maxWidth: 1100 }}>
      <Header C={C} title="Policies" subtitle="Configure workforce rules, monitoring posture, schedules, breaks, overtime, and leave policy versions." />
      {loading ? <div style={{ color: C.textSub }}>Loading policies...</div> : (
        <div style={{ display: "grid", gap: 14 }}>
          {(data?.groups || []).map((group) => (
            <div key={group.key} style={card(C)}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                <div>
                  <h3 style={{ margin: 0, color: C.text, fontSize: 16 }}>{group.title}</h3>
                  <p style={{ margin: "5px 0 0", color: C.textSub, fontSize: 13 }}>{group.description}</p>
                </div>
                <Pill C={C}>{group.policy_count ?? (group.fields?.length || 0)} item(s)</Pill>
              </div>
              {group.fields && (
                <div style={{ marginTop: 14, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: 10 }}>
                  {group.fields.map((f) => (
                    <div key={f.key} style={{ border: `1px solid ${C.border}`, borderRadius: 8, padding: 12 }}>
                      <div style={{ fontSize: 12, color: C.textSub, fontWeight: 700 }}>{f.key}</div>
                      <div style={{ color: C.text, fontWeight: 800, marginTop: 4 }}>{String(f.value)}</div>
                      <div style={{ color: C.textSub, fontSize: 12, marginTop: 4 }}>{f.description}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          {/* Break overtime thresholds (drives the worker pop-up at >N min) */}
          <div style={card(C)}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", marginBottom: 12 }}>
              <div>
                <h3 style={{ margin: 0, color: C.text, fontSize: 16 }}>Break & Lunch Overtime</h3>
                <p style={{ margin: "5px 0 0", color: C.textSub, fontSize: 13 }}>
                  When a worker exceeds these limits, a pop-up + browser notification fires on their dashboard and the admin sees a real-time alert.
                </p>
              </div>
              <Pill C={C}>Live</Pill>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 12, alignItems: "end" }}>
              <label style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
                Short break limit (minutes)
                <input type="number" min={1} max={240} value={breakThresh.short_minutes}
                  onChange={(e) => setBreakThresh((s) => ({ ...s, short_minutes: e.target.value }))}
                  disabled={!isAdmin}
                  style={{ display: "block", width: "100%", marginTop: 6, padding: "9px 12px", border: `1px solid ${C.border}`, borderRadius: 8, background: C.surface, color: C.text }} />
              </label>
              <label style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
                Lunch break limit (minutes)
                <input type="number" min={1} max={480} value={breakThresh.lunch_minutes}
                  onChange={(e) => setBreakThresh((s) => ({ ...s, lunch_minutes: e.target.value }))}
                  disabled={!isAdmin}
                  style={{ display: "block", width: "100%", marginTop: 6, padding: "9px 12px", border: `1px solid ${C.border}`, borderRadius: 8, background: C.surface, color: C.text }} />
              </label>
              <button onClick={saveBreakThresholds} disabled={!isAdmin || breakSaving} style={{ ...primaryBtn(C), opacity: (!isAdmin || breakSaving) ? 0.5 : 1 }}>
                {breakSaving ? "Saving..." : "Save Thresholds"}
              </button>
            </div>
          </div>

          {/* Paid Leave Eligibility — tenant waiting period (default 6 months).
              Stored in AppSettings under `leave_waiting_period_months`. The
              server hard-blocks /leave/apply with 403 if a worker tries to
              apply before `start_date + N months`. */}
          <div style={card(C)}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", marginBottom: 12 }}>
              <div>
                <h3 style={{ margin: 0, color: C.text, fontSize: 16 }}>Paid Leave Eligibility</h3>
                <p style={{ margin: "5px 0 0", color: C.textSub, fontSize: 13 }}>
                  Months a new worker must complete from their Official Start Date
                  before they can apply for paid leave. Applies to Sick, Annual,
                  Casual and all other leave types. Default: 6 months.
                </p>
              </div>
              <Pill C={C}>Tenant policy</Pill>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 12, alignItems: "end" }}>
              <label style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
                Waiting period (months)
                <input type="number" min={0} max={36} step={1} value={waitingPeriod}
                  onChange={(e) => setWaitingPeriod(e.target.value)}
                  disabled={!isAdmin}
                  style={{ display: "block", width: "100%", marginTop: 6, padding: "9px 12px", border: `1px solid ${C.border}`, borderRadius: 8, background: C.surface, color: C.text }} />
              </label>
              <button onClick={saveWaitingPeriod} disabled={!isAdmin || waitingPeriodSaving}
                style={{ ...primaryBtn(C), opacity: (!isAdmin || waitingPeriodSaving) ? 0.5 : 1 }}>
                {waitingPeriodSaving ? "Saving..." : "Save Waiting Period"}
              </button>
            </div>
            <p style={{ margin: "10px 0 0", fontSize: 11, color: C.textSub }}>
              Workers with no Official Start Date set are grandfathered as
              eligible so existing employees aren't locked out when this policy
              ships. Backfill start dates via Workers &amp; Users → Edit Worker.
            </p>
          </div>

          {/* Shared leave pool toggle */}
          <div style={card(C)}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
              <div style={{ flex: 1 }}>
                <h3 style={{ margin: 0, color: C.text, fontSize: 16 }}>Shared Leave Pool</h3>
                <p style={{ margin: "5px 0 0", color: C.textSub, fontSize: 13, lineHeight: 1.5 }}>
                  When enabled, Casual, Sick, Child Care and other eligible leave types draw from
                  a shared annual allowance. Recommended for India-based leave policy configuration.
                </p>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: sharedPoolEnabled ? C.brand : C.textSub }}>
                  {sharedPoolEnabled ? "Enabled" : "Disabled"}
                </span>
                {isAdmin && (
                  <Switch
                    on={sharedPoolEnabled}
                    disabled={sharedPoolSaving}
                    onChange={saveSharedPool}
                    C={C}
                  />
                )}
                {sharedPoolSaving && (
                  <span style={{ fontSize: 11, color: C.textSub }}>Saving…</span>
                )}
              </div>
            </div>
          </div>

          {/* Leave Policy Table — inline editing with all 8 fields */}
          <div style={card(C)}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <div>
                <h3 style={{ margin: 0, color: C.text, fontSize: 16 }}>Leave Policies</h3>
                <p style={{ margin: "4px 0 0", fontSize: 12.5, color: C.textSub }}>
                  Standard types are protected from removal. Click <b>Edit</b> on any row to adjust all settings.
                </p>
              </div>
              {isAdmin && (
                <button
                  onClick={openCustomModal}
                  disabled={editingRow !== null}
                  style={{
                    ...ghostBtn(C),
                    display: "inline-flex", alignItems: "center", gap: 6,
                    borderColor: C.brand, color: C.brand, fontWeight: 700,
                    opacity: editingRow !== null ? 0.4 : 1,
                  }}>
                  + Add Custom Leave Type
                </button>
              )}
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {[
                      "Leave Type", "Annual\nLimit", "Monthly\nAccrual", "Carry\nFwd",
                      "Notice\nDays", "Doc\nReq.", "Shared\nPool", "Enforce\nNotice", "Evidence\nReview",
                      isAdmin ? "Actions" : "",
                    ].map((h) => (
                      <th key={h} style={{
                        textAlign: "left", padding: "8px 8px", whiteSpace: "pre",
                        color: C.textSub, fontWeight: 700, fontSize: 11,
                        textTransform: "uppercase", letterSpacing: .3,
                        borderBottom: `1px solid ${C.border}`,
                      }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {leavePolicies.map((p) => {
                    const isEditing   = editingRow === p.leave_type;
                    const isSaving    = isEditing && rowSaving;
                    const isStandard  = STANDARD_TYPES.has(p.leave_type);
                    const otherEditing = editingRow !== null && !isEditing;
                    const noAnnualCap = hasNoAnnualCap(p);

                    const numInp = (key, min, max, step = 1) => (
                      <input
                        type="number" min={min} max={max} step={step}
                        value={editDraft[key]}
                        onChange={e => setEditDraft(d => ({ ...d, [key]: e.target.value }))}
                        style={{
                          width: 64, padding: "4px 6px", borderRadius: 6,
                          border: `1px solid ${C.brand}44`, background: C.bg,
                          color: C.text, fontSize: 13, fontFamily: "inherit",
                        }} />
                    );

                    const chkInp = (key, label) => (
                      <label style={{ display: "flex", alignItems: "center", gap: 5, cursor: "pointer", fontSize: 11.5, color: C.text }}>
                        <input
                          type="checkbox"
                          checked={!!editDraft[key]}
                          onChange={e => setEditDraft(d => ({ ...d, [key]: e.target.checked }))}
                          style={{ accentColor: C.brand, width: 14, height: 14 }} />
                        {label}
                      </label>
                    );

                    const boolBadge = (val) => (
                      <span style={{ fontSize: 12, fontWeight: 600, color: val ? C.brand : C.textLight }}>
                        {val ? "Yes" : "No"}
                      </span>
                    );

                    return (
                      <tr key={p.leave_type} style={{
                        borderBottom: `1px solid ${C.border}`,
                        background: isEditing ? (C.bg) : "transparent",
                      }}
                        onMouseEnter={e => { if (!isEditing) e.currentTarget.style.background = C.bg; }}
                        onMouseLeave={e => { if (!isEditing) e.currentTarget.style.background = "transparent"; }}>

                        {/* Leave type + badge */}
                        <td style={{ padding: "10px 8px", fontWeight: 700, color: C.text, whiteSpace: "nowrap" }}>
                          {p.leave_type}
                          {isStandard && (
                            <span style={{ marginLeft: 6, fontSize: 10, fontWeight: 600, color: C.textLight, background: C.bg, border: `1px solid ${C.border}`, borderRadius: 4, padding: "1px 5px" }}>
                              Standard
                            </span>
                          )}
                        </td>

                        {/* Annual Limit */}
                        <td style={{ padding: "10px 8px", color: C.text }}>
                          {noAnnualCap ? (
                            <span style={{ color: C.brand, fontWeight: 700 }}>No annual cap</span>
                          ) : isEditing ? numInp("annual_limit", 0, 365) : `${p.annual_limit} days`}
                        </td>

                        {/* Monthly Accrual */}
                        <td style={{ padding: "10px 8px", color: C.text }}>
                          {noAnnualCap ? "-" : isEditing ? numInp("monthly_accrual", 0, 31, 0.25) : p.monthly_accrual}
                        </td>

                        {/* Carry Forward */}
                        <td style={{ padding: "10px 8px", color: C.text }}>
                          {isEditing ? numInp("max_carry_forward", 0, 365) : `${p.max_carry_forward ?? 0} days`}
                        </td>

                        {/* Notice Days */}
                        <td style={{ padding: "10px 8px", color: C.text }}>
                          {isEditing ? numInp("notice_days", 0, 90) : `${p.notice_days}d`}
                        </td>

                        {/* Doc Required */}
                        <td style={{ padding: "10px 8px" }}>
                          {noAnnualCap ? boolBadge(false) : isEditing ? chkInp("doc_required", "") : boolBadge(p.doc_required)}
                        </td>

                        {/* Shared Pool */}
                        <td style={{ padding: "10px 8px" }}>
                          {noAnnualCap ? boolBadge(false) : isEditing ? chkInp("shared_pool", "") : boolBadge(p.shared_pool)}
                        </td>

                        {/* Hard Notice (Enforce Notice Period) */}
                        <td style={{ padding: "10px 8px" }}>
                          {isEditing ? chkInp("hard_notice", "") : boolBadge(p.hard_notice)}
                        </td>

                        {/* Evidence Gate (Evidence Review) */}
                        <td style={{ padding: "10px 8px" }}>
                          {noAnnualCap ? boolBadge(false) : isEditing ? chkInp("evidence_gate", "") : boolBadge(p.evidence_gate)}
                        </td>

                        {/* Actions */}
                        {isAdmin && (
                          <td style={{ padding: "10px 8px", whiteSpace: "nowrap" }}>
                            {isEditing ? (
                              <div style={{ display: "flex", gap: 6 }}>
                                <button
                                  onClick={() => saveRow(p.leave_type)}
                                  disabled={isSaving}
                                  style={{ ...primaryBtn(C), padding: "5px 12px", fontSize: 12, opacity: isSaving ? 0.6 : 1 }}>
                                  {isSaving ? "Saving…" : "Save"}
                                </button>
                                <button
                                  onClick={cancelEdit}
                                  disabled={isSaving}
                                  style={{ ...ghostBtn(C), padding: "5px 12px", fontSize: 12 }}>
                                  Cancel
                                </button>
                              </div>
                            ) : (
                              <div style={{ display: "flex", gap: 6 }}>
                                <button
                                  onClick={() => startEdit(p)}
                                  disabled={otherEditing}
                                  style={{ ...ghostBtn(C), padding: "4px 10px", fontSize: 12, opacity: otherEditing ? 0.35 : 1 }}>
                                  Edit
                                </button>
                                {!isStandard && (
                                  <button
                                    onClick={() => confirmDelete(p.leave_type)}
                                    disabled={otherEditing}
                                    style={{ ...ghostBtn(C), padding: "4px 10px", fontSize: 12, color: "#DC2626", borderColor: "#FCA5A5", opacity: otherEditing ? 0.35 : 1 }}>
                                    Remove
                                  </button>
                                )}
                              </div>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {leavePolicies.length === 0 && (
                <p style={{ textAlign: "center", color: C.textSub, padding: "24px 0", margin: 0 }}>
                  No leave policies configured yet.
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── Add Custom Leave Type Modal ── */}
      {showCustomModal && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.38)", zIndex: 9000, display: "flex", alignItems: "center", justifyContent: "center" }}
          onClick={closeCustomModal}>
          <div style={{ ...card(C), width: 500, boxShadow: "0 20px 70px rgba(0,0,0,.25)" }} onClick={e => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 4px", color: C.text, fontSize: 16 }}>Add Custom Leave Type</h3>
            <p style={{ margin: "0 0 16px", fontSize: 12.5, color: C.textSub }}>
              Create an organization-specific leave type. Standard types (Annual, Sick, etc.) cannot be duplicated.
            </p>

            <label style={{ display: "block", fontSize: 12, fontWeight: 700, color: C.textSub, marginBottom: 12 }}>
              Leave type name *
              <input
                type="text"
                value={customDraft.leave_type}
                onChange={e => setCustomDraft(d => ({ ...d, leave_type: e.target.value }))}
                placeholder="e.g. Volunteer Leave"
                style={{ display: "block", width: "100%", marginTop: 5, padding: "9px 12px",
                         border: `1px solid ${C.border}`, borderRadius: 7, background: C.surface,
                         color: C.text, fontSize: 13, fontFamily: "inherit", boxSizing: "border-box" }} />
            </label>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
              {[
                ["Annual limit (days)", "annual_limit",      0, 365, 1],
                ["Monthly accrual",     "monthly_accrual",   0, 31, 0.25],
                ["Carry forward (days)","max_carry_forward", 0, 365, 1],
                ["Minimum notice (days)","notice_days",      0, 90, 1],
              ].map(([label, key, min, max, step]) => (
                <label key={key} style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
                  {label}
                  <input
                    type="number" min={min} max={max} step={step}
                    value={customDraft[key]}
                    onChange={e => setCustomDraft(d => ({ ...d, [key]: e.target.value }))}
                    style={{ display: "block", width: "100%", marginTop: 5, padding: "8px 10px",
                             border: `1px solid ${C.border}`, borderRadius: 7, background: C.surface,
                             color: C.text, fontSize: 13, fontFamily: "inherit", boxSizing: "border-box" }} />
                </label>
              ))}
            </div>

            <div style={{ display: "flex", gap: 16, marginBottom: 18 }}>
              {[
                ["Require documentation", "doc_required"],
                ["Draw from shared pool", "shared_pool"],
              ].map(([label, key]) => (
                <label key={key} style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13, color: C.text, cursor: "pointer" }}>
                  <input type="checkbox" checked={!!customDraft[key]}
                    onChange={e => setCustomDraft(d => ({ ...d, [key]: e.target.checked }))}
                    style={{ accentColor: C.brand, width: 14, height: 14 }} />
                  {label}
                </label>
              ))}
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button onClick={closeCustomModal} style={ghostBtn(C)} disabled={customSaving}>Cancel</button>
              <button onClick={saveCustomType} style={{ ...primaryBtn(C), opacity: customSaving ? 0.6 : 1 }} disabled={customSaving}>
                {customSaving ? "Adding…" : "Add Leave Type"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Delete Confirmation Modal ── */}
      {deletingType && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.38)", zIndex: 9000, display: "flex", alignItems: "center", justifyContent: "center" }}
          onClick={cancelDelete}>
          <div style={{ ...card(C), width: 440, boxShadow: "0 20px 70px rgba(0,0,0,.25)" }} onClick={e => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 8px", color: "#DC2626", fontSize: 16 }}>Remove Leave Type</h3>
            <p style={{ margin: "0 0 16px", fontSize: 13, color: C.textSub, lineHeight: 1.6 }}>
              You are about to permanently remove <b style={{ color: C.text }}>"{deletingType}"</b> from your organization's leave policies.
              Existing approved leave records are not affected.
            </p>
            <label style={{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 18, fontSize: 13, color: C.text, cursor: "pointer" }}>
              <input type="checkbox" checked={deleteConfirm} onChange={e => setDeleteConfirm(e.target.checked)}
                style={{ accentColor: "#DC2626", marginTop: 2, width: 14, height: 14 }} />
              I understand this action removes the leave type for all employees in this organization.
            </label>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button onClick={cancelDelete} style={ghostBtn(C)}>Cancel</button>
              <button onClick={deleteCustomType}
                style={{ ...primaryBtn(C), background: "#DC2626", opacity: deleteConfirm ? 1 : 0.45 }}>
                Remove
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Workspace Settings → Billing & Subscription.
//
// This route now renders the full Figma-aligned BillingModule directly —
// header, trial banner, 4 status cards, 5 tabs (Plan overview / Invoices &
// payments / Billing profile / Entitlements / Billing audit trail), each
// wired to the live /api/admin/billing/* + /api/v1/entitlements/current
// endpoints. The old condensed "preview + Manage Billing button" version
// is gone — admins land directly on the full module.
//
// `showToast` is still accepted for API compatibility with the parent
// SettingsPage component, but BillingModule has its own internal Toast
// system so we don't have to thread the prop down.
export function BillingSubscriptionTab(/* { showToast } */) {
  return (
    <div style={{ padding: "32px 40px", maxWidth: 1180 }}>
      <BillingModule />
    </div>
  );
}

// SecurityTab now renders the full 7-tab Security Center (see SecurityCenter.jsx)
// — the legacy minimal SecurityTab body lives below as _LegacySecurityTab in case
// anything else still imports the bare component.
export function SecurityTab(props) {
  return <_SecurityCenter {...props} />;
}

function _LegacySecurityTab({ isAdmin, showToast }) {
  const { C } = useTheme();
  const [data, setData] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [ipRules, setIpRules] = useState([]);
  const [ipEnabled, setIpEnabled] = useState(false);
  const [newRule, setNewRule] = useState({ cidr: "", label: "", action: "allow" });

  const load = async () => {
    try {
      const [security, sessionData] = await Promise.all([
        wsGet("/api/workspace/security"),
        wsGet("/api/workspace/security/sessions"),
      ]);
      setData(security);
      setSessions(sessionData.items || []);
      setIpRules(security.ip_rules?.rules || []);
      setIpEnabled(!!security.ip_rules?.enabled);
    } catch {
      showToast("Failed to load security settings", "error");
    }
  };
  useEffect(() => { load(); }, []);

  const toggle = async (path, enabled) => {
    try {
      await wsSend("PUT", path, { enabled });
      showToast("Security setting updated");
      load();
    } catch (e) {
      showToast(e.message || "Failed to update security setting", "error");
    }
  };
  const saveIpRules = async (rules = ipRules, enabled = ipEnabled) => {
    try {
      await wsSend("PUT", "/api/workspace/security/ip-rules", { enabled, rules });
      showToast("IP rules saved");
      load();
    } catch (e) {
      showToast(e.message || "Failed to save IP rules", "error");
    }
  };
  const revoke = async (id) => {
    try {
      await wsSend("POST", `/api/workspace/security/sessions/${id}/revoke`);
      showToast("Session revoked");
      load();
    } catch (e) {
      showToast(e.message || "Failed to revoke session", "error");
    }
  };

  return (
    <div style={{ padding: "32px 40px", maxWidth: 1050 }}>
      <Header C={C} title="Security" subtitle="Manage MFA, SSO, sessions, IP rules, and administrator security controls." cta={<button onClick={load} style={primaryBtn(C)}>Configure Security</button>} />
      {!data ? <div style={{ color: C.textSub }}>Loading security...</div> : (
        <div style={{ display: "grid", gap: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 14 }}>
            <div style={card(C)}>
              <h3 style={{ margin: "0 0 10px", color: C.text }}>Multi-factor Authentication</h3>
              <label style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, color: C.text }}>
                Require MFA
                <Switch C={C} on={!!data.mfa?.required} disabled={!isAdmin} onChange={(v) => toggle("/api/workspace/security/mfa", v)} />
              </label>
            </div>
            <div style={card(C)}>
              <h3 style={{ margin: "0 0 10px", color: C.text }}>Single Sign-On</h3>
              <label style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, color: C.text }}>
                Enable SSO
                <Switch C={C} on={!!data.sso?.enabled} disabled={!isAdmin} onChange={(v) => toggle("/api/workspace/security/sso", v)} />
              </label>
            </div>
            <div style={card(C)}>
              <h3 style={{ margin: "0 0 10px", color: C.text }}>Sessions</h3>
              <div style={{ fontSize: 28, fontWeight: 900, color: C.text }}>{data.sessions?.active || 0}</div>
              <div style={{ color: C.textSub, fontSize: 12 }}>Active workspace sessions</div>
            </div>
          </div>
          <div style={card(C)}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ margin: 0, color: C.text }}>IP Rules</h3>
              <Switch C={C} on={ipEnabled} disabled={!isAdmin} onChange={(v) => { setIpEnabled(v); saveIpRules(ipRules, v); }} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 110px auto", gap: 8, alignItems: "center" }}>
              <input value={newRule.cidr} onChange={(e) => setNewRule((r) => ({ ...r, cidr: e.target.value }))} placeholder="203.0.113.0/24" style={{ padding: "9px 12px", border: `1px solid ${C.border}`, borderRadius: 8 }} />
              <input value={newRule.label} onChange={(e) => setNewRule((r) => ({ ...r, label: e.target.value }))} placeholder="Office network" style={{ padding: "9px 12px", border: `1px solid ${C.border}`, borderRadius: 8 }} />
              <select value={newRule.action} onChange={(e) => setNewRule((r) => ({ ...r, action: e.target.value }))} style={{ padding: "9px 12px", border: `1px solid ${C.border}`, borderRadius: 8 }}><option value="allow">Allow</option><option value="deny">Deny</option></select>
              <button disabled={!isAdmin || !newRule.cidr.trim()} onClick={() => { const next = [...ipRules, newRule]; setIpRules(next); setNewRule({ cidr: "", label: "", action: "allow" }); saveIpRules(next); }} style={ghostBtn(C)}>Add</button>
            </div>
            {ipRules.map((r, idx) => (
              <div key={`${r.cidr}-${idx}`} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 90px auto", gap: 8, padding: "9px 0", borderTop: `1px solid ${C.border}`, marginTop: 10, color: C.text }}>
                <span>{r.cidr}</span><span>{r.label || "-"}</span><Pill C={C} tone={r.action === "allow" ? "configured" : "trial"}>{r.action}</Pill>
                <button disabled={!isAdmin} onClick={() => { const next = ipRules.filter((_, i) => i !== idx); setIpRules(next); saveIpRules(next); }} style={{ ...ghostBtn(C), padding: "5px 9px" }}>Remove</button>
              </div>
            ))}
          </div>
          <div style={card(C)}>
            <h3 style={{ margin: "0 0 12px", color: C.text }}>Active Sessions</h3>
            {sessions.length === 0 ? <div style={{ color: C.textSub }}>No active sessions.</div> : sessions.map((s) => (
              <div key={s.id} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr auto", gap: 10, alignItems: "center", padding: "9px 0", borderTop: `1px solid ${C.border}` }}>
                <span style={{ color: C.text, fontWeight: 700 }}>{s.user_email}</span>
                <span style={{ color: C.textSub }}>{s.device_name || s.os || "-"}</span>
                <span style={{ color: C.textSub }}>{fmtDateTime(s.last_active_at)}</span>
                <button disabled={!isAdmin} onClick={() => revoke(s.id)} style={{ ...ghostBtn(C), padding: "6px 10px" }}>Revoke</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// Per-category visual identity for Connected Systems: an accent colour and a
// compact line icon so each card is instantly recognisable instead of being a
// wall of look-alike boxes. Keyed by the integration `key` from the backend
// catalog (payroll/hris/accounting/pm/calendar/comms) with a safe fallback.
const INTEGRATION_META = {
  payroll:    { accent: "#10B981", icon: "M3 7h18M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7M3 7l2-3h14l2 3M9 13h6" },
  hris:       { accent: "#6366F1", icon: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" },
  accounting: { accent: "#8B5CF6", icon: "M9 7h6M9 11h6M9 15h4M6 3h12a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" },
  pm:         { accent: "#F59E0B", icon: "M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" },
  calendar:   { accent: "#EC4899", icon: "M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" },
  comms:      { accent: "#06B6D4", icon: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10Z" },
};
const integrationMeta = (key) => INTEGRATION_META[key] || { accent: "#64748B", icon: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20M2 12h20" };

function IntegrationIcon({ d, color }) {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

export function ConnectedSystemsTab({ isAdmin, showToast }) {
  const { C } = useTheme();
  const [hovered, setHovered] = useState(null);
  const [items, setItems] = useState([]);
  // `drafts` is the vendor-name input per integration key. We seed it on
  // load but never overwrite values the admin has already typed — that's
  // what caused inputs to flicker / lose characters while another card
  // was being saved.
  const [drafts, setDrafts] = useState({});
  const [busy, setBusy] = useState({});
  const [loading, setLoading] = useState(true);

  const seedDrafts = (list) => {
    setDrafts((prev) => {
      const next = { ...prev };
      list.forEach((i) => {
        if (next[i.key] === undefined) next[i.key] = i.vendor || "";
      });
      return next;
    });
  };

  const load = async () => {
    setLoading(true);
    try {
      const d = await wsGet("/api/workspace/integrations");
      const list = Array.isArray(d.integrations) ? d.integrations : [];
      setItems(list);
      seedDrafts(list);
    } catch {
      showToast("Failed to load connected systems", "error");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  // Merge PUT response into the local row so we don't have to re-fetch the
  // whole list (which was causing the input flicker / wipe).
  const applyUpdate = (key, patch) => {
    setItems((prev) => prev.map((i) => (i.key === key ? { ...i, ...patch } : i)));
  };

  const toggle = async (item) => {
    if (!isAdmin || busy[item.key]) return;
    const wantConnected = !item.connected;
    const vendor = (drafts[item.key] || "").trim();
    // Connecting requires an actual vendor name — never silently fall back
    // to the category label (the bug that put "Payroll" in the input).
    if (wantConnected && !vendor) {
      showToast("Enter a vendor name first", "error");
      return;
    }
    setBusy((b) => ({ ...b, [item.key]: true }));
    try {
      const res = await wsSend("PUT", `/api/workspace/integrations/${item.key}`, {
        connected: wantConnected,
        vendor: wantConnected ? vendor : (item.vendor || ""),
      });
      applyUpdate(item.key, res);
      showToast(wantConnected ? "Integration connected" : "Integration disconnected");
    } catch (e) {
      showToast(e.message || "Failed to update integration", "error");
    } finally {
      setBusy((b) => ({ ...b, [item.key]: false }));
    }
  };

  const connectedCount = items.filter((i) => i.connected).length;
  const total = items.length;

  return (
    <div style={{ padding: "32px 40px", maxWidth: 1100 }}>
      <Header
        C={C}
        title="Connected Systems"
        subtitle="Connect payroll, HRIS, accounting, project management, calendar, and communication systems."
        cta={
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {total > 0 && (
              <span style={{ fontSize: 13, fontWeight: 600, color: C.textSub }}>
                <span style={{ color: C.text, fontWeight: 800 }}>{connectedCount}</span> of {total} connected
              </span>
            )}
            <button onClick={load} disabled={loading} style={{ ...ghostBtn(C), display: "inline-flex", alignItems: "center", gap: 7, opacity: loading ? 0.6 : 1, cursor: loading ? "wait" : "pointer" }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={{ transformOrigin: "center", animation: loading ? "spin 0.8s linear infinite" : "none" }}>
                <path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16" />
              </svg>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        }
      />
      <style>{"@keyframes spin{to{transform:rotate(360deg)}}"}</style>

      {loading && items.length === 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div key={i} style={{ ...card(C), height: 196, opacity: 0.6 }} />
          ))}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
          {items.map((item) => {
            const draftValue = drafts[item.key] ?? "";
            const isBusy = !!busy[item.key];
            const canConnect = isAdmin && !isBusy && (item.connected || draftValue.trim().length > 0);
            const meta = integrationMeta(item.key);
            const isHover = hovered === item.key;
            return (
              <div
                key={item.key}
                onMouseEnter={() => setHovered(item.key)}
                onMouseLeave={() => setHovered(null)}
                style={{
                  ...card(C),
                  padding: 20,
                  display: "flex",
                  flexDirection: "column",
                  position: "relative",
                  overflow: "hidden",
                  borderColor: item.connected ? meta.accent + "66" : C.border,
                  boxShadow: isHover ? "0 8px 24px rgba(0,0,0,0.10)" : "none",
                  transform: isHover ? "translateY(-2px)" : "none",
                  transition: "transform .16s ease, box-shadow .16s ease, border-color .16s ease",
                }}
              >
                {/* accent strip down the left edge for connected systems */}
                <div style={{ position: "absolute", top: 0, left: 0, bottom: 0, width: 3, background: item.connected ? meta.accent : "transparent" }} />

                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10, marginBottom: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
                    <div style={{
                      width: 40, height: 40, borderRadius: 10, flexShrink: 0,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      background: meta.accent + "1A", color: meta.accent,
                    }}>
                      <IntegrationIcon d={meta.icon} color={meta.accent} />
                    </div>
                    <h3 style={{ margin: 0, color: C.text, fontSize: 15.5, fontWeight: 700 }}>{item.category}</h3>
                  </div>
                  <span style={{
                    display: "inline-flex", alignItems: "center", gap: 5,
                    padding: "3px 9px 3px 7px", borderRadius: 999,
                    border: `1px solid ${item.connected ? meta.accent + "66" : C.border}`,
                    background: item.connected ? meta.accent + "14" : C.bg,
                    color: item.connected ? meta.accent : C.textSub,
                    fontSize: 11, fontWeight: 700, whiteSpace: "nowrap", flexShrink: 0,
                  }}>
                    <span style={{ width: 6, height: 6, borderRadius: "50%", background: item.connected ? meta.accent : C.textLight }} />
                    {item.connected ? "Connected" : "Not connected"}
                  </span>
                </div>

                <p style={{ color: C.textSub, fontSize: 13, lineHeight: 1.5, margin: "0 0 6px" }}>{item.description}</p>
                <div style={{ color: C.textLight, fontSize: 11.5, marginBottom: 14 }}>{item.vendor_examples}</div>

                <div style={{ marginTop: "auto" }}>
                  {item.connected ? (
                    <div style={{
                      display: "flex", alignItems: "center", gap: 9,
                      padding: "9px 12px", borderRadius: 8, marginBottom: 12,
                      background: C.bg, border: `1px solid ${C.border}`,
                    }}>
                      <div style={{ width: 26, height: 26, borderRadius: 7, flexShrink: 0, background: meta.accent + "1A", color: meta.accent, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 12 }}>
                        {(item.vendor || "?").trim().charAt(0).toUpperCase()}
                      </div>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ color: C.text, fontSize: 13, fontWeight: 700, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.vendor || "—"}</div>
                        {item.connected_at && (
                          <div style={{ color: C.textSub, fontSize: 11 }}>
                            Connected {fmtDateTime(item.connected_at)}{item.connected_by ? ` · ${item.connected_by}` : ""}
                          </div>
                        )}
                      </div>
                    </div>
                  ) : (
                    <input
                      value={draftValue}
                      onChange={(e) => setDrafts((d) => ({ ...d, [item.key]: e.target.value }))}
                      placeholder="Vendor name"
                      disabled={!isAdmin || isBusy}
                      style={{
                        width: "100%",
                        padding: "9px 12px",
                        border: `1px solid ${C.border}`,
                        borderRadius: 8,
                        marginBottom: 12,
                        background: C.surface,
                        color: C.text,
                        fontFamily: "inherit",
                        fontSize: 13,
                        boxSizing: "border-box",
                      }}
                    />
                  )}
                  <button
                    disabled={!canConnect}
                    onClick={() => toggle(item)}
                    style={{
                      ...(item.connected ? ghostBtn(C) : primaryBtn(C)),
                      width: "100%",
                      ...(item.connected ? {} : { background: meta.accent }),
                      ...(item.connected && isHover ? { borderColor: "#FCA5A5", color: "#DC2626" } : {}),
                      opacity: canConnect ? 1 : 0.55,
                      cursor: canConnect ? "pointer" : "not-allowed",
                    }}
                  >
                    {isBusy ? "Saving…" : item.connected ? "Disconnect" : "Connect"}
                  </button>
                </div>
              </div>
            );
          })}
          {!loading && items.length === 0 && (
            <div style={{ ...card(C), color: C.textSub, fontSize: 13 }}>
              No integrations available.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
// Audit & Compliance — Governance Center
// ──────────────────────────────────────────────────────────────────────────
// Single-page audit governance surface modelled on the Figma design.
//
//   • Top hero with three secondary actions + primary "Create Evidence Package"
//   • Status banner (audit logging active / policy version / last reviewed)
//   • Four headline counters (Audit events / Policy versions / Evidence
//     packages / Legal holds) sourced from /api/workspace/audit
//   • Seven tabs: Audit Log, Policy History, Access Events, Exports,
//     Evidence Packages, Legal Hold, Retention
//   • Footer status strip mirrors the design summary line
//
// The live audit feed + counters come from the backend. The remaining
// tabs render governance content modelled after the design — surfaces
// not yet backed by an API are populated with representative records so
// the UI shape matches what admins will see once those services land.

const RISK_TONE = {
  Critical: { bg: "#1F0A0A", fg: "#FECACA", bd: "#7F1D1D" },
  High:     { bg: "#FEF3F2", fg: "#B42318", bd: "#FECDCA" },
  Medium:   { bg: "#FFFAEB", fg: "#B54708", bd: "#FEDF89" },
  Low:      { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  Info:     { bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" },
};

const STATUS_TONE = {
  Current:        { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  Approved:       { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  Available:      { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  Success:        { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  "Pending review": { bg: "#FFFAEB", fg: "#B54708", bd: "#FEDF89" },
  "Pending":        { bg: "#FFFAEB", fg: "#B54708", bd: "#FEDF89" },
  Superseded:     { bg: "#F1F5F9", fg: "#475569", bd: "#CBD5E1" },
  Expired:        { bg: "#F1F5F9", fg: "#475569", bd: "#CBD5E1" },
  Held:           { bg: "#F4F0FF", fg: "#6941C6", bd: "#D8C7FF" },
  "Active hold":  { bg: "#F4F0FF", fg: "#6941C6", bd: "#D8C7FF" },
  Locked:         { bg: "#FEF3F2", fg: "#B42318", bd: "#FECDCA" },
  Blocked:        { bg: "#FEF3F2", fg: "#B42318", bd: "#FECDCA" },
};

const PURPOSE_TONE = {
  "HR investigation":  { bg: "#FFFAEB", fg: "#B54708", bd: "#FEDF89" },
  "Payroll support":   { bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" },
  "Compliance audit":  { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  "Legal review":      { bg: "#FEF3F2", fg: "#B42318", bd: "#FECDCA" },
};

function Chip({ children, tone, C, solid }) {
  const t = tone || { bg: C.bg, fg: C.textSub, bd: C.border };
  return (
    <span style={{
      display: "inline-block",
      padding: "3px 10px",
      borderRadius: 999,
      border: `1px solid ${t.bd}`,
      background: solid ? t.bg : t.bg,
      color: t.fg,
      fontSize: 11,
      fontWeight: 700,
      letterSpacing: ".2px",
      whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

function Avatar({ name, color }) {
  const initials = (name || "?")
    .split(/\s+/)
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
  return (
    <span style={{
      width: 28, height: 28, borderRadius: "50%",
      background: color || "#0F766E",
      color: "#FFFFFF",
      display: "inline-flex", alignItems: "center", justifyContent: "center",
      fontSize: 11, fontWeight: 800, letterSpacing: ".3px",
      flexShrink: 0,
    }}>{initials}</span>
  );
}

function ActorCell({ name, role, color, C }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
      <Avatar name={name} color={color} />
      <span style={{ display: "inline-flex", flexDirection: "column" }}>
        <span style={{ color: C.text, fontWeight: 700, fontSize: 13 }}>{name}</span>
        {role && (
          <span style={{ marginTop: 2 }}>
            <Chip C={C} tone={{ bg: C.bg, fg: C.textSub, bd: C.border }}>{role}</Chip>
          </span>
        )}
      </span>
    </span>
  );
}

function StatCard({ C, label, value, hint, accent }) {
  return (
    <div style={{
      background: C.surface,
      border: `2px solid ${accent}`,
      borderRadius: 10,
      padding: "16px 18px",
      minHeight: 110,
      boxSizing: "border-box",
      display: "flex", flexDirection: "column", justifyContent: "center",
    }}>
      <div style={{ fontSize: 11, fontWeight: 800, letterSpacing: ".6px", color: C.textSub, textTransform: "uppercase" }}>{label}</div>
      <div style={{ fontSize: 30, fontWeight: 900, color: C.text, marginTop: 4 }}>{value}</div>
      {hint && <div style={{ marginTop: 6, fontSize: 12, color: C.textSub, lineHeight: 1.5 }}>{hint}</div>}
    </div>
  );
}

function TabsBar({ C, tabs, active, onChange }) {
  return (
    <div style={{
      display: "flex",
      gap: 4,
      borderBottom: `1px solid ${C.border}`,
      padding: "0 4px",
      overflowX: "auto",
    }}>
      {tabs.map((t) => {
        const isActive = active === t.key;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onChange(t.key)}
            style={{
              padding: "12px 16px",
              border: "none",
              background: "transparent",
              color: isActive ? C.text : C.textSub,
              fontWeight: isActive ? 800 : 600,
              fontSize: 13,
              cursor: "pointer",
              fontFamily: "inherit",
              borderBottom: isActive ? `2px solid ${C.brand}` : "2px solid transparent",
              marginBottom: -1,
              whiteSpace: "nowrap",
            }}
          >{t.label}</button>
        );
      })}
    </div>
  );
}

function FilterInput({ C, value, onChange, placeholder, width }) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        padding: "9px 12px",
        border: `1px solid ${C.border}`,
        borderRadius: 8,
        background: C.surface,
        color: C.text,
        fontSize: 13,
        width: width || "100%",
        fontFamily: "inherit",
      }}
    />
  );
}

function FilterSelect({ C, value, onChange, options }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        padding: "9px 12px",
        border: `1px solid ${C.border}`,
        borderRadius: 8,
        background: C.surface,
        color: C.text,
        fontSize: 13,
        fontFamily: "inherit",
      }}
    >
      {options.map((opt) => (
        <option key={opt.value} value={opt.value}>{opt.label}</option>
      ))}
    </select>
  );
}


// ── Live-wired governance content ─────────────────────────────────────────
// Every tab below pulls from /api/workspace/audit/* — overview counters,
// audit events, policy history, access events, exports, evidence packages,
// legal holds, and retention rules. All write actions hit POST/PUT endpoints
// and refresh the relevant tab on success.
const ACTOR_COLORS = ["#0F766E", "#9333EA", "#0EA5E9", "#F59E0B", "#EF4444", "#475569", "#10B981", "#6366F1"];
const colorForName = (name) => {
  if (!name) return ACTOR_COLORS[0];
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return ACTOR_COLORS[h % ACTOR_COLORS.length];
};

const POLICY_TYPE_TONE = {
  Security: { bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" },
  Leave:    { bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  Holiday:  { bg: "#FFFAEB", fg: "#B54708", bd: "#FEDF89" },
  Overtime: { bg: "#F4F0FF", fg: "#6941C6", bd: "#D8C7FF" },
};

const TABS = [
  { key: "audit",     label: "Audit Log" },
  { key: "policy",    label: "Policy History" },
  { key: "access",    label: "Access Events" },
  { key: "exports",   label: "Exports" },
  { key: "evidence",  label: "Evidence Packages" },
  { key: "hold",      label: "Legal Hold" },
  { key: "retention", label: "Retention" },
];

const fmtCount = (n) => Number(n || 0).toLocaleString("en-US");

const fmtRelativeDate = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  } catch { return String(iso); }
};

const fmtDate = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
  catch { return String(iso); }
};

// ── Modal primitives ──────────────────────────────────────────────────────
function Modal({ C, title, onClose, children, footer, width = 520 }) {
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,.42)",
      zIndex: 9100, display: "flex", alignItems: "center", justifyContent: "center",
      padding: 20,
    }} onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width, maxWidth: "100%", background: C.surface,
        border: `1px solid ${C.border}`, borderRadius: 12,
        boxShadow: "0 24px 80px rgba(0,0,0,.28)",
        display: "flex", flexDirection: "column", maxHeight: "90vh",
      }}>
        <div style={{ padding: "16px 20px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0, color: C.text, fontSize: 16, fontWeight: 800 }}>{title}</h3>
          <button type="button" onClick={onClose} style={{ background: "transparent", border: "none", color: C.textSub, fontSize: 22, cursor: "pointer", lineHeight: 1 }}>×</button>
        </div>
        <div style={{ padding: 20, overflowY: "auto" }}>{children}</div>
        {footer && <div style={{ padding: "14px 20px", borderTop: `1px solid ${C.border}`, display: "flex", justifyContent: "flex-end", gap: 10 }}>{footer}</div>}
      </div>
    </div>
  );
}

function Field({ C, label, children, hint }) {
  return (
    <label style={{ display: "block", marginBottom: 14, color: C.text, fontSize: 13, fontWeight: 700 }}>
      {label}
      <div style={{ marginTop: 6 }}>{children}</div>
      {hint && <div style={{ marginTop: 4, color: C.textSub, fontSize: 11, fontWeight: 500 }}>{hint}</div>}
    </label>
  );
}

const fieldInput = (C) => ({
  width: "100%", padding: "9px 12px",
  border: `1px solid ${C.border}`, borderRadius: 8,
  background: C.surface, color: C.text,
  fontSize: 13, fontFamily: "inherit", boxSizing: "border-box",
});

export function AuditComplianceTab({ showToast, isAdmin }) {
  const { C } = useTheme();
  const [tab, setTab]       = useState("audit");

  // Overview / counters
  const [overview, setOverview] = useState(null);

  // Audit Log state
  const [eventsLoading, setEventsLoading] = useState(true);
  const [events, setEvents]   = useState([]);
  const [actorList, setActorList] = useState([]);
  const [searchQ, setSearchQ] = useState("");
  const [eventType, setEventType] = useState("all");
  const [riskLevel, setRiskLevel] = useState("all");
  const [actorFilter, setActorFilter] = useState("all");
  const [timeRange, setTimeRange] = useState("30");

  // Policy History
  const [polLoading, setPolLoading] = useState(false);
  const [policies, setPolicies] = useState([]);
  const [polSearch, setPolSearch] = useState("");
  const [polType, setPolType]     = useState("all");
  const [polStatus, setPolStatus] = useState("all");

  // Access Events
  const [accLoading, setAccLoading] = useState(false);
  const [accessEvents, setAccessEvents] = useState([]);
  const [accSearch, setAccSearch] = useState("");
  const [accRisk, setAccRisk]     = useState("all");
  const [accDays, setAccDays]     = useState("7");

  // Exports
  const [expLoading, setExpLoading] = useState(false);
  const [exports, setExports]       = useState([]);
  const [expSearch, setExpSearch]   = useState("");
  const [expType, setExpType]       = useState("all");
  const [expStatus, setExpStatus]   = useState("all");

  // Evidence packages
  const [evLoading, setEvLoading]   = useState(false);
  const [evidence, setEvidence]     = useState([]);
  const [evSearch, setEvSearch]     = useState("");
  const [evPurpose, setEvPurpose]   = useState("all");
  const [evStatus, setEvStatus]     = useState("all");

  // Legal holds
  const [holdLoading, setHoldLoading] = useState(false);
  const [holds, setHolds]             = useState([]);
  const [holdSearch, setHoldSearch]   = useState("");
  const [holdStatus, setHoldStatus]   = useState("all");

  // Retention
  const [retention, setRetention] = useState([]);
  const [retLoading, setRetLoading] = useState(false);

  // Modal control
  const [modal, setModal] = useState(null);

  // ── Loaders ────────────────────────────────────────────────────────────
  const loadOverview = async () => {
    try { setOverview(await wsGet("/api/workspace/audit/overview")); }
    catch { /* counters are non-fatal */ }
  };

  const loadEvents = async () => {
    setEventsLoading(true);
    try {
      const qs = new URLSearchParams({ since_days: timeRange });
      if (searchQ.trim())               qs.set("q", searchQ.trim());
      if (eventType  !== "all")         qs.set("event_type", eventType);
      if (riskLevel  !== "all")         qs.set("risk", riskLevel);
      if (actorFilter!== "all")         qs.set("actor", actorFilter);
      const d = await wsGet(`/api/workspace/audit/events?${qs.toString()}`);
      setEvents(d.events || []);
      setActorList(d.actors || []);
    } catch {
      showToast?.("Failed to load audit events", "error");
    } finally { setEventsLoading(false); }
  };

  const loadPolicies = async () => {
    setPolLoading(true);
    try {
      const qs = new URLSearchParams();
      if (polSearch.trim()) qs.set("q", polSearch.trim());
      if (polType   !== "all") qs.set("policy_type", polType);
      if (polStatus !== "all") qs.set("status", polStatus);
      const d = await wsGet(`/api/workspace/audit/policy-history?${qs.toString()}`);
      setPolicies(d.items || []);
    } catch { showToast?.("Failed to load policy history", "error"); }
    finally { setPolLoading(false); }
  };

  const loadAccessEvents = async () => {
    setAccLoading(true);
    try {
      const qs = new URLSearchParams({ since_days: accDays });
      if (accSearch.trim()) qs.set("q", accSearch.trim());
      if (accRisk !== "all") qs.set("risk", accRisk);
      const d = await wsGet(`/api/workspace/audit/access-events?${qs.toString()}`);
      setAccessEvents(d.items || []);
    } catch { showToast?.("Failed to load access events", "error"); }
    finally { setAccLoading(false); }
  };

  const loadExports = async () => {
    setExpLoading(true);
    try {
      const qs = new URLSearchParams();
      if (expSearch.trim()) qs.set("q", expSearch.trim());
      if (expType   !== "all") qs.set("export_type", expType);
      if (expStatus !== "all") qs.set("status", expStatus);
      const d = await wsGet(`/api/workspace/audit/exports?${qs.toString()}`);
      setExports(d.items || []);
    } catch { showToast?.("Failed to load exports", "error"); }
    finally { setExpLoading(false); }
  };

  const loadEvidence = async () => {
    setEvLoading(true);
    try {
      const qs = new URLSearchParams();
      if (evSearch.trim()) qs.set("q", evSearch.trim());
      if (evPurpose !== "all") qs.set("purpose", evPurpose);
      if (evStatus  !== "all") qs.set("status", evStatus);
      const d = await wsGet(`/api/workspace/audit/evidence?${qs.toString()}`);
      setEvidence(d.items || []);
    } catch { showToast?.("Failed to load evidence packages", "error"); }
    finally { setEvLoading(false); }
  };

  const loadHolds = async () => {
    setHoldLoading(true);
    try {
      const qs = new URLSearchParams();
      if (holdSearch.trim()) qs.set("q", holdSearch.trim());
      if (holdStatus !== "all") qs.set("status", holdStatus);
      const d = await wsGet(`/api/workspace/audit/legal-holds?${qs.toString()}`);
      setHolds(d.items || []);
    } catch { showToast?.("Failed to load legal holds", "error"); }
    finally { setHoldLoading(false); }
  };

  const loadRetention = async () => {
    setRetLoading(true);
    try {
      const d = await wsGet("/api/workspace/audit/retention");
      setRetention(d.items || []);
    } catch { showToast?.("Failed to load retention rules", "error"); }
    finally { setRetLoading(false); }
  };

  // Debounced text search values — API requests only fire after the user
  // stops typing for 350 ms. Select/dropdown filters are intentionally NOT
  // debounced: they fire exactly once on selection.
  const dSearchQ    = useDebounce(searchQ,    350);
  const dPolSearch  = useDebounce(polSearch,  350);
  const dAccSearch  = useDebounce(accSearch,  350);
  const dExpSearch  = useDebounce(expSearch,  350);
  const dEvSearch   = useDebounce(evSearch,   350);
  const dHoldSearch = useDebounce(holdSearch, 350);

  // Initial + tab-driven loads
  useEffect(() => { loadOverview(); }, []);
  useEffect(() => {
    if (tab === "audit")      loadEvents();
    if (tab === "policy")     loadPolicies();
    if (tab === "access")     loadAccessEvents();
    if (tab === "exports")    loadExports();
    if (tab === "evidence")   loadEvidence();
    if (tab === "hold")       loadHolds();
    if (tab === "retention")  loadRetention();
    // eslint-disable-next-line
  }, [tab]);

  // Re-fetch when filters change — text searches use debounced values so
  // the API is only called when the user pauses, not on every keystroke.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "audit")    loadEvents();       }, [dSearchQ,    eventType, riskLevel, actorFilter, timeRange]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "policy")   loadPolicies();     }, [dPolSearch,  polType,   polStatus]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "access")   loadAccessEvents(); }, [dAccSearch,  accRisk,   accDays]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "exports")  loadExports();      }, [dExpSearch,  expType,   expStatus]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "evidence") loadEvidence();     }, [dEvSearch,   evPurpose, evStatus]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "hold")     loadHolds();        }, [dHoldSearch, holdStatus]);

  // ── Live export download (CSV) ─────────────────────────────────────────
  const downloadAuditCsv = async (reason) => {
    const url = `${API_BASE}/api/workspace/audit/export?reason=${encodeURIComponent(reason)}&since_days=${timeRange}`;
    try {
      const res = await fetch(url, { headers: { Authorization: `Bearer ${token()}` } });
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `audit-log-${new Date().toISOString().slice(0,10)}.csv`;
      document.body.appendChild(link); link.click(); link.remove();
      URL.revokeObjectURL(link.href);
      showToast?.("Audit report exported");
      loadOverview();
      if (tab === "exports") loadExports();
    } catch (e) {
      showToast?.(e.message || "Failed to export audit report", "error");
    }
  };

  // ── Mutations ─────────────────────────────────────────────────────────
  const submitExport = async (body) => {
    try {
      await wsSend("POST", "/api/workspace/audit/exports", body);
      showToast?.("Export recorded");
      setModal(null);
      loadExports();
      loadOverview();
    } catch (e) { showToast?.(e.message || "Failed to record export", "error"); }
  };

  const submitEvidence = async (body) => {
    try {
      const r = await wsSend("POST", "/api/workspace/audit/evidence", body);
      showToast?.(`Evidence package ${r.code} created`);
      setModal(null);
      setTab("evidence");
      loadEvidence();
      loadOverview();
    } catch (e) { showToast?.(e.message || "Failed to create evidence package", "error"); }
  };

  const submitLegalHold = async (body) => {
    try {
      await wsSend("POST", "/api/workspace/audit/legal-holds", body);
      showToast?.("Legal hold created");
      setModal(null);
      setTab("hold");
      loadHolds();
      loadOverview();
    } catch (e) { showToast?.(e.message || "Failed to create legal hold", "error"); }
  };

  const releaseHold = async (id) => {
    if (!window.confirm("Release this legal hold? Records will return to ordinary retention rules.")) return;
    try {
      await wsSend("POST", `/api/workspace/audit/legal-holds/${id}/release`);
      showToast?.("Legal hold released");
      loadHolds();
      loadOverview();
    } catch (e) { showToast?.(e.message || "Failed to release legal hold", "error"); }
  };

  const submitPolicyVersion = async (body) => {
    try {
      await wsSend("POST", "/api/workspace/audit/policy-history", body);
      showToast?.("Policy version recorded");
      setModal(null);
      loadPolicies();
      loadOverview();
    } catch (e) { showToast?.(e.message || "Failed to record policy version", "error"); }
  };

  const submitRetention = async (rule_key, body) => {
    try {
      await wsSend("PUT", `/api/workspace/audit/retention/${rule_key}`, body);
      showToast?.("Retention rule updated");
      setModal(null);
      loadRetention();
    } catch (e) { showToast?.(e.message || "Failed to update retention rule", "error"); }
  };

  // ── Derived values ────────────────────────────────────────────────────
  const counters       = overview?.counters || {};
  const auditEvents30d = counters.audit_events_30d ?? 0;
  const highRisk30d    = counters.high_risk_30d ?? 0;
  const policyVersions = counters.policy_versions ?? 0;
  const policyCurrent  = counters.policy_current  ?? 0;
  const evidenceMonth  = counters.evidence_packages_month   ?? 0;
  const evidencePending= counters.evidence_packages_pending ?? 0;
  const legalHoldsActive = counters.legal_holds_active ?? 0;
  const pctChange      = counters.audit_events_pct_change ?? 0;
  const policyVersion  = overview?.policy_version || "—";
  const lastReviewed   = overview?.last_reviewed ? fmtRelativeDate(overview.last_reviewed) : "—";

  // ── Layout ────────────────────────────────────────────────────────────
  return (
    <div style={{ padding: "32px 40px", maxWidth: 1280, paddingBottom: 96 }}>
      {/* Title + actions */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 14, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start", maxWidth: 640 }}>
          <div style={{
            width: 38, height: 38, borderRadius: 10,
            background: C.brandAccentBg, border: `1px solid ${C.greenBdr}`,
            display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
          }}>
            <span style={{ width: 14, height: 14, borderRadius: 4, background: C.brandAccent }} />
          </div>
          <div>
            <h2 style={{ fontSize: 24, fontWeight: 800, color: C.text, margin: 0 }}>Audit & Compliance</h2>
            <p style={{ fontSize: 13, color: C.textSub, margin: "6px 0 0", lineHeight: 1.55 }}>
              Review workspace activity, policy changes, access events, exports,
              evidence packages, retention rules, and legal holds from one
              governed audit center.
            </p>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => setModal({ kind: "export_audit" })} style={ghostBtn(C)} disabled={!isAdmin}>Export Audit Report</button>
          <button onClick={() => setTab("hold")} style={ghostBtn(C)}>Legal Hold</button>
          <button onClick={() => setTab("retention")} style={ghostBtn(C)}>Retention Settings</button>
          <button onClick={() => setModal({ kind: "create_evidence" })} style={{ ...primaryBtn(C), background: "#0F4C3A" }} disabled={!isAdmin}>Create Evidence Package</button>
        </div>
      </div>

      {/* Status banner */}
      <div style={{
        background: C.greenBg, border: `1px solid ${C.greenBdr}`, borderRadius: 10,
        padding: "12px 16px", display: "flex", justifyContent: "space-between",
        gap: 12, flexWrap: "wrap", marginBottom: 18,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, color: C.green, fontWeight: 700, fontSize: 13 }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: C.green }} />
          Audit logging active · Evidence controls enabled · All material workspace events are being recorded.
        </div>
        <div style={{ color: C.textSub, fontSize: 12, fontWeight: 600 }}>
          Policy version {policyVersion} · Last reviewed: {lastReviewed}
        </div>
      </div>

      {/* Stat cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14, marginBottom: 22 }}>
        <StatCard C={C} accent="#0F766E" label="Audit events (30d)"
          value={fmtCount(auditEvents30d)}
          hint={`${pctChange >= 0 ? "↑" : "↓"} ${Math.abs(pctChange)}% from prior period · ${highRisk30d} high-risk`} />
        <StatCard C={C} accent="#65A30D" label="Policy versions"
          value={fmtCount(policyVersions)}
          hint={`${policyCurrent} current + ${Math.max(policyVersions - policyCurrent, 0)} historical · all retained`} />
        <StatCard C={C} accent="#0EA5E9" label="Evidence packages"
          value={fmtCount(evidenceMonth)}
          hint={`Created this month · ${evidencePending} pending review`} />
        <StatCard C={C} accent="#9333EA" label="Legal holds"
          value={fmtCount(legalHoldsActive)}
          hint={legalHoldsActive ? "Active · overrides retention deletion" : "No active holds"} />
      </div>

      {/* Tabs */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10 }}>
        <TabsBar C={C} tabs={TABS} active={tab} onChange={setTab} />

        {/* AUDIT LOG */}
        {tab === "audit" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.5fr repeat(4, 1fr) auto", gap: 8, marginBottom: 14, alignItems: "center" }}>
              <FilterInput C={C} value={searchQ} onChange={setSearchQ} placeholder="Search event ID, actor, object..." />
              <FilterSelect C={C} value={eventType} onChange={setEventType} options={[
                { value: "all",       label: "All event types" },
                { value: "Role",      label: "Role changes" },
                { value: "Policy",    label: "Policy changes" },
                { value: "Billing",   label: "Billing changes" },
                { value: "Timesheet", label: "Timesheet changes" },
                { value: "User",      label: "Worker lifecycle" },
                { value: "Auth",      label: "Auth events" },
              ]} />
              <FilterSelect C={C} value={riskLevel} onChange={setRiskLevel} options={[
                { value: "all", label: "All risk levels" },
                { value: "Critical", label: "Critical" },
                { value: "High",     label: "High" },
                { value: "Medium",   label: "Medium" },
                { value: "Low",      label: "Low" },
                { value: "Info",     label: "Info" },
              ]} />
              <FilterSelect C={C} value={actorFilter} onChange={setActorFilter} options={[
                { value: "all", label: "All actors" },
                ...actorList.map((a) => ({ value: a, label: a })),
              ]} />
              <FilterSelect C={C} value={timeRange} onChange={setTimeRange} options={[
                { value: "7",   label: "Last 7 days" },
                { value: "30",  label: "Last 30 days" },
                { value: "90",  label: "Last 90 days" },
                { value: "365", label: "Last 12 months" },
              ]} />
              <button onClick={() => setModal({ kind: "export_audit" })} style={ghostBtn(C)} disabled={!isAdmin}>Export</button>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Date & time", "Event ID", "Actor", "Role", "Event type", "Object affected", "Risk"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {eventsLoading && (
                    <tr><td colSpan={7} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading audit events…</td></tr>
                  )}
                  {!eventsLoading && events.length === 0 && (
                    <tr><td colSpan={7} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No audit events match the current filters.</td></tr>
                  )}
                  {!eventsLoading && events.map((r) => (
                    <tr key={r.id}>
                      <td style={{ padding: "12px 8px", color: C.text, borderBottom: `1px solid ${C.borderLight}` }}>{fmtRelativeDate(r.occurred_at)}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}`, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}>{r.id}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={r.actor} color={colorForName(r.actor)} />
                          <span style={{ color: C.text, fontWeight: 700 }}>{r.actor}</span>
                        </span>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={{ bg: C.bg, fg: C.textSub, bd: C.border }}>{r.role}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.text, borderBottom: `1px solid ${C.borderLight}`, fontWeight: 700 }}>{r.event}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{r.object}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={RISK_TONE[r.risk]}>{r.risk}</Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", marginTop: 14, color: C.textSub, fontSize: 12 }}>
              <span>Showing {events.length} of {fmtCount(auditEvents30d)} events · Last {timeRange} days</span>
              <button onClick={loadEvents} style={{ ...ghostBtn(C), padding: "6px 14px", fontSize: 12 }}>Refresh</button>
            </div>
          </div>
        )}

        {/* POLICY HISTORY */}
        {tab === "policy" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 1fr auto", gap: 8, marginBottom: 14, alignItems: "center" }}>
              <FilterInput C={C} value={polSearch} onChange={setPolSearch} placeholder="Search policy name, type..." />
              <FilterSelect C={C} value={polType} onChange={setPolType} options={[
                { value: "all", label: "All policy types" },
                { value: "Security", label: "Security" },
                { value: "Leave",    label: "Leave" },
                { value: "Holiday",  label: "Holiday" },
                { value: "Overtime", label: "Overtime" },
              ]} />
              <FilterSelect C={C} value={polStatus} onChange={setPolStatus} options={[
                { value: "all", label: "All statuses" },
                { value: "Current",        label: "Current" },
                { value: "Superseded",     label: "Superseded" },
                { value: "Pending review", label: "Pending review" },
              ]} />
              <button onClick={() => setModal({ kind: "create_policy" })} style={ghostBtn(C)} disabled={!isAdmin}>Record version</button>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Policy name", "Policy type", "Changed by", "Version", "Effective date", "Countries / teams", "Status", "Actions"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {polLoading && <tr><td colSpan={8} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading…</td></tr>}
                  {!polLoading && policies.length === 0 && <tr><td colSpan={8} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No policy versions found.</td></tr>}
                  {!polLoading && policies.map((p) => (
                    <tr key={p.id}>
                      <td style={{ padding: "12px 8px", color: C.text, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}` }}>{p.policy_name}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={POLICY_TYPE_TONE[p.policy_type] || { bg: C.bg, fg: C.textSub, bd: C.border }}>{p.policy_type}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={p.changed_by} color={colorForName(p.changed_by)} />
                          <span style={{ color: C.text, fontWeight: 700 }}>{p.changed_by}</span>
                        </span>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{p.version}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{fmtDate(p.effective_date)}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{p.scope || "—"}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={STATUS_TONE[p.status]}>{p.status}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.brand, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}`, fontSize: 12 }}>
                        <span onClick={() => setModal({ kind: "view_policy", payload: p })} style={{ marginRight: 10, cursor: "pointer" }}>View</span>
                        <span onClick={() => submitExport({ export_type: `Policy: ${p.policy_name}`, scope: p.scope || "Workspace", fmt: "PDF", reason: `Policy record export ${p.version}` })} style={{ cursor: "pointer" }}>Export</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, color: C.textSub, fontSize: 12 }}>Showing {policies.length} policy version{policies.length === 1 ? "" : "s"}</div>
          </div>
        )}

        {/* ACCESS EVENTS */}
        {tab === "access" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 1fr auto", gap: 8, marginBottom: 14, alignItems: "center" }}>
              <FilterInput C={C} value={accSearch} onChange={setAccSearch} placeholder="Search user, IP, session ID..." />
              <FilterSelect C={C} value={accRisk} onChange={setAccRisk} options={[
                { value: "all", label: "All risk levels" },
                { value: "Critical", label: "Critical" },
                { value: "High",     label: "High" },
                { value: "Medium",   label: "Medium" },
                { value: "Low",      label: "Low" },
                { value: "Info",     label: "Info" },
              ]} />
              <FilterSelect C={C} value={accDays} onChange={setAccDays} options={[
                { value: "7",  label: "Last 7 days" },
                { value: "30", label: "Last 30 days" },
                { value: "90", label: "Last 90 days" },
              ]} />
              <button onClick={() => submitExport({ export_type: "Access events report", scope: `Last ${accDays} days`, fmt: "CSV", reason: "Manual access-events export" })} style={ghostBtn(C)} disabled={!isAdmin}>Export</button>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Date & time", "User", "Role", "Event", "Location / IP", "Device", "Result", "Risk"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {accLoading && <tr><td colSpan={8} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading…</td></tr>}
                  {!accLoading && accessEvents.length === 0 && <tr><td colSpan={8} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No access events in this window.</td></tr>}
                  {!accLoading && accessEvents.map((e) => (
                    <tr key={e.id}>
                      <td style={{ padding: "12px 8px", color: C.text, borderBottom: `1px solid ${C.borderLight}` }}>{fmtRelativeDate(e.occurred_at)}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={e.user} color={colorForName(e.user)} />
                          <span style={{ color: C.text, fontWeight: 700 }}>{e.user}</span>
                        </span>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={{ bg: C.bg, fg: C.textSub, bd: C.border }}>{e.role}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", color: e.risk === "Critical" || e.risk === "High" ? C.red : C.text, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}` }}>{e.event}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{e.location}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{e.device}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={STATUS_TONE[e.result]}>{e.result}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={RISK_TONE[e.risk]}>{e.risk}</Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, color: C.textSub, fontSize: 12 }}>Showing {accessEvents.length} access event{accessEvents.length === 1 ? "" : "s"} · Last {accDays} days</div>
          </div>
        )}

        {/* EXPORTS */}
        {tab === "exports" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 1fr auto", gap: 8, marginBottom: 14, alignItems: "center" }}>
              <FilterInput C={C} value={expSearch} onChange={setExpSearch} placeholder="Search export ID, type..." />
              <FilterSelect C={C} value={expType} onChange={setExpType} options={[
                { value: "all", label: "All export types" },
                { value: "Audit report",         label: "Audit report" },
                { value: "Leave report",         label: "Leave report" },
                { value: "Payroll-support export", label: "Payroll export" },
                { value: "Timesheet export",     label: "Timesheet export" },
                { value: "Access events report", label: "Access events" },
              ]} />
              <FilterSelect C={C} value={expStatus} onChange={setExpStatus} options={[
                { value: "all", label: "All statuses" },
                { value: "Available", label: "Available" },
                { value: "Expired",   label: "Expired" },
                { value: "Pending",   label: "Pending" },
              ]} />
              <button onClick={() => setModal({ kind: "create_export" })} style={ghostBtn(C)} disabled={!isAdmin}>Record export</button>
            </div>

            <div style={{
              background: C.greenBg, border: `1px solid ${C.greenBdr}`, borderRadius: 8,
              padding: "10px 14px", color: C.green, fontSize: 12, fontWeight: 600, marginBottom: 14,
            }}>
              Every export requires a reason before download. The reason is immutably recorded in the audit log with actor, role, IP, and session metadata.
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Export date", "Exported by", "Export type", "Scope", "Format", "Reason", "Status"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {expLoading && <tr><td colSpan={7} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading…</td></tr>}
                  {!expLoading && exports.length === 0 && <tr><td colSpan={7} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No exports recorded yet.</td></tr>}
                  {!expLoading && exports.map((e) => (
                    <tr key={e.id}>
                      <td style={{ padding: "12px 8px", color: C.text, borderBottom: `1px solid ${C.borderLight}` }}>{fmtRelativeDate(e.exported_at)}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={e.exported_by} color={colorForName(e.exported_by)} />
                          <span style={{ color: C.text, fontWeight: 700 }}>{e.exported_by}</span>
                        </span>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.text, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}` }}>{e.export_type}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{e.scope || "—"}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={{ bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" }}>{e.fmt}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{e.reason}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={STATUS_TONE[e.status]}>{e.status}</Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, color: C.textSub, fontSize: 12 }}>Showing {exports.length} export record{exports.length === 1 ? "" : "s"}</div>
          </div>
        )}

        {/* EVIDENCE PACKAGES */}
        {tab === "evidence" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 1fr auto", gap: 8, marginBottom: 14, alignItems: "center" }}>
              <FilterInput C={C} value={evSearch} onChange={setEvSearch} placeholder="Search package ID, name..." />
              <FilterSelect C={C} value={evPurpose} onChange={setEvPurpose} options={[
                { value: "all", label: "All purposes" },
                { value: "HR investigation",  label: "HR investigation" },
                { value: "Payroll support",   label: "Payroll support" },
                { value: "Compliance audit",  label: "Compliance audit" },
                { value: "Legal review",      label: "Legal review" },
              ]} />
              <FilterSelect C={C} value={evStatus} onChange={setEvStatus} options={[
                { value: "all", label: "All statuses" },
                { value: "Approved",        label: "Approved" },
                { value: "Pending review",  label: "Pending review" },
                { value: "Held",            label: "Held" },
              ]} />
              <button onClick={() => setModal({ kind: "create_evidence" })} style={{ ...primaryBtn(C), background: "#0F4C3A" }} disabled={!isAdmin}>Create Evidence Package</button>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Package ID", "Package name", "Purpose", "Scope", "Created by", "Redaction", "Status"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {evLoading && <tr><td colSpan={7} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading…</td></tr>}
                  {!evLoading && evidence.length === 0 && <tr><td colSpan={7} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No evidence packages yet. Use the button above to create one.</td></tr>}
                  {!evLoading && evidence.map((p) => (
                    <tr key={p.id}>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}`, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}>{p.code}</td>
                      <td style={{ padding: "12px 8px", color: C.text, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}` }}>{p.name}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={PURPOSE_TONE[p.purpose]}>{p.purpose}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{p.scope || "—"}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={p.created_by} color={colorForName(p.created_by)} />
                          <span style={{ color: C.text, fontWeight: 700 }}>{p.created_by}</span>
                        </span>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={{ bg: C.bg, fg: C.textSub, bd: C.border }}>{p.redaction}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={STATUS_TONE[p.status]}>{p.status}</Chip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, color: C.textSub, fontSize: 12 }}>Showing {evidence.length} package{evidence.length === 1 ? "" : "s"}</div>
          </div>
        )}

        {/* LEGAL HOLD */}
        {tab === "hold" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr auto", gap: 8, marginBottom: 14, alignItems: "center" }}>
              <FilterInput C={C} value={holdSearch} onChange={setHoldSearch} placeholder="Search hold name, matter reference..." />
              <FilterSelect C={C} value={holdStatus} onChange={setHoldStatus} options={[
                { value: "all", label: "All statuses" },
                { value: "Active hold", label: "Active hold" },
                { value: "Released",    label: "Released" },
              ]} />
              <button onClick={() => setModal({ kind: "create_hold" })} style={{ ...primaryBtn(C), background: "#0F4C3A" }} disabled={!isAdmin}>Create Legal Hold</button>
            </div>

            <div style={{
              background: "#FEF2F2", border: `1px solid #FECDCA`, borderRadius: 8,
              padding: "10px 14px", color: "#B42318", fontSize: 12, fontWeight: 600, marginBottom: 14,
            }}>
              Legal hold overrides ordinary retention deletion. Records under hold cannot be deleted regardless of retention schedule. Requires MFA step-up to create or release.
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Hold name", "Matter reference", "Scope", "Created by", "Start date", "Reason", "Status", "Actions"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {holdLoading && <tr><td colSpan={8} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading…</td></tr>}
                  {!holdLoading && holds.length === 0 && <tr><td colSpan={8} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>No legal holds. Use the button above to create one.</td></tr>}
                  {!holdLoading && holds.map((h) => (
                    <tr key={h.id}>
                      <td style={{ padding: "12px 8px", color: C.text, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}` }}>{h.name}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}`, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }}>{h.matter_ref}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{h.scope || "—"}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={h.created_by} color={colorForName(h.created_by)} />
                          <span style={{ color: C.text, fontWeight: 700 }}>{h.created_by}</span>
                        </span>
                      </td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{fmtDate(h.start_date)}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}`, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={h.reason}>{h.reason}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={STATUS_TONE[h.status]}>{h.status}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}`, fontSize: 12 }}>
                        {h.status === "Active hold" && isAdmin
                          ? <span onClick={() => releaseHold(h.id)} style={{ color: C.red, fontWeight: 700, cursor: "pointer" }}>Release</span>
                          : <span style={{ color: C.textSub }}>—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, color: C.textSub, fontSize: 12 }}>
              {holds.filter((h) => h.status === "Active hold").length} active legal hold{holds.filter((h) => h.status === "Active hold").length === 1 ? "" : "s"}. Creating or releasing a hold requires MFA re-authentication and creates an immutable audit event.
            </div>
          </div>
        )}

        {/* RETENTION */}
        {tab === "retention" && (
          <div style={{ padding: 18 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
              <div style={{ maxWidth: 640 }}>
                <h3 style={{ margin: 0, color: C.text, fontSize: 16, fontWeight: 800 }}>Retention rules</h3>
                <p style={{ margin: "6px 0 0", color: C.textSub, fontSize: 13, lineHeight: 1.55 }}>
                  Retention periods are governed by jurisdictional statute and your
                  workspace policy. Records under an active legal hold are exempt
                  from automatic deletion until the hold is released.
                </p>
              </div>
              <button onClick={loadRetention} style={ghostBtn(C)}>Refresh</button>
            </div>

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {["Record category", "Retention period", "Legal basis", "Deletion behaviour", "Legal hold", "Actions"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "10px 8px", color: C.textSub, borderBottom: `1px solid ${C.border}`, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: ".5px" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {retLoading && <tr><td colSpan={6} style={{ padding: 24, color: C.textSub, textAlign: "center" }}>Loading…</td></tr>}
                  {!retLoading && retention.map((r) => (
                    <tr key={r.id}>
                      <td style={{ padding: "12px 8px", color: C.text, fontWeight: 700, borderBottom: `1px solid ${C.borderLight}` }}>{r.category}</td>
                      <td style={{ padding: "12px 8px", color: C.text, borderBottom: `1px solid ${C.borderLight}` }}>{r.retention_period}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{r.legal_basis}</td>
                      <td style={{ padding: "12px 8px", color: C.textSub, borderBottom: `1px solid ${C.borderLight}` }}>{r.deletion_behaviour}</td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}` }}>
                        <Chip C={C} tone={STATUS_TONE["Approved"]}>{r.legal_hold_behaviour}</Chip>
                      </td>
                      <td style={{ padding: "12px 8px", borderBottom: `1px solid ${C.borderLight}`, fontSize: 12 }}>
                        {isAdmin ? <span onClick={() => setModal({ kind: "edit_retention", payload: r })} style={{ color: C.brand, fontWeight: 700, cursor: "pointer" }}>Edit</span> : <span style={{ color: C.textSub }}>—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* Footer status strip */}
      <div style={{
        position: "sticky", bottom: 0, marginTop: 18, background: C.surface,
        border: `1px solid ${C.border}`, borderRadius: 10, padding: "12px 16px",
        display: "flex", justifyContent: "space-between", alignItems: "center",
        gap: 12, flexWrap: "wrap", boxShadow: "0 -4px 16px rgba(0,0,0,.04)",
      }}>
        <div style={{ color: C.textSub, fontSize: 12, fontWeight: 600 }}>
          Audit logging active · {fmtCount(auditEvents30d)} events this month · Policy {policyVersion} · {fmtCount(legalHoldsActive)} active legal hold{legalHoldsActive === 1 ? "" : "s"} ·{" "}
          <span onClick={() => setModal({ kind: "export_audit" })} style={{ color: C.brand, fontWeight: 700, cursor: "pointer" }}>Export audit report</span>
        </div>
      </div>

      {/* Modals */}
      {modal?.kind === "export_audit"   && <ExportAuditModal     C={C} onClose={() => setModal(null)} onSubmit={(reason) => { downloadAuditCsv(reason); setModal(null); }} timeRange={timeRange} />}
      {modal?.kind === "create_export"  && <CreateExportModal    C={C} onClose={() => setModal(null)} onSubmit={submitExport} />}
      {modal?.kind === "create_evidence"&& <CreateEvidenceModal  C={C} onClose={() => setModal(null)} onSubmit={submitEvidence} />}
      {modal?.kind === "create_hold"    && <CreateLegalHoldModal C={C} onClose={() => setModal(null)} onSubmit={submitLegalHold} />}
      {modal?.kind === "create_policy"  && <CreatePolicyModal    C={C} onClose={() => setModal(null)} onSubmit={submitPolicyVersion} />}
      {modal?.kind === "view_policy"    && <ViewPolicyModal      C={C} onClose={() => setModal(null)} policy={modal.payload} />}
      {modal?.kind === "edit_retention" && <EditRetentionModal   C={C} onClose={() => setModal(null)} rule={modal.payload} onSubmit={(body) => submitRetention(modal.payload.rule_key, body)} />}
    </div>
  );
}

// ── Modals ────────────────────────────────────────────────────────────────
function ExportAuditModal({ C, onClose, onSubmit, timeRange }) {
  const [reason, setReason] = useState("");
  const canSubmit = reason.trim().length >= 4;
  return (
    <Modal C={C} title="Export audit report" onClose={onClose}
      footer={<>
        <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
        <button disabled={!canSubmit} onClick={() => onSubmit(reason.trim())}
          style={{ ...primaryBtn(C), background: "#0F4C3A", opacity: canSubmit ? 1 : .5 }}>Generate CSV</button>
      </>}>
      <p style={{ margin: "0 0 14px", color: C.textSub, fontSize: 13, lineHeight: 1.55 }}>
        The export will contain the last <b>{timeRange}</b> days of audit events.
        Your reason will be recorded in the audit log alongside actor, role, IP, and session metadata.
      </p>
      <Field C={C} label="Reason for export" hint="Minimum 4 characters. Examples: 'Quarterly compliance review', 'Subject access request'.">
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
          placeholder="Why are you exporting this report?"
          style={{ ...fieldInput(C), resize: "vertical" }} />
      </Field>
    </Modal>
  );
}

function CreateExportModal({ C, onClose, onSubmit }) {
  const [form, setForm] = useState({ export_type: "Audit report", scope: "", fmt: "CSV", reason: "" });
  const ok = form.export_type && form.reason.trim().length >= 4;
  return (
    <Modal C={C} title="Record an export" onClose={onClose}
      footer={<>
        <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
        <button disabled={!ok} onClick={() => onSubmit(form)}
          style={{ ...primaryBtn(C), background: "#0F4C3A", opacity: ok ? 1 : .5 }}>Record export</button>
      </>}>
      <Field C={C} label="Export type">
        <select value={form.export_type} onChange={(e) => setForm({ ...form, export_type: e.target.value })} style={fieldInput(C)}>
          <option>Audit report</option>
          <option>Leave report</option>
          <option>Payroll-support export</option>
          <option>Timesheet export</option>
          <option>Access events report</option>
        </select>
      </Field>
      <Field C={C} label="Scope">
        <input value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}
          placeholder="e.g. All workspace · May 2026" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Format">
        <select value={form.fmt} onChange={(e) => setForm({ ...form, fmt: e.target.value })} style={fieldInput(C)}>
          <option>CSV</option><option>XLSX</option><option>PDF</option><option>PDF + CSV</option>
        </select>
      </Field>
      <Field C={C} label="Reason" hint="Reason is immutably recorded with the export.">
        <textarea rows={3} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })}
          style={{ ...fieldInput(C), resize: "vertical" }} placeholder="Why is this export being created?" />
      </Field>
    </Modal>
  );
}

function CreateEvidenceModal({ C, onClose, onSubmit }) {
  const [form, setForm] = useState({ name: "", purpose: "HR investigation", scope: "", redaction: "Standard", notes: "" });
  const ok = form.name.trim().length >= 3;
  return (
    <Modal C={C} title="Create evidence package" onClose={onClose}
      footer={<>
        <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
        <button disabled={!ok} onClick={() => onSubmit(form)}
          style={{ ...primaryBtn(C), background: "#0F4C3A", opacity: ok ? 1 : .5 }}>Create package</button>
      </>}>
      <Field C={C} label="Package name">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder="e.g. Timesheet Dispute — Jane Doe" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Purpose">
        <select value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} style={fieldInput(C)}>
          <option>HR investigation</option>
          <option>Payroll support</option>
          <option>Compliance audit</option>
          <option>Legal review</option>
        </select>
      </Field>
      <Field C={C} label="Scope">
        <input value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}
          placeholder="e.g. Jane Doe · Jan–Apr 2026" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Redaction">
        <select value={form.redaction} onChange={(e) => setForm({ ...form, redaction: e.target.value })} style={fieldInput(C)}>
          <option>Standard</option><option>PII redacted</option><option>Full redaction</option>
        </select>
      </Field>
      <Field C={C} label="Notes (optional)">
        <textarea rows={3} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })}
          style={{ ...fieldInput(C), resize: "vertical" }} />
      </Field>
    </Modal>
  );
}

function CreateLegalHoldModal({ C, onClose, onSubmit }) {
  const year = new Date().getFullYear();
  const [form, setForm] = useState({ name: "", matter_ref: `MATTER-${year}-`, scope: "", reason: "" });
  const ok = form.name.trim().length >= 3 && form.matter_ref.trim().length >= 6 && form.reason.trim().length >= 8;
  return (
    <Modal C={C} title="Create legal hold" onClose={onClose}
      footer={<>
        <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
        <button disabled={!ok} onClick={() => onSubmit(form)}
          style={{ ...primaryBtn(C), background: "#0F4C3A", opacity: ok ? 1 : .5 }}>Create hold</button>
      </>}>
      <div style={{ background: "#FEF2F2", border: "1px solid #FECDCA", borderRadius: 8, padding: "10px 14px", color: "#B42318", fontSize: 12, fontWeight: 600, marginBottom: 14 }}>
        A legal hold blocks retention deletion for the records in scope until released. This action is immutably logged.
      </div>
      <Field C={C} label="Hold name">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder="e.g. Jane Doe — Role Dispute Hold" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Matter reference">
        <input value={form.matter_ref} onChange={(e) => setForm({ ...form, matter_ref: e.target.value })}
          placeholder={`MATTER-${year}-XXXX`} style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Scope">
        <input value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}
          placeholder="e.g. Jane Doe · All records" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Reason" hint="Minimum 8 characters.">
        <textarea rows={3} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })}
          style={{ ...fieldInput(C), resize: "vertical" }}
          placeholder="Why is this hold required?" />
      </Field>
    </Modal>
  );
}

function CreatePolicyModal({ C, onClose, onSubmit }) {
  const [form, setForm] = useState({
    policy_name: "", policy_type: "Security", version: "",
    effective_date: "", scope: "", status: "Current",
  });
  const ok = form.policy_name.trim().length >= 3 && form.version.trim().length >= 1;
  return (
    <Modal C={C} title="Record policy version" onClose={onClose}
      footer={<>
        <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
        <button disabled={!ok} onClick={() => onSubmit({
          ...form,
          effective_date: form.effective_date ? new Date(form.effective_date).toISOString() : null,
        })} style={{ ...primaryBtn(C), background: "#0F4C3A", opacity: ok ? 1 : .5 }}>Save version</button>
      </>}>
      <Field C={C} label="Policy name">
        <input value={form.policy_name} onChange={(e) => setForm({ ...form, policy_name: e.target.value })}
          placeholder="e.g. Annual Leave Policy — India" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Policy type">
        <select value={form.policy_type} onChange={(e) => setForm({ ...form, policy_type: e.target.value })} style={fieldInput(C)}>
          <option>Security</option><option>Leave</option><option>Holiday</option><option>Overtime</option>
        </select>
      </Field>
      <Field C={C} label="Version">
        <input value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })}
          placeholder="v1.0" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Effective date">
        <input type="date" value={form.effective_date} onChange={(e) => setForm({ ...form, effective_date: e.target.value })} style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Scope">
        <input value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}
          placeholder="e.g. India · All teams" style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Status" hint="Saving as 'Current' supersedes any prior 'Current' version of the same policy.">
        <select value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })} style={fieldInput(C)}>
          <option>Current</option><option>Pending review</option><option>Superseded</option>
        </select>
      </Field>
    </Modal>
  );
}

function ViewPolicyModal({ C, onClose, policy }) {
  return (
    <Modal C={C} title={policy?.policy_name || "Policy version"} onClose={onClose}
      footer={<button onClick={onClose} style={ghostBtn(C)}>Close</button>} width={560}>
      <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: "10px 14px", color: C.text, fontSize: 13 }}>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Type</div>            <div>{policy?.policy_type}</div>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Version</div>         <div>{policy?.version}</div>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Effective date</div>  <div>{fmtDate(policy?.effective_date)}</div>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Scope</div>           <div>{policy?.scope || "—"}</div>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Status</div>          <div><Chip C={C} tone={STATUS_TONE[policy?.status]}>{policy?.status}</Chip></div>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Changed by</div>      <div>{policy?.changed_by}</div>
        <div style={{ color: C.textSub, fontWeight: 700 }}>Recorded at</div>     <div>{fmtRelativeDate(policy?.created_at)}</div>
      </div>
    </Modal>
  );
}

function EditRetentionModal({ C, onClose, rule, onSubmit }) {
  const [form, setForm] = useState({
    retention_period:     rule?.retention_period || "",
    legal_basis:          rule?.legal_basis || "",
    deletion_behaviour:   rule?.deletion_behaviour || "",
    legal_hold_behaviour: rule?.legal_hold_behaviour || "Honoured",
  });
  return (
    <Modal C={C} title={`Edit retention — ${rule?.category}`} onClose={onClose}
      footer={<>
        <button onClick={onClose} style={ghostBtn(C)}>Cancel</button>
        <button onClick={() => onSubmit(form)} style={{ ...primaryBtn(C), background: "#0F4C3A" }}>Save changes</button>
      </>}>
      <Field C={C} label="Retention period">
        <input value={form.retention_period} onChange={(e) => setForm({ ...form, retention_period: e.target.value })} style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Legal basis">
        <input value={form.legal_basis} onChange={(e) => setForm({ ...form, legal_basis: e.target.value })} style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Deletion behaviour">
        <input value={form.deletion_behaviour} onChange={(e) => setForm({ ...form, deletion_behaviour: e.target.value })} style={fieldInput(C)} />
      </Field>
      <Field C={C} label="Legal hold behaviour">
        <select value={form.legal_hold_behaviour} onChange={(e) => setForm({ ...form, legal_hold_behaviour: e.target.value })} style={fieldInput(C)}>
          <option>Honoured</option><option>Always honoured</option><option>Not applicable</option>
        </select>
      </Field>
    </Modal>
  );
}
