import React, { useState, useEffect, useCallback, useRef } from "react";
import { API_BASE, WS_URL } from "../config";
import API from "../api/axios";
import AdminSidebar from "../components/AdminSidebar";
import TopNavbar from "../components/TopNavbar";
import { useCapability } from "../entitlements/Gate";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { refreshAccessToken } from "../api/refresh";
import { openLeaveAttachment } from "../utils/leaveAttachment";
import LeaveLedgerModal from "../components/leave/LeaveLedgerModal";
import MonthlyLimitControl from "../components/leave/MonthlyLimitControl";

const TOKEN = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

// H1: delegate to the shared axios instance for in-flight GET dedup +
// centralized 401 silent-refresh (+ Electron-safe logout). Preserves the
// original contract: returns the parsed body, and on a non-2xx returns the
// error body instead of throwing. Auth header + base URL come from the axios
// interceptor (readToken() reads the same zt_token/token keys as TOKEN()).
const apiFetch = async (path, opts = {}) => {
  const isForm = opts.body instanceof FormData;
  const headers = { ...(opts.headers || {}) };
  if (!isForm && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  try {
    const res = await API.request({
      url: path,
      method: (opts.method || "GET").toLowerCase(),
      data: opts.body,
      headers,
    });
    return res.data;
  } catch (err) {
    if (err?.response?.data !== undefined) return err.response.data;
    throw err;
  }
};

const STATUS_CFG = {
  submitted:    { bg: "#EFF6FF", color: "#1D4ED8", border: "#93C5FD", label: "Submitted",    dot: "#3B82F6" },
  under_review: { bg: "#FFF7ED", color: "#C2410C", border: "#FED7AA", label: "Under Review", dot: "#F97316" },
  approved:     { bg: "#F0FDF4", color: "#2F8F5C", border: "#A8E0BD", label: "Approved",      dot: "#44B97B" },
  rejected:     { bg: "#FEF2F2", color: "#B91C1C", border: "#FCA5A5", label: "Declined",      dot: "#EF4444" },
  locked_for_payroll: { bg: "#F5F3FF", color: "#6D28D9", border: "#C4B5FD", label: "Locked", dot: "#8B5CF6" },
};

const Badge = ({ status }) => {
  const c = STATUS_CFG[status] || { bg: "#F9FAFB", color: "#374151", border: "#E5E7EB", label: status, dot: "#94A3B8" };
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6,
      background: c.bg, color: c.color, border: `1px solid ${c.border}`,
      borderRadius: 20, padding: "3px 12px", fontSize: 12, fontWeight: 600 }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: c.dot, display: "inline-block" }} />
      {c.label}
    </span>
  );
};

const LEAVE_TYPE_ICONS = {
  "Annual Leave": "🏖️", "Sick Leave": "🤒", "Casual Leave": "🗓️",
  "Emergency Leave": "🚨", "Bereavement Leave": "🕊️", "Compensatory Leave": "⚖️",
  "Maternity Leave": "🤱", "Paternity Leave": "👨‍👶", "Adoption Leave": "🧡",
  "Company Leave": "🏢", "Unpaid Leave": "📋",
};

const FILTERS = ["all", "submitted", "under_review", "approved", "rejected"];

export default function AdminLeave() {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  // leave.approvals (Governed+) gates acting on a request. Viewing the request
  // (leave.manage) is All-plan, so the detail panel stays visible; only the
  // approve/reject/review actions are withheld when the projection withholds
  // the capability (LOCK for a Verified admin — the frontend follows it).
  const leaveApprovals = useCapability("leave.approvals");
  // Theme-aware form styles so the Manage Types form is readable in dark
  // mode (used to render with hard-coded light backgrounds and dark text).
  const labelStyle = { display: "block", fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 6 };
  const inputStyle = {
    width: "100%", padding: "10px 14px",
    border: `1px solid ${C.border}`, borderRadius: 8,
    fontSize: 13, color: C.text, background: C.surface,
    boxSizing: "border-box", outline: "none", fontFamily: "inherit",
    colorScheme: isDark ? "dark" : "light",
  };
  const hasNoAnnualCap = (item) => !!(item?.no_annual_limit || (item?.is_earned && Number(item?.annual_limit ?? 0) === 0));
  const limitLabelFor = (item) => {
    if (hasNoAnnualCap(item)) return T.noAnnualCap || "No annual cap";
    if (item?.is_unlimited || item?.annual_limit >= 99) return T.unlimited || "Unlimited";
    if (item?.is_earned) return T.leavePageEarnedFromOvertime || "Earned from worked dates";
    return `${item?.annual_limit ?? 0} ${T.daysPerYear || "days/yr"}`;
  };
  const [leaves, setLeaves]         = useState([]);
  const [leaveTypes, setLeaveTypes] = useState([]);
  const [filter, setFilter]         = useState("all");
  const [search, setSearch]         = useState("");
  const [selected, setSelected]     = useState(null);
  // Surfaced next to the 📎 chip when the document can't be opened (deleted
  // from storage, pop-up blocked, session expired).
  const [attachmentError, setAttachmentError] = useState(null);
  // Drop a stale error when the reviewer moves to a different request.
  useEffect(() => { setAttachmentError(null); }, [selected?.id]);
  // The requesting employee's balance for the requested leave type, shown at
  // the top of the review modal. A response for a request the reviewer has
  // already moved away from is ignored.
  const [reviewBalance, setReviewBalance] = useState({ loading: false, row: null, all: null, error: false, leaveYear: null });
  useEffect(() => {
    if (!selected?.user_id) { setReviewBalance({ loading: false, row: null, all: null, error: false, leaveYear: null }); return; }
    let stale = false;
    setReviewBalance({ loading: true, row: null, all: null, error: false, leaveYear: null });
    apiFetch(`/leave/admin/balances/${selected.user_id}`)
      .then(d => {
        if (stale) return;
        const row = d?.balances?.[selected.leave_type] || null;
        // The whole map travels too: a shared-pool type needs the pool row and
        // the other pooled types to explain its available balance.
        // `leave_year` is the period the figures cover (the modal's Policy Year).
        setReviewBalance({ loading: false, row, all: d?.balances || null, error: !d?.balances, leaveYear: d?.leave_year || null });
      })
      .catch(() => { if (!stale) setReviewBalance({ loading: false, row: null, all: null, error: true, leaveYear: null }); });
    return () => { stale = true; };
  }, [selected?.id, selected?.user_id, selected?.leave_type]);
  // The request's audit trail (submitted / sent to review / decided / locked /
  // edited) for the modal's Audit & Workflow timeline.
  // Keyed by request + status so a decision made since the last fetch reads as
  // loading rather than showing the old trail; state is only set once the
  // fetch settles.
  const historyKey = selected?.id ? `${selected.id}:${selected.status}` : null;
  const [historyState, setHistoryState] = useState({ key: null, items: null, error: false });
  useEffect(() => {
    if (!historyKey) return undefined;
    let stale = false;
    apiFetch(`/leave/${historyKey.split(":")[0]}/history`)
      .then(d => { if (!stale) setHistoryState({ key: historyKey, items: Array.isArray(d) ? d : null, error: !Array.isArray(d) }); })
      .catch(() => { if (!stale) setHistoryState({ key: historyKey, items: null, error: true }); });
    return () => { stale = true; };
  }, [historyKey]);
  const reviewHistory = historyKey && historyState.key === historyKey
    ? { loading: false, items: historyState.items, error: historyState.error }
    : { loading: !!historyKey, items: null, error: false };
  // For a Compensatory Leave request: what Temveris recorded on each date the
  // employee says they worked, shown next to the date in the modal.
  const [workedEvidence, setWorkedEvidence] = useState({ loading: false, dates: null });
  useEffect(() => {
    if (!selected?.id || !(Array.isArray(selected.worked_dates) && selected.worked_dates.length)) {
      setWorkedEvidence({ loading: false, dates: null }); return;
    }
    let stale = false;
    setWorkedEvidence({ loading: true, dates: null });
    apiFetch(`/leave/${selected.id}/worked-evidence`)
      .then(d => { if (!stale) setWorkedEvidence({ loading: false, dates: Array.isArray(d?.dates) ? d.dates : null }); })
      .catch(() => { if (!stale) setWorkedEvidence({ loading: false, dates: null }); });
    return () => { stale = true; };
  }, [selected?.id]);
  const [comment, setComment]       = useState("");
  const [commentError, setCommentError] = useState("");
  const [actioning, setActioning]   = useState(null);
  // Edit modal state — admins can patch leave details without going
  // through the approve/decline flow. Mirrors `selected` so opening the
  // edit form doesn't fight with the View modal.
  const [editLeave, setEditLeave]   = useState(null);
  const [editForm, setEditForm]     = useState({ leave_type: "", from_date: "", to_date: "", description: "" });
  const [savingEdit, setSavingEdit] = useState(false);
  // Swap-dates quick action — one-click way to move an already-approved
  // leave to new dates (e.g. an employee took the day a day earlier than
  // booked). Reuses PUT /leave/{id}; audited server-side.
  const [swapLeave, setSwapLeave]   = useState(null);
  const [swapForm, setSwapForm]     = useState({ from_date: "", to_date: "" });
  const [savingSwap, setSavingSwap] = useState(false);
  const [loading, setLoading]       = useState(true);
  const [toast, setToast]           = useState(null);
  const [riskHistory, setRiskHistory] = useState([]);
  const [tab, setTab]               = useState("leaves");
  const [notifications, setNotifications] = useState([]); // real-time leave notifications
  const prevLeaveCount = useRef(0);
  const prevLeaveIds   = useRef(new Set());

  // Admin: Manage leave types state
  const [newType, setNewType]       = useState({
    leave_type: "", annual_limit: 10, notice_days: 1,
    doc_required: false, monthly_accrual: 0, monthly_limit: "",
    description: "", eligibility: "", max_duration: "", required_docs: "", terms: "",
  });
  // Sets only the per-month cap of an existing type (other rules untouched).
  const saveMonthlyLimit = (leaveType, value) =>
    apiFetch(`/leave/admin/types/${encodeURIComponent(leaveType)}/monthly-limit`, {
      method: "PUT", body: JSON.stringify({ monthly_limit: value }),
    });
  const [typeSubmitting, setTypeSubmitting] = useState(false);
  const [typeSuccess, setTypeSuccess] = useState(null);
  const [typeError, setTypeError]   = useState(null);

  const showToast = (msg, type = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 4000);
  };

  const fetchLeaves = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch("/leave/requests");
      const arr  = Array.isArray(data) ? data : [];

      // Detect new leave requests for notifications
      const newIds = new Set(arr.map(l => l.id));
      if (prevLeaveIds.current.size > 0) {
        const freshLeaves = arr.filter(l =>
          !prevLeaveIds.current.has(l.id) && l.status === "submitted"
        );
        if (freshLeaves.length > 0) {
          setNotifications(prev => [
            ...freshLeaves.map(l => ({
              id: l.id,
              msg: `${l.name} has applied for ${l.leave_type} (${l.from_date} → ${l.to_date}, ${l.days} day${l.days !== 1 ? "s" : ""})`,
              ts: new Date().toLocaleTimeString(),
            })),
            ...prev,
          ].slice(0, 10));
        }
      }
      prevLeaveIds.current = newIds;
      setLeaves(arr);
    } catch {}
    setLoading(false);
  }, []);

  const fetchRisk = useCallback(async () => {
    try { setRiskHistory(await apiFetch("/risk-alert/history")); } catch {}
  }, []);

  const fetchLeaveTypes = useCallback(async () => {
    try { setLeaveTypes(await apiFetch("/leave/types")); } catch {}
  }, []);

  useEffect(() => { fetchLeaves(); fetchRisk(); fetchLeaveTypes(); }, []);

  // WebSocket for real-time new leave notifications.
  //
  // Uses the canonical WS_URL from config.js (already includes the `/ws` path
  // and the correct ws/wss scheme + host). The previous code derived the URL
  // from API_BASE — when API_BASE is "" (same-origin deploys) that produced a
  // bare "/ws" with no scheme/host, which threw and silently killed the socket.
  // Also adds the same teardown-guarded reconnect the AdminDashboard/Dashboard
  // sockets have, so a single network drop no longer permanently disables
  // real-time leave notifications for the session.
  useEffect(() => {
    let ws = null;
    let reconnectTimer = null;
    let tearingDown = false;
    let wsBackoff = 1000;

    const connect = () => {
      if (tearingDown) return;
      try {
        ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(TOKEN() || "")}`);
        ws.onmessage = (evt) => {
          try {
            const data = JSON.parse(evt.data);
            if (data.type === "new_leave_request") {
              setNotifications(prev => [{
                id: data.leave_id,
                msg: `${data.employee_name} has applied for ${data.leave_type} (${data.from_date} → ${data.to_date}, ${data.days} day${data.days !== 1 ? "s" : ""})`,
                ts: new Date().toLocaleTimeString(),
              }, ...prev].slice(0, 10));
              fetchLeaves();
            }
          } catch { /* ignore malformed frames */ }
        };
        ws.onclose = async (event) => {
          if (tearingDown || reconnectTimer) return;
          if (event.code === 1008) {
            const tok = await refreshAccessToken();
            if (tok === false) return;
            wsBackoff = 1000;
          }
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
        };
        ws.onerror = () => { try { ws && ws.close(); } catch { /* already closing */ } };
      } catch {
        if (!tearingDown && !reconnectTimer) {
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
        }
      }
    };

    connect();
    return () => {
      tearingDown = true;
      wsBackoff = 1000;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try { ws && ws.close(); } catch { /* already closed */ }
    };
  }, [fetchLeaves]);

  // WebSocket above already pushes leave updates in real time, so polling
  // only needs to act as a slow safety net (and not at all when hidden).
  useEffect(() => {
    const t = setInterval(() => {
      if (!document.hidden) fetchLeaves();
    }, 120000);
    return () => clearInterval(t);
  }, [fetchLeaves]);

  const updateStatus = async (leaveId, status, cmt = "") => {
    setCommentError("");
    setActioning(leaveId);
    try {
      const data = await apiFetch(`/leave/${leaveId}/status`, {
        method: "PUT",
        body: JSON.stringify({ status, comment: cmt }),
      });
      if (data.success) {
        // Backend includes email_sent / email_error so we can tell the
        // admin whether the employee actually got the email. The in-app
        // notification fires either way, but a failed email means they
        // should also reach the employee out-of-band.
        if (data.email_sent === false) {
          showToast(
            `Leave ${status} — email failed (employee notified in-app). ${data.email_error || ""}`.trim(),
            "error",
          );
        } else {
          showToast(`Leave ${status} successfully ✓ — employee notified by email`);
        }
        fetchLeaves();
        setSelected(null);
        setComment("");
        setCommentError("");
      } else {
        const msg = data.detail || (T.actionFailed || "Action failed");
        showToast(typeof msg === "string" ? msg : JSON.stringify(msg), "error");
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setActioning(null);
  };

  const openEditLeave = (r) => {
    // Pre-fill the form from the row. Dates from the API are ISO
    // datetimes, but the <input type="date"> needs YYYY-MM-DD only.
    const toDateInput = (v) => {
      if (!v) return "";
      try { return String(v).slice(0, 10); } catch { return ""; }
    };
    setEditForm({
      leave_type:  r.leave_type || "",
      from_date:   toDateInput(r.from_date),
      to_date:     toDateInput(r.to_date),
      description: r.description || r.reason || "",
    });
    setEditLeave(r);
  };

  const saveEditLeave = async () => {
    if (!editLeave) return;
    if (!editForm.leave_type.trim() || !editForm.from_date || !editForm.to_date) {
      showToast(T.fillAllFields || "Type, from-date and to-date are required", "error");
      return;
    }
    if (editForm.to_date < editForm.from_date) {
      showToast(T.toDateAfterFrom || "to_date must be on or after from_date", "error");
      return;
    }
    setSavingEdit(true);
    try {
      const data = await apiFetch(`/leave/${editLeave.id}`, {
        method: "PUT",
        body: JSON.stringify({
          leave_type:  editForm.leave_type.trim(),
          from_date:   editForm.from_date,
          to_date:     editForm.to_date,
          description: editForm.description,
        }),
      });
      if (data.detail) {
        showToast(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail), "error");
      } else {
        showToast(T.leaveUpdated || "Leave updated ✓");
        setEditLeave(null);
        fetchLeaves();
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setSavingEdit(false);
  };

  const openSwapDates = (r) => {
    const toDateInput = (v) => v ? String(v).slice(0, 10) : "";
    setSwapForm({ from_date: toDateInput(r.from_date), to_date: toDateInput(r.to_date) });
    setSwapLeave(r);
  };

  const saveSwapDates = async () => {
    if (!swapLeave) return;
    if (!swapForm.from_date || !swapForm.to_date) {
      showToast(T.bothDatesRequired || "Both dates are required", "error");
      return;
    }
    if (swapForm.to_date < swapForm.from_date) {
      showToast(T.toDateAfterFrom || "to_date must be on or after from_date", "error");
      return;
    }
    setSavingSwap(true);
    try {
      const data = await apiFetch(`/leave/${swapLeave.id}`, {
        method: "PUT",
        body: JSON.stringify({ from_date: swapForm.from_date, to_date: swapForm.to_date }),
      });
      if (data.detail) {
        showToast(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail), "error");
      } else {
        showToast(T.leaveUpdated || "Leave dates updated ✓");
        setSwapLeave(null);
        fetchLeaves();
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setSavingSwap(false);
  };

  const deleteLeave = async (r) => {
    if (!window.confirm(
      (T.confirmDeleteLeave || "Delete this leave request?") +
      `\n\n${r.user_name || r.user_email} · ${r.leave_type} · ${String(r.from_date).slice(0,10)} → ${String(r.to_date).slice(0,10)}`
    )) return;
    setActioning(r.id);
    try {
      const res = await fetch(`${API_BASE}/leave/${r.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${TOKEN()}` },
      });
      if (res.ok) {
        showToast(T.leaveDeleted || "Leave deleted ✓");
        setSelected(null);
        fetchLeaves();
      } else {
        let msg = T.actionFailed || "Action failed";
        try { const d = await res.json(); msg = d.detail || msg; } catch {}
        showToast(typeof msg === "string" ? msg : JSON.stringify(msg), "error");
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setActioning(null);
  };

  const handleCreateType = async (e) => {
    e.preventDefault();
    if (!newType.leave_type.trim()) { setTypeError(T.leaveTypeNameRequired || "Leave type name is required"); return; }
    setTypeSubmitting(true); setTypeError(null); setTypeSuccess(null);
    try {
      const terms = newType.terms.split("\n").map(t => t.trim()).filter(Boolean);
      const res = await fetch(`${API_BASE}/leave/admin/types`, {
        method: "POST",
        headers: { Authorization: `Bearer ${TOKEN()}`, "Content-Type": "application/json" },
        body: JSON.stringify({ ...newType, terms,
          monthly_limit: newType.monthly_limit === "" ? null : Number(newType.monthly_limit) }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setTypeSuccess(`${T.leaveType || "Leave type"} "${newType.leave_type}" ${T.leaveTypeCreatedSuccess || "created successfully"}`);
        setNewType({ leave_type: "", annual_limit: 10, notice_days: 1, doc_required: false, monthly_accrual: 0, monthly_limit: "", description: "", eligibility: "", max_duration: "", required_docs: "", terms: "" });
        fetchLeaveTypes();
      } else {
        setTypeError(data.detail || (T.failedToCreateLeaveType || "Failed to create leave type"));
      }
    } catch { setTypeError(T.networkError || "Network error"); }
    setTypeSubmitting(false);
  };

  const handleDisableType = async (leaveTypeName) => {
    if (!window.confirm(`${T.disable || "Disable"} "${leaveTypeName}"? ${T.disableTypeConfirm || "Employees won't be able to apply for this leave type."}`)) return;
    try {
      const encoded = encodeURIComponent(leaveTypeName);
      await apiFetch(`/leave/admin/types/${encoded}`, { method: "DELETE" });
      showToast(`"${leaveTypeName}" ${T.disable || "disabled"}`);
      fetchLeaveTypes();
    } catch { showToast(T.failedToDisable || "Failed to disable leave type", "error"); }
  };

  const handleEnableType = async (leaveTypeName) => {
    try {
      const encoded = encodeURIComponent(leaveTypeName);
      await apiFetch(`/leave/admin/types/${encoded}/enable`, { method: "POST" });
      showToast(`"${leaveTypeName}" ${T.enableType || "re-enabled"}`);
      fetchLeaveTypes();
    } catch { showToast(T.failedToEnable || "Failed to enable leave type", "error"); }
  };

  const filtered = leaves.filter(l => {
    const matchFilter = filter === "all" || l.status === filter;
    const matchSearch = !search ||
      l.name.toLowerCase().includes(search.toLowerCase()) ||
      l.email.toLowerCase().includes(search.toLowerCase()) ||
      l.leave_type.toLowerCase().includes(search.toLowerCase());
    return matchFilter && matchSearch;
  });

  const counts = FILTERS.reduce((acc, f) => {
    acc[f] = f === "all" ? leaves.length : leaves.filter(l => l.status === f).length;
    return acc;
  }, {});

  const pendingCount = leaves.filter(l => ["submitted", "under_review"].includes(l.status)).length;

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden", background: C.bg, fontFamily: "'DM Sans', system-ui, sans-serif" }}>
      <style>{`
        @keyframes ztAlFadeIn { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }
        /* Green hover system — matches the AdminSidebar accent so every
           interactive element on the page reads as part of the same family.
           Inactive cards/tabs/rows tint green on hover; active items keep
           their own treatment so the selection state stays unambiguous. */
        .zt-al-card { transition: transform .22s cubic-bezier(.34,1.56,.64,1), box-shadow .22s ease, border-color .22s ease, background .22s ease; }
        .zt-al-card:hover { transform: translateY(-2px); box-shadow: 0 8px 22px ${C.brandAccent}1F; border-color: ${C.brandAccent}; }
        .zt-al-tab { position: relative; transition: color .15s ease, background .15s ease; }
        .zt-al-tab[data-active="false"]:hover { background: ${C.brandAccentBg} !important; color: ${C.brandAccent} !important; }
        .zt-al-tab[data-active="false"]:hover svg { stroke: ${C.brandAccent} !important; }
        .zt-al-row:hover { background: ${C.brandAccentBg} !important; }
        .zt-al-anim { animation: ztAlFadeIn .35s cubic-bezier(.22,1,.36,1) both; }
        .zt-al-btn-primary { transition: transform .18s ease, box-shadow .18s ease; }
        .zt-al-btn-primary:not(:disabled):hover { transform: translateY(-1px); box-shadow: 0 6px 14px ${C.brandAccent}55; }
      `}</style>
      <AdminSidebar activeKey="manageleave" />
      <main style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0 }}>
        <TopNavbar
          title={T.leaveManagement || "Leave Management"}
          subtitle={T.reviewLeave || "Review and manage employee leave requests"}
          right={
            <button onClick={fetchLeaves} className="zt-al-btn-primary" style={{
              padding: "9px 18px", background: C.brand, color: "#fff",
              border: "none", borderRadius: C.rSm, fontWeight: 700, cursor: "pointer", fontSize: 13,
              boxShadow: `0 2px 6px ${C.brand}33`, fontFamily: "inherit",
              display: "inline-flex", alignItems: "center", gap: 6,
            }}>↻ {T.refreshBtn || "Refresh"}</button>
          }
        />
        <div style={{ flex: 1, overflowY: "auto", padding: "32px 24px" }}>
        <div style={{ maxWidth: 1200, margin: "0 auto" }}>

          {pendingCount > 0 && (
            <div className="zt-al-anim" style={{ marginBottom: 16, display:"inline-flex", alignItems:"center", gap:8,
              background: C.amberBg, color: C.amber, border: `1px solid ${C.amberBdr}`,
              borderRadius: 999, padding: "6px 14px", fontSize: 12, fontWeight: 700 }}>
              ⚑ {pendingCount} {T.pending || "pending"}
            </div>
          )}

        {/* Toast */}
        {toast && (
          <div style={{
            position: "fixed", top: 74, right: 24, zIndex: 9999,
            background: toast.type === "error" ? C.errorBg : C.successBg,
            border: `1px solid ${toast.type === "error" ? C.errorBorder : C.successBorder}`,
            color: toast.type === "error" ? C.error : C.success,
            borderRadius: 10, padding: "14px 20px", fontWeight: 600, fontSize: 14,
            boxShadow: "0 4px 20px rgba(0,0,0,0.1)",
          }}>
            {toast.type === "error" ? "❌" : "✅"} {toast.msg}
          </div>
        )}

        {/* Real-time notification banner */}
        {notifications.length > 0 && (
          <div style={{ background: "#EFF6FF", border: "1px solid #93C5FD", borderRadius: 10, padding: "12px 16px", marginBottom: 20 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <div style={{ fontWeight: 700, color: "#1D4ED8", fontSize: 13 }}>🔔 {T.newLeaveRequests || "New Leave Requests"}</div>
              <button onClick={() => setNotifications([])} style={{ background: "none", border: "none", color: "#94A3B8", cursor: "pointer", fontSize: 16 }}>✕</button>
            </div>
            {notifications.map((n, i) => (
              <div key={i} style={{ fontSize: 13, color: "#1E40AF", padding: "3px 0", borderTop: i > 0 ? "1px solid #BFDBFE" : "none" }}>
                🆕 {n.msg} <span style={{ color: "#94A3B8", fontSize: 11 }}>({n.ts})</span>
              </div>
            ))}
          </div>
        )}

        {/* Tabs — underlined style matches Payroll Calculator + Activity Log */}
        <div style={{
          display: "flex", gap: 4, marginBottom: 24,
          borderBottom: `1px solid ${C.border}`, overflowX: "auto",
        }}>
          {[
            ["leaves", T.leaveRequests || "Leave Requests", "📋"],
            ["manage", T.manageTypes   || "Manage Types",   "⚙️"],
            ["risk",   T.riskAlerts    || "Risk Alerts",    "⚠️"],
          ].map(([k, l, icon]) => {
            const isActive = tab === k;
            return (
              <button key={k} onClick={() => setTab(k)}
                className="zt-al-tab" data-active={isActive ? "true" : "false"}
                style={{
                  padding: "10px 18px", border: "none", background: "none",
                  fontFamily: "inherit", fontSize: 13, fontWeight: isActive ? 700 : 600,
                  color: isActive ? C.brand : C.textSub, cursor: "pointer",
                  borderBottom: `2px solid ${isActive ? C.brand : "transparent"}`,
                  marginBottom: -1, whiteSpace: "nowrap",
                  display: "inline-flex", alignItems: "center", gap: 6,
                  borderRadius: "8px 8px 0 0",
                }}>
                <span>{icon}</span>{l}
              </button>
            );
          })}
        </div>

        {/* ════════════ LEAVE REQUESTS TAB ════════════ */}
        {tab === "leaves" && (
          <>
            {/* Summary cards — match the SaaS-grade KPI treatment used by
                Payroll / Activity Log (hover lift + brand-tint border). */}
            <div style={{
              display: "grid", gap: 14,
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
              marginBottom: 24,
            }}>
              {FILTERS.map(f => {
                const isActive = filter === f;
                return (
                  <div key={f} onClick={() => setFilter(f)} className="zt-al-card" style={{
                    background: isActive ? C.brand : C.surface,
                    border: `1px solid ${isActive ? C.brand : C.border}`,
                    borderRadius: 12, padding: "18px 20px", cursor: "pointer",
                    boxShadow: isActive ? `0 6px 18px ${C.brand}33` : "0 1px 2px rgba(16,24,40,0.04)",
                    minHeight: 96, display: "flex", flexDirection: "column", justifyContent: "center",
                  }}>
                    <div style={{
                      fontSize: 11, fontWeight: 700,
                      color: isActive ? "rgba(255,255,255,0.85)" : C.textLight,
                      textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6,
                    }}>
                      {f === "all" ? (T.allRequests || "All Requests") : f.replace(/_/g, " ")}
                    </div>
                    <div style={{
                      fontSize: 26, fontWeight: 800, lineHeight: 1.1,
                      color: isActive ? "#fff" : C.text,
                    }}>{counts[f]}</div>
                  </div>
                );
              })}
            </div>

            {/* Search */}
            <div style={{ marginBottom: 16, position: "relative" }}>
              <span style={{
                position: "absolute", left: 14, top: "50%", transform: "translateY(-50%)",
                color: C.textLight, fontSize: 14, pointerEvents: "none",
              }}>🔍</span>
              <input placeholder={T.searchLeave||"Search by name, email or leave type..."}
                value={search} onChange={e => setSearch(e.target.value)}
                style={{ width: "100%", padding: "11px 16px 11px 38px",
                  border: `1px solid ${C.border}`, borderRadius: 8, fontFamily: "inherit",
                  fontSize: 13, background: C.surface, color: C.text,
                  boxSizing: "border-box", outline: "none",
                  transition: "border-color .15s ease, box-shadow .15s ease" }}
                onFocus={(e) => { e.target.style.borderColor = C.brand; e.target.style.boxShadow = `0 0 0 3px ${C.brand}1A`; }}
                onBlur={(e)  => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }} />
            </div>

            {/* Table */}
            <div style={{
              background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`,
              overflow: "hidden", boxShadow: "0 1px 2px rgba(16,24,40,0.04)",
            }}>
              {/* "Loading" only before the first rows arrive. Background
                  refreshes (the 2-minute poll, a live leave event, after a
                  decision) keep the current table on screen; swapping it for
                  "Loading..." each time made the page blink. */}
              {loading && leaves.length === 0 ? (
                <div style={{ textAlign: "center", padding: "48px 0", color: C.textSub, fontSize: 13 }}>{T.loading||"Loading..."}</div>
              ) : filtered.length === 0 ? (
                <div style={{ textAlign: "center", padding: "56px 24px", color: C.textSub, fontSize: 13 }}>
                  <div style={{
                    width: 64, height: 64, borderRadius: "50%",
                    background: `linear-gradient(135deg, ${C.brandBg}, ${C.surface})`,
                    border: `1px solid ${C.brandBdr}`, display: "inline-flex",
                    alignItems: "center", justifyContent: "center", marginBottom: 14,
                    fontSize: 26,
                  }}>📋</div>
                  <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>
                    {T.noLeaveRequestsTable || "No leave requests found"}
                  </p>
                </div>
              ) : (
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, fontFamily: "inherit" }}>
                  <thead>
                    <tr style={{ background: C.bg }}>
                      {[T.employee||"Employee",T.leaveType||"Leave Type",T.dates||"Dates",T.days||"Days",T.status||"Status",T.flags||"Flags",T.actions||"Actions"].map(h => (
                        <th key={h} style={{ padding: "10px 16px", textAlign: "left",
                          color: C.textLight, fontWeight: 700, fontSize: 11,
                          textTransform: "uppercase", letterSpacing: 0.4,
                          borderBottom: `1px solid ${C.borderLight}`, whiteSpace: "nowrap" }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map(r => {
                      const icon = LEAVE_TYPE_ICONS[r.leave_type] || "📋";
                      return (
                        <tr key={r.id} className="zt-al-row" style={{
                          borderBottom: `1px solid ${C.borderLight}`,
                          transition: "background .15s ease",
                        }}>
                          <td style={{ padding: "12px 16px" }}>
                            <div style={{ fontWeight: 600, color: C.text }}>{r.name}</div>
                            <div style={{ fontSize: 12, color: C.sub }}>{r.email}</div>
                            {r.department && <div style={{ fontSize: 11, color: "#94A3B8" }}>{r.department}</div>}
                          </td>
                          <td style={{ padding: "12px 16px", color: C.text, fontWeight: 600 }}>
                            <span style={{ marginRight: 6 }}>{icon}</span>{r.leave_type}
                          </td>
                          <td style={{ padding: "12px 16px", color: C.sub, whiteSpace: "nowrap" }}>
                            {r.from_date}<br /><span style={{ fontSize: 11 }}>→ {r.to_date}</span>
                          </td>
                          <td style={{ padding: "12px 16px", color: C.text, fontWeight: 700, textAlign: "center" }}>{r.days}</td>
                          <td style={{ padding: "12px 16px" }}><Badge status={r.status} /></td>
                          <td style={{ padding: "12px 16px" }}>
                            {r.hr_flag     && <div style={{ fontSize: 11, color: "#D97706", fontWeight: 600 }}>⚑ {T.hrReview || "HR Review"}</div>}
                            {r.is_lwp      && <div style={{ fontSize: 11, color: "#7C3AED", fontWeight: 600 }}>{T.lwp || "LWP"}</div>}
                            {r.has_attachment && <div style={{ fontSize: 11, color: "#0369A1", fontWeight: 600 }}>📎 {T.doc || "Doc"}</div>}
                          </td>
                          <td style={{ padding: "12px 16px" }}>
                            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                              <button onClick={() => { setSelected(r); setComment(""); setCommentError(""); }}
                                style={btnStyle("#F1F5F9","#475467")}>{T.view || "View"}</button>
                              {["submitted","under_review"].includes(r.status) && (
                                <button disabled={actioning === r.id}
                                  onClick={() => { setSelected(r); setComment(""); setCommentError(""); }}
                                  style={btnStyle("#F0FDF4","#2F8F5C")}>✓ {T.review || "Review"}</button>
                              )}
                              {/* Admin Edit + Delete — useful for fixing typos
                                  and clearing test data. Both audited
                                  server-side via the new /leave/{id}
                                  PUT/DELETE endpoints. */}
                              {r.status === "approved" && (
                                <button disabled={actioning === r.id}
                                  onClick={() => openSwapDates(r)}
                                  style={btnStyle("#ECFDF5","#047857")}>🔁 {T.swapDates || "Swap"}</button>
                              )}
                              <button disabled={actioning === r.id}
                                onClick={() => openEditLeave(r)}
                                style={btnStyle("#EFF6FF","#1D4ED8")}>✎ {T.edit || "Edit"}</button>
                              <button disabled={actioning === r.id}
                                onClick={() => deleteLeave(r)}
                                style={btnStyle("#FEF2F2","#B91C1C")}>🗑 {T.delete || "Delete"}</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}

        {/* ════════════ MANAGE LEAVE TYPES TAB ════════════ */}
        {tab === "manage" && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 380px", gap: 20, alignItems: "start" }}>
            {/* Existing types list */}
            <div>
              <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, overflow: "hidden" }}>
                <div style={{ padding: "16px 20px", borderBottom: `1px solid ${C.border}`, fontWeight: 700, color: C.text, fontSize: 15 }}>
                  {T.activeLeaveTypes || "Active Leave Types"} ({leaveTypes.length})
                </div>
                {leaveTypes.length === 0 ? (
                  <div style={{ padding: "32px", textAlign: "center", color: C.sub }}>{T.loading || "Loading..."}</div>
                ) : (
                  leaveTypes.map((lt, i) => (
                    <div key={lt.leave_type} style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 20px", borderBottom: i < leaveTypes.length - 1 ? `1px solid ${C.border}` : "none" }}>
                      <span style={{ fontSize: 22, flexShrink: 0 }}>{lt.icon}</span>
                      <div style={{ flex: 1 }}>
                        <div style={{ fontWeight: 600, color: C.text, fontSize: 14 }}>{lt.leave_type}</div>
                        <div style={{ fontSize: 12, color: C.sub }}>
                          {limitLabelFor(lt)} ·
                          {lt.notice_days === 0 ? ` ${T.noNotice || "No notice"}` : ` ${lt.notice_days}d ${T.notice || "notice"}`} ·
                          {lt.doc_required && !hasNoAnnualCap(lt) ? ` 📎 ${T.docRequired || "Doc required"}` : ` ${T.noDocs || "No docs"}`}
                          {lt.monthly_limit ? ` · ${T.maxPerMonthShort || "max"} ${lt.monthly_limit}d/${T.month || "month"}` : ""}
                        </div>
                      </div>
                      <MonthlyLimitControl leaveType={lt.leave_type} value={lt.monthly_limit}
                        save={saveMonthlyLimit} onSaved={fetchLeaveTypes} C={C} T={T} />
                      <button onClick={() => handleDisableType(lt.leave_type)}
                        style={{ padding: "5px 12px", background: C.errorBg, color: C.error, border: `1px solid ${C.errorBorder}`, borderRadius: 6, cursor: "pointer", fontWeight: 600, fontSize: 12 }}>
                        {T.disable || "Disable"}
                      </button>
                    </div>
                  ))
                )}
              </div>
            </div>

            {/* Add new type form */}
            <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 24 }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 16, fontWeight: 700, color: C.text }}>{T.addNewLeaveType || "Add New Leave Type"}</h3>

              {typeSuccess && (
                <div style={{ background: C.successBg, border: `1px solid ${C.successBorder}`, borderRadius: 8, padding: "10px 14px", marginBottom: 14, fontSize: 13, color: C.success, fontWeight: 600 }}>
                  ✅ {typeSuccess}
                </div>
              )}
              {typeError && (
                <div style={{ background: C.errorBg, border: `1px solid ${C.errorBorder}`, borderRadius: 8, padding: "10px 14px", marginBottom: 14, fontSize: 13, color: C.error, fontWeight: 600 }}>
                  ❌ {typeError}
                </div>
              )}

              <form onSubmit={handleCreateType}>
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{T.leaveTypeName || "Leave Type Name"} *</label>
                  <input value={newType.leave_type}
                    onChange={e => setNewType(n => ({ ...n, leave_type: e.target.value }))}
                    placeholder={T.leaveTypeNamePlaceholder || "e.g. Study Leave"} style={inputStyle} required />
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 12 }}>
                  <div>
                    <label style={labelStyle}>{T.annualLimitDays || "Annual Limit (days)"}</label>
                    <input type="number" value={newType.annual_limit} min={0}
                      onChange={e => setNewType(n => ({ ...n, annual_limit: parseInt(e.target.value) || 0 }))}
                      style={inputStyle} />
                  </div>
                  <div>
                    <label style={labelStyle}>{T.noticeDays || "Notice Days"}</label>
                    <input type="number" value={newType.notice_days} min={0}
                      onChange={e => setNewType(n => ({ ...n, notice_days: parseInt(e.target.value) || 0 }))}
                      style={inputStyle} />
                  </div>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 12 }}>
                  <div>
                    <label style={labelStyle}>{T.monthlyAccrual || "Monthly Accrual"}</label>
                    <input type="number" step="0.25" value={newType.monthly_accrual} min={0}
                      onChange={e => setNewType(n => ({ ...n, monthly_accrual: parseFloat(e.target.value) || 0 }))}
                      style={inputStyle} />
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, paddingTop: 24 }}>
                    <input type="checkbox" id="docReq" checked={newType.doc_required}
                      onChange={e => setNewType(n => ({ ...n, doc_required: e.target.checked }))} />
                    <label htmlFor="docReq" style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{T.documentRequired || "Document Required"}</label>
                  </div>
                </div>
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{T.monthlyLimitDays || "Max days per month (optional)"}</label>
                  <input type="number" step="0.5" min={0} max={31} value={newType.monthly_limit}
                    placeholder={T.noLimit || "No limit"}
                    onChange={e => setNewType(n => ({ ...n, monthly_limit: e.target.value }))}
                    style={inputStyle} />
                </div>
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{T.description || "Description"}</label>
                  <textarea value={newType.description}
                    onChange={e => setNewType(n => ({ ...n, description: e.target.value }))}
                    placeholder={T.leaveDescriptionPlaceholderAdmin || "Brief description of this leave type..."} rows={2}
                    style={{ ...inputStyle, resize: "vertical" }} />
                </div>
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{T.eligibility || "Eligibility"}</label>
                  <input value={newType.eligibility}
                    onChange={e => setNewType(n => ({ ...n, eligibility: e.target.value }))}
                    placeholder={T.eligibilityPlaceholder || "Who can apply for this leave?"} style={inputStyle} />
                </div>
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{T.maxDuration || "Max Duration"}</label>
                  <input value={newType.max_duration}
                    onChange={e => setNewType(n => ({ ...n, max_duration: e.target.value }))}
                    placeholder={T.maxDurationPlaceholder || "e.g. 10 days per year"} style={inputStyle} />
                </div>
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{T.requiredDocs || "Required Docs"}</label>
                  <input value={newType.required_docs}
                    onChange={e => setNewType(n => ({ ...n, required_docs: e.target.value }))}
                    placeholder={T.requiredDocsPlaceholder || "What documents are needed?"} style={inputStyle} />
                </div>
                <div style={{ marginBottom: 16 }}>
                  <label style={labelStyle}>{T.termsAndConditions || "Terms & Conditions"} <span style={{ fontWeight: 400, color: C.sub }}></span></label>
                  <textarea value={newType.terms}
                    onChange={e => setNewType(n => ({ ...n, terms: e.target.value }))}
                    placeholder={T.termsPlaceholder || "Each rule on a new line:\nMust apply 5 days in advance\nSubject to manager approval"}
                    rows={4} style={{ ...inputStyle, resize: "vertical" }} />
                </div>
                <button type="submit" disabled={typeSubmitting} style={{
                  width: "100%", padding: "12px", background: C.brand, color: "#fff",
                  border: "none", borderRadius: 8, fontWeight: 700, fontSize: 14, cursor: "pointer",
                  opacity: typeSubmitting ? 0.7 : 1,
                }}>
                  {typeSubmitting ? (T.creating || "Creating...") : (T.createLeaveType || "Create Leave Type")}
                </button>
              </form>
            </div>
          </div>
        )}

        {/* ════════════ RISK ALERTS TAB ════════════ */}
        {tab === "risk" && (
          <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, overflow: "hidden" }}>
            <div style={{ padding: "16px 20px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ fontWeight: 700, color: C.text }}>{T.riskAlertsInsuffHours || "Risk Alerts — Insufficient Hours"}</div>
                <div style={{ fontSize: 13, color: C.sub }}>{T.belowThreshold || "Employees who worked below the 8h 45m threshold"}</div>
              </div>
              <button onClick={async () => {
                await apiFetch("/risk-alert/check-all", { method: "POST" });
                fetchRisk(); showToast(T.riskCheckCompleted || "Risk check completed");
              }} style={{ padding: "9px 18px", background: C.errorBg, color: C.error, border: `1px solid ${C.errorBorder}`, borderRadius: 8, fontWeight: 600, cursor: "pointer", fontSize: 13 }}>
                ⚠️ {T.runCheckNow || "Run Check Now"}
              </button>
            </div>
            {riskHistory.length === 0 ? (
              <div style={{ textAlign: "center", padding: "48px 0", color: C.sub }}>
                <div style={{ fontSize: 40, marginBottom: 8 }}>✅</div>
                <p style={{ margin: 0 }}>{T.noRiskAlertsAll || "No risk alerts — all employees met the work threshold"}</p>
              </div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr style={{ background: C.surfaceAlt || C.bg }}>
                    {[T.employee||"Employee",T.date||"Date",T.worked||"Worked",T.required||"Required",T.emailSent||"Email Sent"].map(h => (
                      <th key={h} style={{ padding: "12px 16px", textAlign: "left", color: C.textSub || C.sub, fontWeight: 600, borderBottom: `1px solid ${C.border}` }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {riskHistory.map(a => (
                    <tr key={a.id} style={{ borderBottom: `1px solid ${C.border}` }}>
                      <td style={{ padding: "12px 16px", fontWeight: 600, color: C.text }}>{a.user_name}</td>
                      <td style={{ padding: "12px 16px", color: C.sub }}>{new Date(a.alert_date).toLocaleDateString("en-IN")}</td>
                      <td style={{ padding: "12px 16px", color: C.error, fontWeight: 700 }}>{a.worked_display}</td>
                      <td style={{ padding: "12px 16px", color: C.sub }}>{a.threshold_display}</td>
                      <td style={{ padding: "12px 16px" }}>
                        <span style={{ fontSize: 12, fontWeight: 600, color: a.email_sent ? "#2F8F5C" : "#94A3B8" }}>
                          {a.email_sent ? `✅ ${T.sent || "Sent"}` : `⏳ ${T.pendingStatus || "Pending"}`}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {/* ════════════ DETAIL MODAL — Leave Ledger ════════════ */}
        {selected && (
          <LeaveLedgerModal C={C} T={T} isDark={isDark} request={selected}
            typeIcon={LEAVE_TYPE_ICONS[selected.leave_type] || "📋"}
            statusBadge={<Badge status={selected.status} />}
            balance={reviewBalance} history={reviewHistory} workedEvidence={workedEvidence}
            canAct={leaveApprovals.interactive}
            comment={comment} commentError={commentError}
            onCommentChange={(v) => { setComment(v); setCommentError(""); }}
            actioning={actioning === selected.id}
            onAction={(status, cmt) => updateStatus(selected.id, status, cmt)}
            attachmentError={attachmentError}
            onOpenAttachment={() => {
              // Reviewers read the document through the authenticated
              // GET /leave/{id}/attachment, which verifies this admin is in the
              // employee's organization (never the unauthenticated static mount).
              setAttachmentError(null);
              openLeaveAttachment(selected.id).catch(err => setAttachmentError(err.message));
            }}
            onClose={() => { setSelected(null); setCommentError(""); }} />
        )}

        {/* ════════════ EDIT LEAVE MODAL ════════════ */}
        {editLeave && (
          <div style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.55)",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        zIndex: 9000, padding: 20 }}
               onClick={() => !savingEdit && setEditLeave(null)}>
            <div onClick={e => e.stopPropagation()}
                 style={{ width: 520, maxWidth: "100%", background: C.surface,
                          borderRadius: 14, padding: 26,
                          boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}>
              <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
                {T.editLeaveRequest || "Edit Leave Request"}
              </p>
              <p style={{ margin: "4px 0 18px", fontSize: 12.5, color: C.sub, lineHeight: 1.5 }}>
                {editLeave.user_name || editLeave.user_email}
                {" · "}
                <span style={{ fontWeight: 600 }}>{editLeave.status}</span>
              </p>

              <label style={labelStyle}>{T.leaveType || "Leave type"}</label>
              <select value={editForm.leave_type}
                      onChange={e => setEditForm(p => ({ ...p, leave_type: e.target.value }))}
                      style={inputStyle}>
                {/* Always include the current value so admins editing a
                    leave whose type was later disabled don't see it
                    blanked out. */}
                {!leaveTypes.some(lt => lt.leave_type === editForm.leave_type) && editForm.leave_type && (
                  <option value={editForm.leave_type}>{editForm.leave_type}</option>
                )}
                {leaveTypes.map(lt => (
                  <option key={lt.leave_type} value={lt.leave_type}>{lt.leave_type}</option>
                ))}
              </select>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
                <div>
                  <label style={labelStyle}>{T.fromDate || "From date"}</label>
                  <input type="date" value={editForm.from_date}
                         onChange={e => setEditForm(p => ({ ...p, from_date: e.target.value }))}
                         style={inputStyle} />
                </div>
                <div>
                  <label style={labelStyle}>{T.toDate || "To date"}</label>
                  <input type="date" value={editForm.to_date}
                         onChange={e => setEditForm(p => ({ ...p, to_date: e.target.value }))}
                         style={inputStyle} />
                </div>
              </div>

              <label style={{ ...labelStyle, marginTop: 12 }}>{T.description || "Description"}</label>
              <textarea value={editForm.description}
                        onChange={e => setEditForm(p => ({ ...p, description: e.target.value }))}
                        rows={3}
                        style={{ ...inputStyle, resize: "vertical", fontFamily: "inherit" }} />

              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginTop: 18 }}>
                <button onClick={() => deleteLeave(editLeave)}
                        disabled={savingEdit || actioning === editLeave.id}
                        style={btnStyle("#FEF2F2", "#B91C1C")}>
                  🗑 {T.deleteLeave || "Delete leave"}
                </button>
                <div style={{ display: "flex", gap: 10 }}>
                  <button onClick={() => setEditLeave(null)} disabled={savingEdit}
                          style={btnStyle("#F1F5F9", "#475467")}>
                    {T.cancel || "Cancel"}
                  </button>
                  <button onClick={saveEditLeave} disabled={savingEdit}
                          style={{ ...btnStyle("#0F172A", "#fff"), background: "#0F172A", color: "#fff" }}>
                    {savingEdit ? (T.saving || "Saving...") : (T.save || "Save")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ════════════ SWAP DATES MODAL ════════════ */}
        {swapLeave && (
          <div style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.55)",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        zIndex: 9001, padding: 20 }}
               onClick={() => !savingSwap && setSwapLeave(null)}>
            <div onClick={e => e.stopPropagation()}
                 style={{ width: 460, maxWidth: "100%", background: C.surface,
                          borderRadius: 14, padding: 24, border: `1px solid ${C.border}`,
                          boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                <div style={{ width: 36, height: 36, borderRadius: "50%", background: "#ECFDF5", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18 }}>🔁</div>
                <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
                  {T.swapLeaveDates || "Swap leave dates"}
                </p>
              </div>

              <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 12px", fontSize: 13, color: C.text, marginBottom: 14, lineHeight: 1.5 }}>
                <div><strong>{swapLeave.name || swapLeave.user_name || swapLeave.email}</strong> · {swapLeave.leave_type}</div>
                <div style={{ color: C.sub, fontSize: 12, marginTop: 2 }}>
                  {T.currently || "Currently"}: {String(swapLeave.from_date).slice(0, 10)} → {String(swapLeave.to_date).slice(0, 10)}
                </div>
              </div>

              <p style={{ margin: "0 0 14px", fontSize: 12.5, color: C.sub, lineHeight: 1.5 }}>
                {T.swapDatesHint || "Move this approved leave to new dates. The change is recorded in the audit log."}
              </p>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div>
                  <label style={labelStyle}>{T.fromDate || "From date"}</label>
                  <input type="date" value={swapForm.from_date}
                         onChange={e => setSwapForm(p => ({ ...p, from_date: e.target.value }))}
                         style={inputStyle} />
                </div>
                <div>
                  <label style={labelStyle}>{T.toDate || "To date"}</label>
                  <input type="date" value={swapForm.to_date}
                         onChange={e => setSwapForm(p => ({ ...p, to_date: e.target.value }))}
                         style={inputStyle} />
                </div>
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 20 }}>
                <button onClick={() => setSwapLeave(null)} disabled={savingSwap}
                        style={btnStyle("#F1F5F9", "#475467")}>
                  {T.cancel || "Cancel"}
                </button>
                <button onClick={saveSwapDates} disabled={savingSwap}
                        style={{ ...btnStyle("#047857", "#fff"), background: "#047857", color: "#fff",
                                 opacity: savingSwap ? 0.55 : 1 }}>
                  {savingSwap ? (T.saving || "Saving...") : (T.saveDates || "Save dates")}
                </button>
              </div>
            </div>
          </div>
        )}

        </div>
        </div>
      </main>
    </div>
  );
}

const btnStyle = (bg, color) => ({
  padding: "5px 12px", background: bg, color, border: `1px solid ${color}33`,
  borderRadius: 6, cursor: "pointer", fontWeight: 600, fontSize: 12, whiteSpace: "nowrap",
});
