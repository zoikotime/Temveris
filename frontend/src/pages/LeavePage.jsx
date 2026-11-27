import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE } from "../config";
import API from "../api/axios";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import TopNavbar from "../components/TopNavbar";
import DocumentUpload, { validateDocument } from "../components/leave/DocumentUpload";
import { openLeaveAttachment } from "../utils/leaveAttachment";

const TOKEN = () => localStorage.getItem("token");

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

const STATUS_COLORS = {
  submitted:    { bg: "#EFF6FF", color: "#1D4ED8", border: "#93C5FD", label: "Submitted" },
  under_review: { bg: "#FFF7ED", color: "#C2410C", border: "#FED7AA", label: "Under Review" },
  approved:     { bg: "#F0FDF4", color: "#2F8F5C", border: "#A8E0BD", label: "Approved" },
  rejected:     { bg: "#FEF2F2", color: "#B91C1C", border: "#FCA5A5", label: "Rejected" },
  locked_for_payroll: { bg: "#F5F3FF", color: "#6D28D9", border: "#C4B5FD", label: "Locked for Payroll" },
};

const Badge = ({ status }) => {
  const cfg = STATUS_COLORS[status] || { bg: "#F9FAFB", color: "#374151", border: "#E5E7EB", label: status };
  return (
    <span style={{
      background: cfg.bg, color: cfg.color, border: `1px solid ${cfg.border}`,
      borderRadius: 20, padding: "3px 12px", fontSize: 12, fontWeight: 600,
    }}>{cfg.label}</span>
  );
};

const labelStyle = { display: "block", fontSize: 13, fontWeight: 600, color: "#374151", marginBottom: 6 };
const inputStyle = {
  width: "100%", padding: "10px 14px", border: "1px solid #D1D5DB", borderRadius: 8,
  fontSize: 14, color: "#101828", background: "#fff", boxSizing: "border-box",
  outline: "none", fontFamily: "inherit",
};

export default function LeavePage({ embedded = false }) {
  const { T } = useLanguage();
  const { C } = useTheme();
  const navigate = useNavigate();
  const role = (localStorage.getItem("role") || "").toLowerCase();
  const isAdmin = role === "admin" || role === "hr";
  const [tab, setTab]             = useState("apply");
  const [balance, setBalance]     = useState({});
  const [myLeaves, setMyLeaves]   = useState([]);
  const [leaveTypes, setLeaveTypes] = useState([]);
  const [policies, setPolicies]   = useState([]);
  const [loading, setLoading]     = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess]     = useState(null);
  const [error, setError]         = useState(null);
  const [validation, setValidation] = useState({ errors: [], flags: [], auto_lwp: false });
  const [expandedType, setExpandedType] = useState(null);
  // Edit / Delete state — owners can edit or delete their own leave
  // request only while the backend still considers it editable
  // (status in submitted / under_review). The same permission rules
  // are enforced server-side, so this is purely a UI affordance.
  const [editLeave, setEditLeave]   = useState(null);
  const [editForm, setEditForm]     = useState({ leave_type: "", from_date: "", to_date: "", description: "" });
  const [savingEdit, setSavingEdit] = useState(false);
  const [busyId, setBusyId]         = useState(null);
  // Delete-with-reason modal. Leaves are a payroll-linked record, so the
  // backend now requires a reason when an owner deletes one. The reason
  // lives in the audit log so HR / managers can reconcile a withdrawn day.
  const [deleteLeaveTarget, setDeleteLeaveTarget] = useState(null);
  const [deleteReason, setDeleteReason]           = useState("");
  const [deleting, setDeleting]                   = useState(false);
  // Swap-dates quick action. Employees can't touch an approved leave, but
  // admins/HR often need to move one (e.g. the person took the day a day
  // earlier than booked). This reuses PUT /leave/{id}, which the backend
  // allows for admins at any status; the change is captured in the audit log.
  const [swapLeave, setSwapLeave]   = useState(null);
  const [swapForm, setSwapForm]     = useState({ from_date: "", to_date: "" });
  const [savingSwap, setSavingSwap] = useState(false);

  const [form, setForm] = useState({
    // Empty until GET /leave/types lands; an effect below selects this
              // organization's first ACTIVE type. Defaulting to "Annual Leave"
              // pre-selected a type a renamed catalogue may not contain.
    leave_type: "",
    from_date: "",
    to_date: "",
    description: "",
    reason: "",
    is_half_day: false,
    half_day_period: "first_half",   // "first_half" | "second_half"
    // Compensatory Leave only — the dates the employee already worked
    // (overtime / weekend / public-holiday) that this comp-off claims back.
    // Each entry is a "YYYY-MM-DD" string; empty strings are placeholder rows.
    worked_dates: [],
  });
  const [attachment, setAttachment] = useState(null);
  // Set on the first Submit press. The dropzone keeps its "required" message
  // hidden until then, so selecting Sick Leave doesn't immediately paint the
  // form red before the employee has done anything wrong.
  const [submitAttempted, setSubmitAttempted] = useState(false);
  // {id, message} — why one history row's document failed to open (deleted
  // from storage, pop-up blocked, expired session). Scoped to the row so it
  // renders in the History tab, which has no shared error banner.
  const [docOpenError, setDocOpenError] = useState(null);
  // Leave eligibility — populated from /leave/eligibility/me. The form
  // disables itself when `eligibility.eligible === false`; we still let
  // the user see leave balances and history so they understand why.
  const [eligibility, setEligibility] = useState(null);

  const fetchAll = useCallback(() => {
    apiFetch("/leave/balance").then(d => setBalance(d || {})).catch(() => {});
    apiFetch("/leave/my-requests").then(d => setMyLeaves(Array.isArray(d) ? d : [])).catch(() => {});
    apiFetch("/leave/types").then(d => setLeaveTypes(Array.isArray(d) ? d : [])).catch(() => {});
    apiFetch("/leave/policies").then(d => setPolicies(Array.isArray(d) ? d : [])).catch(() => {});
    apiFetch("/leave/eligibility/me").then(d => setEligibility(d || null)).catch(() => setEligibility(null));
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  // Live validation
  useEffect(() => {
    if (form.from_date && form.to_date && form.leave_type) {
      // Mirror the apply handler's half-day resolution so the live balance
      // warning matches the eventual submit (the backend re-derives it too).
      const singleDay = form.from_date === form.to_date;
      const halfDayHint = form.leave_type === "Half Day Leave" || (!!form.is_half_day && singleDay);
      apiFetch("/leave/validate", {
        method: "POST",
        body: JSON.stringify({
          leave_type: form.leave_type,
          from_date: form.from_date,
          to_date: form.to_date,
          has_attachment: !!attachment,
          is_half_day: halfDayHint,
        }),
      }).then(d => setValidation(d || { errors: [], flags: [], auto_lwp: false })).catch(() => {});
    } else {
      setValidation({ errors: [], flags: [], auto_lwp: false });
    }
  }, [form.leave_type, form.from_date, form.to_date, form.is_half_day, attachment]);

  const selectedTypeInfo = leaveTypes.find(t => t.leave_type === form.leave_type);
  const activeTypeNames  = leaveTypes.map(t => t.leave_type);
  // Select this organization's first configured type once the catalogue
  // arrives, so the form never sits on a type the tenant does not offer.
  useEffect(() => {
    if (!form.leave_type && activeTypeNames.length > 0) {
      setForm(f => ({ ...f, leave_type: activeTypeNames[0] }));
    }
  }, [activeTypeNames.join("|")]);   // eslint-disable-line react-hooks/exhaustive-deps
  // The new-joiner waiting period only restricts PAID, ACCRUED leave. Which
  // types are exempt comes from the server (`waiting_exempt` on /leave/types)
  // — it was a literal ["Compensatory Leave", "Unpaid Leave"] here, so a
  // workspace that renamed either type had its new joiners blocked from leave
  // the backend was happy to grant.
  const eligibilityBlocks = !!(eligibility && eligibility.eligible === false)
    && !selectedTypeInfo?.waiting_exempt;
  // The dedicated half-day type is identified by its POLICY flag. The old
  // `|| form.leave_type === "Half Day Leave"` fallback fired on the name even
  // when the server said the flag was false.
  const isHalfDayOnlyType = !!selectedTypeInfo?.half_day_only;

  const isSingleDay   = !!form.from_date && form.from_date === form.to_date;
  // Two paths into half-day mode: the dedicated leave type (forced server- and
  // client-side) OR the per-request checkbox on any other single-day request.
  const halfDayActive = isHalfDayOnlyType || (form.is_half_day && isSingleDay);

  // ── Compensatory Leave: "dates already worked" ──────────────────────────
  // Comp leave is earned by working overtime / weekends / public holidays, so
  // we ask the employee to list those dates as the justification. The field
  // only appears for this type; `compWorkedMissing` blocks Submit until at
  // least one non-empty date is entered (mirrors the backend's hard 400).
  const isCompLeave = form.leave_type === "Compensatory Leave";
  const cleanWorkedDates = (form.worked_dates || []).map(d => (d || "").trim()).filter(Boolean);
  const compWorkedMissing = isCompLeave && cleanWorkedDates.length === 0;
  const todayStr = new Date().toISOString().split("T")[0];

  const addWorkedDate    = () => setForm(f => ({ ...f, worked_dates: [...(f.worked_dates || []), ""] }));
  const updateWorkedDate = (i, v) => setForm(f => ({ ...f, worked_dates: (f.worked_dates || []).map((d, idx) => idx === i ? v : d) }));
  const removeWorkedDate = (i) => setForm(f => {
    const next = (f.worked_dates || []).filter((_, idx) => idx !== i);
    // Always keep one row visible while the comp-leave form is open.
    return { ...f, worked_dates: next.length ? next : [""] };
  });

  // Seed a single empty row the moment Compensatory Leave is picked so the
  // employee sees the date entry immediately; clear it again when they switch
  // to a type that doesn't use worked dates so stale entries aren't submitted.
  useEffect(() => {
    if (isCompLeave) {
      setForm(f => (f.worked_dates && f.worked_dates.length > 0) ? f : { ...f, worked_dates: [""] });
    } else {
      setForm(f => (f.worked_dates && f.worked_dates.length === 0) ? f : { ...f, worked_dates: [] });
    }
  }, [isCompLeave]);

  const leaveDays = () => {
    if (!form.from_date) return 0;
    if (halfDayActive) return 0.5;
    if (!form.to_date) return 0;
    const diff = (new Date(form.to_date) - new Date(form.from_date)) / 86400000;
    return diff < 0 ? 0 : Math.floor(diff) + 1;
  };

  // When the user picks "Half Day Leave", lock To Date to From Date so the
  // backend always receives a single-day range. Done as an effect (not in
  // setForm at change time) so it also fires when the user changes From Date
  // *after* selecting the type.
  useEffect(() => {
    if (!isHalfDayOnlyType) return;
    setForm(f => {
      const next = { ...f, is_half_day: true };
      if (f.from_date && f.to_date !== f.from_date) next.to_date = f.from_date;
      return next;
    });
  }, [isHalfDayOnlyType, form.from_date]);

  const openEditLeave = (r) => {
    const toDateInput = (v) => v ? String(v).slice(0, 10) : "";
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
    if (!editForm.leave_type || !editForm.from_date || !editForm.to_date) {
      setError("Type, from date and to date are required");
      return;
    }
    if (editForm.to_date < editForm.from_date) {
      setError("End date must be on or after start date");
      return;
    }
    setSavingEdit(true);
    setError(null); setSuccess(null);
    try {
      const data = await apiFetch(`/leave/${editLeave.id}`, {
        method: "PUT",
        body: JSON.stringify({
          leave_type:  editForm.leave_type,
          from_date:   editForm.from_date,
          to_date:     editForm.to_date,
          description: editForm.description,
        }),
      });
      if (data.detail) {
        setError(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      } else {
        setSuccess("Leave updated");
        setEditLeave(null);
        // Refresh my-requests so the row reflects the new values.
        apiFetch("/leave/my-requests").then(d => setMyLeaves(Array.isArray(d) ? d : [])).catch(() => {});
      }
    } catch { setError("Network error"); }
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
      setError("Both dates are required");
      return;
    }
    if (swapForm.to_date < swapForm.from_date) {
      setError("End date must be on or after start date");
      return;
    }
    setSavingSwap(true);
    setError(null); setSuccess(null);
    try {
      const data = await apiFetch(`/leave/${swapLeave.id}`, {
        method: "PUT",
        body: JSON.stringify({ from_date: swapForm.from_date, to_date: swapForm.to_date }),
      });
      if (data.detail) {
        setError(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      } else {
        setSuccess("Leave dates updated");
        setSwapLeave(null);
        apiFetch("/leave/my-requests").then(d => setMyLeaves(Array.isArray(d) ? d : [])).catch(() => {});
      }
    } catch { setError("Network error"); }
    setSavingSwap(false);
  };

  // Step 1 — open the reason-capture modal.
  const deleteLeave = (r) => {
    setDeleteLeaveTarget(r);
    setDeleteReason("");
  };

  // Step 2 — modal confirms; submit DELETE with the captured reason.
  const confirmDeleteLeave = async () => {
    const r = deleteLeaveTarget;
    if (!r) return;
    const reason = deleteReason.trim();
    if (!reason) { setError(T.leavePageDeleteReasonRequired || "Please tell us why you're deleting this leave."); return; }
    setDeleting(true);
    setBusyId(r.id);
    setError(null); setSuccess(null);
    try {
      const url = `${API_BASE}/leave/${r.id}?reason=${encodeURIComponent(reason)}`;
      const res = await fetch(url, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${TOKEN()}` },
      });
      if (res.ok) {
        setSuccess(T.leavePageLeaveDeleted || "Leave deleted");
        setMyLeaves(prev => prev.filter(x => x.id !== r.id));
        setDeleteLeaveTarget(null);
        setDeleteReason("");
      } else {
        let msg = T.leavePageCouldNotDelete || "Could not delete";
        try { const d = await res.json(); msg = d.detail || msg; } catch {}
        setError(typeof msg === "string" ? msg : JSON.stringify(msg));
      }
    } catch { setError("Network error"); }
    setBusyId(null);
    setDeleting(false);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitAttempted(true);
    if (!form.description.trim()) { setError(T.leavePageDescriptionRequired || "Description is required"); return; }
    if (isCompLeave && cleanWorkedDates.length === 0) {
      setError(T.workedDatesRequired || "Please add at least one date you already worked to claim Compensatory Leave.");
      return;
    }
    // Document gate. The button is already disabled via `hasHardErrors`, but a
    // keyboard submit or a stale render can still reach here, and the backend
    // rejects the same case with the same wording — so fail here rather than
    // spending a round trip to be told the same thing.
    if (docIsMandatory && !attachment) {
      setError(T.documentRequiredError || "Supporting document is required for this leave type.");
      return;
    }
    // Re-check the file against the type currently selected: the employee may
    // have attached a 9 MB PDF, then switched to a type with a smaller cap.
    if (attachment) {
      const problem = validateDocument(attachment, {
        accept:  selectedTypeInfo?.doc_accept,
        formats: selectedTypeInfo?.doc_formats,
        maxMb:   selectedTypeInfo?.doc_max_mb,
      });
      if (problem) { setError(problem); return; }
    }
    setSubmitting(true); setError(null); setSuccess(null);

    const fd = new FormData();
    fd.append("leave_type",  form.leave_type);
    fd.append("from_date",   form.from_date);
    fd.append("to_date",     form.to_date);
    fd.append("description", form.description);
    fd.append("reason",      form.reason);
    fd.append("is_half_day", halfDayActive ? "true" : "false");
    if (halfDayActive) fd.append("half_day_period", form.half_day_period);
    if (isCompLeave) fd.append("worked_dates", JSON.stringify(cleanWorkedDates));
    if (attachment) fd.append("attachment", attachment);

    try {
      const res = await fetch(`${API_BASE}/leave/apply`, {
        method: "POST",
        headers: { Authorization: `Bearer ${TOKEN()}` },
        body: fd,
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setSuccess(data.message || T.leavePageSubmittedSuccess || "Leave request submitted successfully!");
        setForm({ leave_type: activeTypeNames[0] || "", from_date: "", to_date: "", description: "", reason: "", is_half_day: false, half_day_period: "first_half", worked_dates: [] });
        setAttachment(null);
        setSubmitAttempted(false);
        fetchAll();
      } else {
        const msg = data.detail?.errors?.join(", ") || data.detail || T.leavePageSubmitFailed || "Failed to submit leave request";
        setError(typeof msg === "string" ? msg : JSON.stringify(msg));
      }
    } catch { setError("Network error. Please try again."); }
    setSubmitting(false);
  };

  // Evidence gate — block Submit until the required document is attached, for
  // any type this ORGANIZATION flagged `evidence_gate`.
  //
  // This carried `&& form.leave_type === "Medical Leave"`, a type name that
  // exists in no catalogue, so the gate was dead on the client exactly as it
  // was on the server. The server now enforces it (leave/router.py), and this
  // stops the employee filling in a form that is going to be rejected.
  // `doc_required` alone is informational and deliberately does NOT block.
  const evidenceMissing = !!selectedTypeInfo?.evidence_gate && !attachment;

  // ── Supporting document: when to SHOW vs when to BLOCK ────────────────────
  // Two different questions, two different flags — the same split the backend
  // makes in _run_validation:
  //   doc_required  → show the dropzone. This is the flag behind the
  //                   "📎 Document Required" badge, so the field appears for
  //                   exactly the types that advertise one.
  //   evidence_gate → also block Submit until a file is attached.
  // Compensatory Leave is excluded for the same reason the badge excludes it:
  // its evidence is the worked-date list above, not an upload.
  const showDocUpload   = !!selectedTypeInfo?.doc_required && !isCompLeave;
  const docIsMandatory  = showDocUpload && !!selectedTypeInfo?.evidence_gate;

  // Switching to a type that takes no document drops the attached file rather
  // than silently uploading it against the new type — the dropzone is hidden at
  // that point, so the employee has no way to see it is still attached.
  // `submitAttempted` resets with it so the new type starts clean.
  useEffect(() => {
    if (!showDocUpload && attachment) setAttachment(null);
    setSubmitAttempted(false);
  }, [form.leave_type]);   // eslint-disable-line react-hooks/exhaustive-deps

  const hasHardErrors =
    evidenceMissing ||
    compWorkedMissing ||
    validation.errors.some(e => !e.toLowerCase().includes("balance"));
  const currentBalance = balance[form.leave_type];
  const hasNoAnnualCap = (item) => !!(item?.no_annual_limit || (item?.is_earned && Number(item?.annual_limit ?? item?.total ?? 0) === 0));
  const showNumericBalance = (item) => !!item && !item.is_unlimited && !item.is_earned && !hasNoAnnualCap(item);
  const limitLabelFor = (item) => {
    if (!item) return "";
    if (hasNoAnnualCap(item)) return T.noAnnualCap || "No annual cap";
    if (item.is_unlimited) return T.unlimited || T.leavePageUnlimited || "Unlimited";
    if (item.is_earned) return T.leavePageEarnedFromOvertime || "Earned from worked dates";
    return `${item.annual_limit ?? item.total ?? 0} ${T.daysPerYear || "days/yr"}`;
  };

  const pageContent = (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", background: C.bg, fontFamily: "'Segoe UI', Arial, sans-serif" }}>
      {/* When embedded inside Dashboard.jsx the parent shell already paints
          a page header (the "Leave & Requests · Live" bar with theme/lang
          controls), so a second TopNavbar here renders as a duplicate row.
          Standalone /leave route keeps it. */}
      {!embedded && (
        <TopNavbar
          title={T.leaveManagement || "Leave Management"}
          subtitle={T.reviewLeave || "Apply for leave, track your history, check balances and view all leave type policies"}
          onBack={isAdmin ? null : () => navigate("/dashboard")}
        />
      )}
      <div style={{ flex: 1, overflowY: "auto", padding: "32px 24px" }}>
      <div style={{ maxWidth: 1000, margin: "0 auto" }}>

        {/* Tabs */}
        <div style={{ display: "flex", gap: 4, marginBottom: 24, background: "#E5E7EB", borderRadius: 10, padding: 4, width: "fit-content", flexWrap: "wrap" }}>
          {[
            ["apply",T.apply_leave||"Apply Leave"],
            ["history",T.leaveHistory||"My History"],
            ["balance",T.leave_balance||"My Balance"],
            ["types",T.leaveTypes||"Leave Types"],
            ["policies",T.leavePolicies||"Policies"],
          ].map(([k, l]) => (
            <button key={k} onClick={() => setTab(k)} style={{
              padding: "8px 20px", borderRadius: 8, border: "none", cursor: "pointer", fontWeight: 600, fontSize: 14,
              background: tab === k ? C.brand : "transparent",
              color: tab === k ? "#fff" : C.sub,
              transition: "all 0.15s",
            }}>{l}</button>
          ))}
        </div>

        {/* ════════════ APPLY TAB ════════════ */}
        {tab === "apply" && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 20 }}>
            {/* Form */}
            <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 28 }}>
              <h2 style={{ margin: "0 0 20px", fontSize: 18, fontWeight: 700, color: C.text }}>{T.apply_leave||"Apply for Leave"}</h2>

              {/* Eligibility banner — shows when the worker hasn't yet
                  completed the tenant's waiting period after their start
                  date. The backend hard-blocks /leave/apply with 403 if
                  the rule fails, but we surface it here so the user
                  doesn't waste time filling the form. */}
              {eligibilityBlocks && (
                <div style={{
                  background: "#FFFBEB", border: "1px solid #FDE68A",
                  borderRadius: 10, padding: "14px 16px", marginBottom: 20,
                  display: "flex", gap: 12, alignItems: "flex-start",
                }}>
                  <div style={{ fontSize: 22, lineHeight: 1 }}>🔒</div>
                  <div style={{ flex: 1 }}>
                    <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: "#92400E" }}>
                      {T.notYetEligibleForLeave || "Paid leave not yet available"}
                    </p>
                    <p style={{ margin: "4px 0 0", fontSize: 12, color: "#78350F", lineHeight: 1.6 }}>
                      You become eligible on{" "}
                      <strong>{eligibility.eligible_from}</strong>
                      {" "}
                      (after {eligibility.waiting_period_months} months from your start date
                      {" "}
                      {eligibility.start_date && <strong>{eligibility.start_date}</strong>}).
                      {eligibility.days_remaining > 0 && (
                        <> — <strong>{eligibility.days_remaining} day{eligibility.days_remaining === 1 ? "" : "s"}</strong> to go.</>
                      )}
                    </p>
                  </div>
                </div>
              )}

              {success && (
                <div style={{ background: C.successBg, border: `1px solid ${C.successBorder}`, borderRadius: 8, padding: "14px 16px", marginBottom: 20 }}>
                  <p style={{ margin: 0, color: C.success, fontWeight: 600 }}>✅ {success}</p>
                </div>
              )}
              {error && (
                <div style={{ background: C.errorBg, border: `1px solid ${C.errorBorder}`, borderRadius: 8, padding: "14px 16px", marginBottom: 20 }}>
                  <p style={{ margin: 0, color: C.error, fontWeight: 600 }}>❌ {error}</p>
                </div>
              )}

              {/* Validation warnings */}
              {validation.errors.length > 0 && (
                <div style={{ background: C.warnBg, border: `1px solid ${C.warnBorder}`, borderRadius: 8, padding: "12px 16px", marginBottom: 16 }}>
                  {validation.errors.map((e, i) => (
                    <p key={i} style={{ margin: "2px 0", color: C.warn, fontSize: 13 }}>⚠️ {e}</p>
                  ))}
                  {validation.auto_lwp && (
                    <p style={{ margin: "4px 0 0", color: "#7C3AED", fontSize: 12, fontWeight: 600 }}>
                      {T.excessDaysLWP || "Note: Excess days will be treated as Unpaid Leave (LWP) automatically."}
                    </p>
                  )}
                </div>
              )}

              <form onSubmit={handleSubmit}>
                {/* Leave Type */}
                <div style={{ marginBottom: 16 }}>
                  <label style={labelStyle}>{T.leave_type||"Leave Type"} *</label>
                  <select value={form.leave_type}
                    onChange={e => setForm(f => ({ ...f, leave_type: e.target.value }))}
                    style={inputStyle}>
                    {/* THIS organization's configured types, from
                        GET /leave/types. There is deliberately no hard-coded
                        fallback list: offering a tenant eleven types it does
                        not have — and that /leave/apply would reject — is
                        worse than showing an empty picker while the fetch
                        completes or retries. */}
                    {activeTypeNames.length === 0 && (
                      <option value="">{T.loading || "Loading leave types…"}</option>
                    )}
                    {activeTypeNames.map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>

                {/* Leave type info pill */}
                {selectedTypeInfo && (
                  <div style={{ background: "#EFF6FF", border: "1px solid #93C5FD", borderRadius: 8, padding: "10px 14px", marginBottom: 16 }}>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#1D4ED8", marginBottom: 4 }}>
                      {selectedTypeInfo.icon} {selectedTypeInfo.leave_type}
                    </div>
                    <div style={{ fontSize: 12, color: "#1E40AF" }}>{selectedTypeInfo.description}</div>
                    <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 11, color: "#3B82F6", flexWrap: "wrap" }}>
                      <span>⏰ {T.notice || "Notice"}: {selectedTypeInfo.notice_days === 0 ? (T.noNotice || "None") : `${selectedTypeInfo.notice_days} ${T.days || "day(s)"}`}</span>
                      <span>📅 {T.max || "Max"}: {limitLabelFor(selectedTypeInfo)}</span>
                      {/* Same condition as the dropzone below (`showDocUpload`)
                          so the badge can never promise a document field the
                          form does not render. */}
                      {showDocUpload && <span style={{ color: "#D97706", fontWeight: 600 }}>📎 {T.docRequired || "Document Required"}</span>}
                    </div>
                  </div>
                )}

                {/* ── Dates already worked (Compensatory Leave only) ──────────
                    The employee lists the overtime / weekend / public-holiday
                    days they already worked that this comp-off claims back.
                    At least one is required; future dates are blocked at the
                    input (max=today) and again on the backend. */}
                {isCompLeave && (
                  <div style={{
                    border: `1px solid ${compWorkedMissing ? "#FCA5A5" : C.brand}`,
                    borderRadius: 8, padding: "14px 16px", marginBottom: 16,
                    background: compWorkedMissing ? "#FEF2F2" : C.brandBg,
                  }}>
                    <p style={{ margin: "0 0 4px", fontSize: 13, fontWeight: 700, color: C.brand, display: "flex", alignItems: "center", gap: 8 }}>
                      🛠️ {T.workedDatesLabel || "Dates you already worked"} *
                    </p>
                    <p style={{ margin: "0 0 10px", fontSize: 12, color: C.textSub }}>
                      {T.workedDatesHint || "Add each overtime / weekend / public-holiday date you worked to earn this compensatory day off. HR will verify these."}
                    </p>
                    {(form.worked_dates && form.worked_dates.length ? form.worked_dates : [""]).map((d, i) => (
                      <div key={i} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                        <input type="date" value={d} max={todayStr}
                          onChange={e => updateWorkedDate(i, e.target.value)}
                          style={{ ...inputStyle, marginBottom: 0 }} />
                        <button type="button" onClick={() => removeWorkedDate(i)}
                          title={T.remove || "Remove"}
                          disabled={(form.worked_dates || []).length <= 1}
                          style={{
                            flexShrink: 0, width: 38, height: 38, borderRadius: 6,
                            border: `1px solid ${C.border}`, background: C.surface,
                            color: C.sub, fontSize: 18, lineHeight: 1,
                            cursor: (form.worked_dates || []).length <= 1 ? "not-allowed" : "pointer",
                            opacity: (form.worked_dates || []).length <= 1 ? 0.4 : 1,
                          }}>×</button>
                      </div>
                    ))}
                    <button type="button" onClick={addWorkedDate}
                      style={{
                        marginTop: 2, background: "none", border: `1px dashed ${C.brand}`,
                        color: C.brand, borderRadius: 6, padding: "8px 14px",
                        fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
                      }}>
                      + {T.addWorkedDate || "Add another date"}
                    </button>
                  </div>
                )}

                {/* Date range — collapses to a single field for Half Day Leave */}
                <div style={{
                  display: "grid",
                  gridTemplateColumns: isHalfDayOnlyType ? "1fr" : "1fr 1fr",
                  gap: 12, marginBottom: 16,
                }}>
                  <div>
                    <label style={labelStyle}>
                      {isHalfDayOnlyType ? (T.date || "Date") : (T.from || "From")} *
                    </label>
                    <input type="date" value={form.from_date}
                      min={new Date().toISOString().split("T")[0]}
                      onChange={e => setForm(f => ({
                        ...f,
                        from_date: e.target.value,
                        // Keep to_date in lock-step for the half-day type so a
                        // late edit to from_date doesn't desync the range.
                        to_date: isHalfDayOnlyType ? e.target.value : f.to_date,
                      }))}
                      style={inputStyle} required />
                  </div>
                  {!isHalfDayOnlyType && (
                    <div>
                      <label style={labelStyle}>{T.to || "To"} *</label>
                      <input type="date" value={form.to_date}
                        min={form.from_date || new Date().toISOString().split("T")[0]}
                        onChange={e => setForm(f => ({ ...f, to_date: e.target.value }))}
                        style={inputStyle} required />
                    </div>
                  )}
                </div>

                {/* ── Half-period selector ────────────────────────────────────
                    Three render paths:
                    1. Dedicated "Half Day Leave" type → always show the period
                       picker, no toggle (the type IS the toggle).
                    2. Single-day request on another type → show the optional
                       "Apply as half day" checkbox; period picker appears when
                       the checkbox is on.
                    3. Multi-day or no date selected → render nothing.        */}
                {isHalfDayOnlyType ? (
                  <div style={{
                    border: `1px solid ${C.brand}`,
                    borderRadius: 8, padding: "14px 16px", marginBottom: 16,
                    background: C.brandBg,
                  }}>
                    <p style={{
                      margin: "0 0 10px", fontSize: 13, fontWeight: 700, color: C.brand,
                      display: "flex", alignItems: "center", gap: 8,
                    }}>
                      🌗 {T.selectHalf || "Select Half"} *
                    </p>
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                      {[
                        { v: "first_half",  label: T.morningHalf || "Morning Half" },
                        { v: "second_half", label: T.eveningHalf || "Evening Half" },
                      ].map(opt => {
                        const active = form.half_day_period === opt.v;
                        return (
                          <label key={opt.v} style={{
                            flex: 1, minWidth: 200,
                            padding: "10px 12px", borderRadius: 6,
                            border: `2px solid ${active ? C.brand : C.border}`,
                            background: active ? C.surface : "#fff",
                            cursor: "pointer",
                            display: "flex", alignItems: "center", gap: 8,
                            fontSize: 13, fontWeight: active ? 700 : 500,
                            color: active ? C.brand : C.text,
                            fontFamily: "inherit",
                          }}>
                            <input type="radio" name="half_day_period" value={opt.v}
                              checked={active}
                              onChange={() => setForm(f => ({ ...f, half_day_period: opt.v }))}
                              style={{ accentColor: C.brand }} />
                            {opt.label}
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ) : isSingleDay && (
                  <div style={{
                    border: `1px solid ${form.is_half_day ? C.brand : C.border}`,
                    borderRadius: 8, padding: "12px 14px", marginBottom: 16,
                    background: form.is_half_day ? C.brandBg : C.bg,
                    transition: "background .15s, border-color .15s",
                  }}>
                    <label style={{
                      display: "flex", alignItems: "center", gap: 10,
                      fontSize: 13, fontWeight: 600, color: C.text, cursor: "pointer",
                    }}>
                      <input type="checkbox" checked={form.is_half_day}
                        onChange={(e) => setForm(f => ({ ...f, is_half_day: e.target.checked }))}
                        style={{ accentColor: C.brand, width: 16, height: 16 }} />
                      {T.applyAsHalfDay || "Apply as half day"}
                      <span style={{ fontWeight: 400, color: C.textSub, marginLeft: 6 }}>
                        ({T.deducts05 || "deducts 0.5 day"})
                      </span>
                    </label>
                    {form.is_half_day && (
                      <div style={{ display: "flex", gap: 10, marginTop: 12, flexWrap: "wrap" }}>
                        {[
                          { v: "first_half",  label: T.firstHalf  || "First half (Morning)"   },
                          { v: "second_half", label: T.secondHalf || "Second half (Afternoon)" },
                        ].map(opt => {
                          const active = form.half_day_period === opt.v;
                          return (
                            <label key={opt.v} style={{
                              flex: 1, minWidth: 200,
                              padding: "10px 12px", borderRadius: 6,
                              border: `2px solid ${active ? C.brand : C.border}`,
                              background: active ? C.surface : C.bg,
                              cursor: "pointer",
                              display: "flex", alignItems: "center", gap: 8,
                              fontSize: 13, fontWeight: active ? 700 : 500,
                              color: active ? C.brand : C.text,
                              fontFamily: "inherit",
                            }}>
                              <input type="radio" name="half_day_period" value={opt.v}
                                checked={active}
                                onChange={() => setForm(f => ({ ...f, half_day_period: opt.v }))}
                                style={{ accentColor: C.brand }} />
                              {opt.label}
                            </label>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}

                {leaveDays() > 0 && (
                  <div style={{ background: "#EFF6FF", borderRadius: 8, padding: "10px 14px", marginBottom: 16, fontSize: 13, color: "#1D4ED8", fontWeight: 600 }}>
                    📅 {leaveDays()} {leaveDays() === 0.5
                      ? (T.halfDaySelected || "half day selected")
                      : leaveDays() > 1
                        ? (T.daysSelectedPlural || "days selected")
                        : (T.daySelected || "day selected")}
                    {hasNoAnnualCap(currentBalance) ? (
                      <span style={{ marginLeft: 12, fontWeight: 400, color: C.brand }}>
                        · {T.compLeaveWorkedDatesVerify || "No annual cap - HR verifies the worked dates"}
                      </span>
                    ) : showNumericBalance(currentBalance) && (
                      <span style={{ marginLeft: 12, fontWeight: 400, color: currentBalance.remaining >= leaveDays() ? "#2F8F5C" : "#B91C1C" }}>
                        · {T.leavePageBalanceLabel || "Balance"}: {currentBalance.remaining} / {currentBalance.total} {T.leavePageDays || "days"}
                      </span>
                    )}
                  </div>
                )}

                {/* Description */}
                <div style={{ marginBottom: 16 }}>
                  <label style={labelStyle}>{T.reason||"Reason"} *</label>
                  <textarea value={form.description}
                    onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
                    placeholder={T.leaveDescriptionPlaceholder || "Please describe the reason for your leave request in detail..."}
                    rows={4} style={{ ...inputStyle, resize: "vertical", minHeight: 100 }} required />
                </div>

                {/* ── Supporting document ─────────────────────────────────────
                    Shown for any type this ORGANIZATION flagged `doc_required`
                    — the same flag that renders the "📎 Document Required"
                    badge in the info card above, so the badge and the field can
                    never disagree. This replaced `form.leave_type ===
                    "Medical Leave"`, a type name no catalogue contains, which
                    meant Sick Leave advertised "Doc required" and then offered
                    the employee nowhere to put the document.

                    Required (blocks Submit) only for types that also carry
                    `evidence_gate` — see `evidenceMissing` above. */}
                {showDocUpload && (
                  <DocumentUpload
                    C={C}
                    T={T}
                    file={attachment}
                    onChange={setAttachment}
                    required={docIsMandatory}
                    accept={selectedTypeInfo?.doc_accept}
                    formats={selectedTypeInfo?.doc_formats}
                    maxMb={selectedTypeInfo?.doc_max_mb}
                    showRequiredError={submitAttempted}
                    disabled={submitting}
                    hint={
                      docIsMandatory
                        ? (T.documentRequiredHint
                           || "Attach proof for this leave type — e.g. a medical certificate, hospital letter or birth certificate.")
                        : (T.documentOptionalHint
                           || "Attaching supporting proof now saves HR from chasing it later.")
                    }
                  />
                )}

                {/* `notEligible` shares the dimmed/disabled style with
                    hasHardErrors so the eligibility lock is visually
                    indistinguishable from the existing validation block. */}
                {(() => {
                  const notEligible = eligibilityBlocks;
                  const blocked = submitting || hasHardErrors || notEligible;
                  return (
                    <button type="submit" disabled={blocked} style={{
                      width: "100%", padding: "13px", background: (hasHardErrors || notEligible) ? "#94A3B8" : C.brand,
                      color: "#fff", border: "none", borderRadius: 8, fontWeight: 700, fontSize: 15,
                      cursor: blocked ? "not-allowed" : "pointer",
                      opacity: submitting ? 0.7 : 1,
                    }}>
                      {submitting
                        ? (T.loading || "Submitting...")
                        : notEligible
                          ? (T.notYetEligibleForLeaveShort || "Not yet eligible for paid leave")
                          : (T.submit_leave || "Submit Leave Request")}
                    </button>
                  );
                })()}
              </form>
            </div>

            {/* Balance sidebar */}
            <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 20, height: "fit-content" }}>
              <h3 style={{ margin: "0 0 16px", fontSize: 15, fontWeight: 700, color: C.text }}>{T.leave_balance||"Your Leave Balance"}</h3>
              {Object.keys(balance).length === 0 ? (
                <p style={{ color: C.sub, fontSize: 13 }}>{T.loading || "Loading..."}</p>
              ) : (
                // Skip keys prefixed with "_" (e.g., `_shared_pool`) — those
                // are metadata entries, not user-facing leave types. The
                // shared pool info is conveyed via the `shared_pool` flag
                // on each individual type.
                Object.entries(balance).filter(([type]) => !type.startsWith("_")).map(([type, data]) => (
                  <div key={type} style={{ marginBottom: 14, paddingBottom: 14, borderBottom: `1px solid ${C.border}` }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 4 }}>{type}</div>
                    {hasNoAnnualCap(data) ? (
                      <div style={{ fontSize: 12, color: C.brand, fontWeight: 600 }}>No annual cap</div>
                    ) : data.is_unlimited ? (
                      <div style={{ fontSize: 12, color: C.success, fontWeight: 600 }}>Unlimited</div>
                    ) : data.is_earned ? (
                      <div style={{ fontSize: 12, color: C.brand, fontWeight: 600 }}>Earned ({data.remaining} day{data.remaining === 1 ? "" : "s"})</div>
                    ) : (
                      <>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: C.sub }}>
                          <span>Available</span>
                          <span style={{ fontWeight: 700, color: data.remaining > 0 ? C.success : C.error }}>
                            {data.remaining} / {data.total} days
                          </span>
                        </div>
                        <div style={{ marginTop: 6, height: 4, background: C.borderLight, borderRadius: 4 }}>
                          <div style={{
                            height: 4, borderRadius: 4,
                            background: data.remaining > 0 ? C.brand : C.error,
                            width: `${Math.min(100, (data.remaining / Math.max(1, data.total)) * 100)}%`,
                          }} />
                        </div>
                      </>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {/* ════════════ HISTORY TAB ════════════ */}
        {tab === "history" && (
          <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 24 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
              <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: C.text }}>{T.leaveHistory || "My Leave History"}</h2>
              <span style={{ fontSize: 13, color: C.sub }}>{myLeaves.length} {myLeaves.length !== 1 ? (T.leavePageRequestsPlural || "requests") : (T.leavePageRequestSingular || "request")}</span>
            </div>
            {myLeaves.length === 0 ? (
              <div style={{ textAlign: "center", padding: "48px 0", color: C.sub }}>
                <div style={{ fontSize: 40, marginBottom: 8 }}>📋</div>
                <p style={{ margin: 0, fontWeight: 600 }}>{T.noLeaveRequests || "No leave requests yet"}</p>
                <p style={{ margin: "4px 0 0", fontSize: 13 }}>{T.apply_leave || "Apply Leave"}</p>
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {myLeaves.map(r => {
                  const typeInfo = leaveTypes.find(t => t.leave_type === r.leave_type);
                  return (
                    <div key={r.id} style={{ border: `1px solid ${C.border}`, borderRadius: 10, padding: "16px 20px" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                        <div style={{ flex: 1 }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                            {typeInfo && <span style={{ fontSize: 18 }}>{typeInfo.icon}</span>}
                            <span style={{ fontWeight: 700, color: C.text, fontSize: 15 }}>{r.leave_type}</span>
                            {r.is_lwp && <span style={{ fontSize: 11, color: "#7C3AED", fontWeight: 700, background: "#F5F3FF", padding: "2px 8px", borderRadius: 12 }}>LWP</span>}
                            {r.hr_flag && <span style={{ fontSize: 11, color: "#D97706", fontWeight: 700, background: "#FFF7ED", padding: "2px 8px", borderRadius: 12 }}>⚑ {T.leavePageHrFlagged || "HR Flagged"}</span>}
                          </div>
                          <div style={{ fontSize: 13, color: C.sub }}>
                            📅 {r.from_date} → {r.to_date} &nbsp;·&nbsp; {r.days} day{r.days !== 1 ? "s" : ""}
                          </div>
                          {r.description && (
                            <p style={{ margin: "6px 0 0", fontSize: 13, color: C.sub, lineHeight: 1.5 }}>{r.description}</p>
                          )}
                          {/* Openable — the employee can re-read what they filed.
                              Goes through the authenticated attachment route,
                              which allows the request's own owner. */}
                          {r.has_attachment && (
                            <div>
                              <button type="button"
                                onClick={() => {
                                  setDocOpenError(null);
                                  openLeaveAttachment(r.id)
                                    .catch(err => setDocOpenError({ id: r.id, message: err.message }));
                                }}
                                style={{
                                  marginTop: 6, fontSize: 12, color: "#0369A1", background: "none",
                                  border: "none", padding: 0, cursor: "pointer", fontFamily: "inherit",
                                  textDecoration: "underline",
                                }}>
                                📎 {r.attachment_name || T.leavePageDocumentAttached || "Document attached"}
                              </button>
                              {/* Kept on the row rather than routed to `error`,
                                  which only renders inside the Apply tab. */}
                              {docOpenError?.id === r.id && (
                                <p style={{ margin: "4px 0 0", fontSize: 12, color: C.error, fontWeight: 600 }}>
                                  ❌ {docOpenError.message}
                                </p>
                              )}
                            </div>
                          )}
                          {r.review_comment && (
                            <div style={{ marginTop: 10, background: r.status === "approved" ? C.successBg : r.status === "rejected" ? C.errorBg : "#F8FAFC", borderRadius: 6, padding: "8px 12px", fontSize: 12 }}>
                              <strong style={{ color: C.text }}>{T.previousComment || "HR Comment"}: </strong>
                              <span style={{ color: C.sub }}>{r.review_comment}</span>
                            </div>
                          )}
                        </div>
                        <div style={{ textAlign: "right", flexShrink: 0 }}>
                          <Badge status={r.status} />
                          <div style={{ marginTop: 6, fontSize: 11, color: "#94A3B8" }}>
                            Applied: {new Date(r.created_at).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })}
                          </div>
                          {/* Owner actions (Edit + Delete) are only meaningful
                              while the request is still owner-editable —
                              the backend returns 400 once it's approved /
                              rejected / locked. Admins/HR additionally get
                              Edit + Swap-dates on an approved leave so they
                              can move an already-granted day. */}
                          {(() => {
                            const ownerEditable = ["submitted", "under_review"].includes(r.status);
                            const adminCanAdjust = isAdmin && r.status === "approved";
                            if (!ownerEditable && !adminCanAdjust) return null;
                            return (
                              <div style={{ marginTop: 10, display: "flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
                                {adminCanAdjust && (
                                  <button
                                    onClick={() => openSwapDates(r)}
                                    disabled={busyId === r.id}
                                    style={{
                                      padding: "5px 10px", borderRadius: 6,
                                      border: `1px solid ${C.border}`, background: "#ECFDF5",
                                      color: "#047857", fontWeight: 600, fontSize: 11,
                                      cursor: "pointer", fontFamily: "inherit",
                                    }}>
                                    🔁 Swap dates
                                  </button>
                                )}
                                {(ownerEditable || adminCanAdjust) && (
                                  <button
                                    onClick={() => openEditLeave(r)}
                                    disabled={busyId === r.id}
                                    style={{
                                      padding: "5px 10px", borderRadius: 6,
                                      border: `1px solid ${C.border}`, background: "#EFF6FF",
                                      color: "#1D4ED8", fontWeight: 600, fontSize: 11,
                                      cursor: "pointer", fontFamily: "inherit",
                                    }}>
                                    ✎ Edit
                                  </button>
                                )}
                                {ownerEditable && (
                                  <button
                                    onClick={() => deleteLeave(r)}
                                    disabled={busyId === r.id}
                                    style={{
                                      padding: "5px 10px", borderRadius: 6,
                                      border: `1px solid ${C.border}`, background: "#FEF2F2",
                                      color: "#B91C1C", fontWeight: 600, fontSize: 11,
                                      cursor: "pointer", fontFamily: "inherit",
                                    }}>
                                    {busyId === r.id ? "…" : `🗑 ${T.leavePageDelete || "Delete"}`}
                                  </button>
                                )}
                              </div>
                            );
                          })()}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {/* ════════════ BALANCE TAB ════════════ */}
        {tab === "balance" && (
          <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 24 }}>
            <h2 style={{ margin: "0 0 4px", fontSize: 18, fontWeight: 700, color: C.text }}>{T.leave_balance || "Leave Balance"} — {new Date().getFullYear()}</h2>
            <p style={{ margin: "0 0 20px", fontSize: 13, color: C.sub }}>Balances are calculated based on your accrual rate for {new Date().getMonth() + 1} months elapsed this year.</p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 16 }}>
              {/* Skip meta entries (e.g., _shared_pool) so they don't render
                  as a leave-type card with a raw key as the title. */}
              {Object.entries(balance).filter(([type]) => !type.startsWith("_")).map(([type, data]) => {
                const typeInfo = leaveTypes.find(t => t.leave_type === type);
                const pct = data.is_unlimited ? 100 : Math.min(100, (data.remaining / Math.max(1, data.total)) * 100);
                return (
                  <div key={type} style={{ border: `1px solid ${C.border}`, borderRadius: 10, padding: "16px 20px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                      {typeInfo && <span style={{ fontSize: 20 }}>{typeInfo.icon}</span>}
                      <span style={{ fontWeight: 700, color: C.text, fontSize: 14 }}>{type}</span>
                    </div>
                    {hasNoAnnualCap(data) ? (
                      <>
                        <div style={{ fontSize: 13, color: C.brand, fontWeight: 700, marginBottom: 6 }}>No annual cap</div>
                        <div style={{ fontSize: 13, color: C.sub }}>
                          {T.compLeaveWorkedDatesVerify || "Apply by listing the extra dates already worked."}
                        </div>
                      </>
                    ) : data.is_unlimited ? (
                      <div style={{ fontSize: 13, color: C.success, fontWeight: 600 }}>Unlimited</div>
                    ) : data.is_earned ? (
                      <>
                        <div style={{ fontSize: 13, color: C.brand, fontWeight: 700, marginBottom: 6 }}>Earned from overtime</div>
                        <div style={{ fontSize: 14, fontWeight: 700, color: data.remaining > 0 ? C.success : C.sub }}>
                          {data.remaining} day{data.remaining === 1 ? "" : "s"} {T.leavePageAvailableLower || "available"}
                        </div>
                      </>
                    ) : (
                      <>
                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8, fontSize: 13 }}>
                          <span style={{ color: C.sub }}>Used: <strong style={{ color: C.text }}>{data.used} days</strong></span>
                          <span style={{ color: C.sub }}>Total: <strong style={{ color: C.text }}>{data.total} days</strong></span>
                        </div>
                        <div style={{ height: 8, background: C.borderLight, borderRadius: 4, marginBottom: 8 }}>
                          <div style={{ height: 8, borderRadius: 4, background: pct > 30 ? C.brand : C.error, width: `${pct}%`, transition: "width 0.3s" }} />
                        </div>
                        <div style={{ fontSize: 14, fontWeight: 700, color: data.remaining > 0 ? C.success : C.error }}>
                          {data.remaining} days remaining
                        </div>
                      </>
                    )}
                    <div style={{ marginTop: 8, display: "flex", gap: 8, flexWrap: "wrap" }}>
                      {data.doc_required && !hasNoAnnualCap(data) && <span style={{ fontSize: 11, color: C.amber, background: C.amberBg, padding: "2px 6px", borderRadius: 10 }}>📎 {T.leavePageDocRequired || "Doc required"}</span>}
                      {data.notice_days > 0 && <span style={{ fontSize: 11, color: C.sub, background: C.surface2, padding: "2px 6px", borderRadius: 10 }}>⏰ {data.notice_days}{T.leavePageNoticeDaysSuffix || "d notice"}</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* ════════════ LEAVE TYPES TAB ════════════ */}
        {tab === "types" && (
          <div>
            <div style={{ marginBottom: 20 }}>
              <h2 style={{ margin: "0 0 4px", fontSize: 18, fontWeight: 700, color: C.text }}>{T.leaveTypes || "Leave Types"}</h2>
              <p style={{ margin: 0, fontSize: 13, color: C.sub }}>
                Complete details for all available leave types as per the Temveris Leave Policy (effective Jan 28, 2026).
              </p>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {leaveTypes.map(lt => {
                const isExpanded = expandedType === lt.leave_type;
                const bal = balance[lt.leave_type];
                return (
                  <div key={lt.leave_type} style={{ background: C.surface, borderRadius: 12, border: `1px solid ${isExpanded ? C.brand : C.border}`, overflow: "hidden", transition: "border-color 0.2s" }}>
                    {/* Header row */}
                    <div
                      onClick={() => setExpandedType(isExpanded ? null : lt.leave_type)}
                      style={{ display: "flex", alignItems: "center", gap: 14, padding: "16px 20px", cursor: "pointer" }}>
                      <div style={{ fontSize: 28, flexShrink: 0 }}>{lt.icon}</div>
                      <div style={{ flex: 1 }}>
                        <div style={{ fontWeight: 700, color: C.text, fontSize: 15 }}>{lt.leave_type}</div>
                        <div style={{ fontSize: 13, color: C.sub, marginTop: 2 }}>{lt.description}</div>
                      </div>
                      <div style={{ display: "flex", gap: 10, alignItems: "center", flexShrink: 0 }}>
                        {hasNoAnnualCap(bal) && (
                          <div style={{ textAlign: "right" }}>
                            <div style={{ fontSize: 11, color: C.sub }}>Limit</div>
                            <div style={{ fontSize: 13, fontWeight: 700, color: C.brand }}>No annual cap</div>
                          </div>
                        )}
                        {showNumericBalance(bal) && (
                          <div style={{ textAlign: "right" }}>
                            <div style={{ fontSize: 11, color: C.sub }}>{T.leavePageYourBalance || "Your Balance"}</div>
                            <div style={{ fontSize: 14, fontWeight: 700, color: bal.remaining > 0 ? C.success : C.error }}>
                              {bal.remaining} / {bal.total} days
                            </div>
                          </div>
                        )}
                        {bal?.is_earned && !hasNoAnnualCap(bal) && (
                          <div style={{ textAlign: "right" }}>
                            <div style={{ fontSize: 11, color: C.sub }}>Earned</div>
                            <div style={{ fontSize: 13, fontWeight: 700, color: C.brand }}>{bal.remaining} day{bal.remaining === 1 ? "" : "s"}</div>
                          </div>
                        )}
                        {bal?.is_unlimited && (
                          <div style={{ textAlign: "right" }}>
                            <div style={{ fontSize: 11, color: C.sub }}>Limit</div>
                            <div style={{ fontSize: 13, fontWeight: 700, color: C.success }}>Unlimited</div>
                          </div>
                        )}
                        <div style={{ fontSize: 14, color: C.sub, transition: "transform 0.2s", transform: isExpanded ? "rotate(180deg)" : "none" }}>▾</div>
                      </div>
                    </div>

                    {/* Expanded details */}
                    {isExpanded && (
                      <div style={{ borderTop: `1px solid ${C.border}`, padding: "20px 24px" }}>
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 16, marginBottom: 20 }}>

                          <div style={{ background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 8, padding: "14px 16px" }}>
                            <div style={{ fontSize: 11, fontWeight: 700, color: C.sub, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{T.eligibility || "Eligibility"}</div>
                            <div style={{ fontSize: 13, color: C.text, lineHeight: 1.6 }}>{lt.eligibility}</div>
                          </div>

                          <div style={{ background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 8, padding: "14px 16px" }}>
                            <div style={{ fontSize: 11, fontWeight: 700, color: C.sub, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{T.maxDuration || "Maximum Duration"}</div>
                            <div style={{ fontSize: 13, color: C.text, lineHeight: 1.6 }}>{lt.max_duration}</div>
                          </div>

                          <div style={{ background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 8, padding: "14px 16px" }}>
                            <div style={{ fontSize: 11, fontWeight: 700, color: C.sub, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{T.requiredDocs || "Required Documents"}</div>
                            <div style={{ fontSize: 13, color: lt.doc_required ? C.amber : C.text, lineHeight: 1.6 }}>
                              {lt.doc_required ? "📎 " : ""}{lt.required_docs}
                            </div>
                          </div>

                          <div style={{ background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 8, padding: "14px 16px" }}>
                            <div style={{ fontSize: 11, fontWeight: 700, color: C.sub, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{T.leavePageQuickStats || "Quick Stats"}</div>
                            <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13, color: C.text }}>
                              <div><span style={{ color: C.sub }}>{T.leavePageNoticePeriodLabel || "Notice Period"}: </span><strong>{lt.notice_days === 0 ? (T.leavePageNotRequired || "Not required") : `${lt.notice_days} ${T.leavePageDaysParen || "day(s)"}`}</strong></div>
                              <div><span style={{ color: C.sub }}>{T.leavePageAnnualLimitLabel || "Annual Limit"}: </span><strong>{limitLabelFor(lt)}</strong></div>
                              <div><span style={{ color: C.sub }}>{T.leavePagePayStatusLabel || "Pay Status"}: </span><strong style={{ color: lt.is_paid ? C.success : C.error }}>{lt.is_paid ? (T.leavePagePaid || "Paid") : (T.leavePageUnpaid || "Unpaid")}</strong></div>
                              {lt.monthly_accrual > 0 && <div><span style={{ color: C.sub }}>{T.leavePageAccrualLabel || "Accrual"}: </span><strong>{lt.monthly_accrual} {T.leavePageDaysPerMonth || "days/month"}</strong></div>}
                            </div>
                          </div>
                        </div>

                        {lt.terms && lt.terms.length > 0 && (
                          <div style={{ background: C.amberBg, border: `1px solid ${C.amberBdr}`, borderRadius: 8, padding: "14px 16px" }}>
                            <div style={{ fontSize: 11, fontWeight: 700, color: C.amber, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 10 }}>{T.termsAndConditions || "Terms & Conditions"}</div>
                            <ul style={{ margin: 0, paddingLeft: 20 }}>
                              {lt.terms.map((t, i) => (
                                <li key={i} style={{ fontSize: 13, color: C.amber, lineHeight: 1.6, marginBottom: 4 }}>{t}</li>
                              ))}
                            </ul>
                          </div>
                        )}

                        <div style={{ marginTop: 16, display: "flex", gap: 10 }}>
                          <button
                            onClick={() => { setTab("apply"); setForm(f => ({ ...f, leave_type: lt.leave_type })); }}
                            style={{ padding: "9px 20px", background: C.brand, color: "#fff", border: "none", borderRadius: 8, fontWeight: 600, fontSize: 13, cursor: "pointer" }}>
                            Apply {lt.icon}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* ════════════ POLICIES TAB ════════════ */}
        {tab === "policies" && (
          <div style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 24 }}>
            <h2 style={{ margin: "0 0 4px", fontSize: 18, fontWeight: 700, color: C.text }}>{T.leavePolicies || "Leave Policies Summary"}</h2>
            <p style={{ margin: "0 0 20px", fontSize: 13, color: C.sub }}>{T.leavePagePolicyConfiguredNote || "Policy details as configured by HR / Administration."}</p>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr style={{ background: C.surface2 }}>
                    {[T.leaveType||"Leave Type",T.annualLimitDays||"Annual Limit",T.noticeDays||"Notice Period",T.document||"Documents",T.monthlyAccrual||"Monthly Accrual",T.paidLeave||"Paid"].map(h => (
                      <th key={h} style={{ padding: "10px 16px", textAlign: "left", color: C.sub, fontWeight: 600, borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {policies.map(p => {
                    const typeInfo = leaveTypes.find(t => t.leave_type === p.leave_type);
                    return (
                      <tr key={p.leave_type} style={{ borderBottom: `1px solid ${C.border}` }}
                        onMouseEnter={e => e.currentTarget.style.background = C.surface2}
                        onMouseLeave={e => e.currentTarget.style.background = ""}>
                        <td style={{ padding: "12px 16px", fontWeight: 600, color: C.text }}>
                          {typeInfo && <span style={{ marginRight: 6 }}>{typeInfo.icon}</span>}
                          {p.leave_type}
                        </td>
                        <td style={{ padding: "12px 16px", color: C.sub }}>{limitLabelFor(p)}</td>
                        <td style={{ padding: "12px 16px", color: C.sub }}>{p.notice_days === 0 ? "None" : `${p.notice_days} day(s)`}</td>
                        <td style={{ padding: "12px 16px" }}>
                          <span style={{ color: p.doc_required ? C.amber : C.success, fontWeight: 600 }}>
                            {p.doc_required ? `✓ ${T.leavePageRequiredWord || "Required"}` : (T.leavePageNotRequired || "Not required")}
                          </span>
                        </td>
                        <td style={{ padding: "12px 16px", color: C.sub }}>
                          {p.monthly_accrual > 0 ? `${p.monthly_accrual} ${T.leavePageDaysPerMonth || "days/month"}` : (T.leavePageLumpSumEventBased || "Lump sum / Event-based")}
                        </td>
                        <td style={{ padding: "12px 16px" }}>
                          <span style={{ color: p.leave_type === "Unpaid Leave" ? C.error : C.success, fontWeight: 600 }}>
                            {p.leave_type === "Unpaid Leave" ? (T.leavePageUnpaid || "Unpaid") : (T.leavePagePaid || "Paid")}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ════════════ EDIT LEAVE MODAL (employee) ════════════ */}
        {editLeave && (
          <div style={{
            position: "fixed", inset: 0, background: "rgba(15,23,42,0.55)",
            display: "flex", alignItems: "center", justifyContent: "center",
            zIndex: 9000, padding: 20,
          }} onClick={() => !savingEdit && setEditLeave(null)}>
            <div onClick={e => e.stopPropagation()} style={{
              width: 520, maxWidth: "100%", background: C.surface,
              borderRadius: 14, padding: 26,
              boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
            }}>
              <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
                {T.editLeaveRequest || "Edit Leave Request"}
              </p>
              <p style={{ margin: "4px 0 18px", fontSize: 12.5, color: C.sub, lineHeight: 1.5 }}>
                {editLeave && !["submitted", "under_review"].includes(editLeave.status)
                  ? "Admin edit — changes to this approved leave are recorded in the audit log."
                  : (T.editLeaveSub || "You can edit this request only while it's still pending review.")}
              </p>

              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                {T.leaveType || "Leave type"}
              </label>
              <select
                value={editForm.leave_type}
                onChange={e => setEditForm(p => ({ ...p, leave_type: e.target.value }))}
                style={{
                  width: "100%", padding: "10px 12px", borderRadius: 7,
                  border: `1px solid ${C.border}`, fontSize: 14, fontFamily: "inherit",
                  background: C.surface, color: C.text, outline: "none",
                  boxSizing: "border-box", marginBottom: 14,
                }}>
                {!leaveTypes.some(lt => lt.leave_type === editForm.leave_type) && editForm.leave_type && (
                  <option value={editForm.leave_type}>{editForm.leave_type}</option>
                )}
                {leaveTypes.map(lt => (
                  <option key={lt.leave_type} value={lt.leave_type}>{lt.leave_type}</option>
                ))}
              </select>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                    {T.fromDate || "From date"}
                  </label>
                  <input type="date" value={editForm.from_date}
                    onChange={e => setEditForm(p => ({ ...p, from_date: e.target.value }))}
                    style={{
                      width: "100%", padding: "10px 12px", borderRadius: 7,
                      border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                      background: C.surface, color: C.text, outline: "none",
                      boxSizing: "border-box",
                    }} />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                    {T.toDate || "To date"}
                  </label>
                  <input type="date" value={editForm.to_date}
                    onChange={e => setEditForm(p => ({ ...p, to_date: e.target.value }))}
                    style={{
                      width: "100%", padding: "10px 12px", borderRadius: 7,
                      border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                      background: C.surface, color: C.text, outline: "none",
                      boxSizing: "border-box",
                    }} />
                </div>
              </div>

              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginTop: 14, marginBottom: 5 }}>
                {T.description || "Description"}
              </label>
              <textarea value={editForm.description}
                onChange={e => setEditForm(p => ({ ...p, description: e.target.value }))}
                rows={3}
                style={{
                  width: "100%", padding: "10px 12px", borderRadius: 7,
                  border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                  background: C.surface, color: C.text, outline: "none",
                  boxSizing: "border-box", resize: "vertical", marginBottom: 18,
                }} />

              <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
                <button onClick={() => setEditLeave(null)} disabled={savingEdit}
                  style={{
                    padding: "9px 18px", borderRadius: 7,
                    border: `1px solid ${C.border}`, background: C.surface,
                    color: C.text, fontWeight: 600, fontSize: 13,
                    cursor: "pointer", fontFamily: "inherit",
                  }}>
                  {T.cancel || "Cancel"}
                </button>
                <button onClick={saveEditLeave} disabled={savingEdit}
                  style={{
                    padding: "9px 22px", borderRadius: 7, border: "none",
                    background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13,
                    cursor: "pointer", fontFamily: "inherit",
                    opacity: savingEdit ? 0.55 : 1,
                  }}>
                  {savingEdit ? (T.saving || "Saving...") : (T.save || "Save")}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ════════════ SWAP DATES MODAL (admin/HR · approved leave) ════════════ */}
        {swapLeave && (
          <div style={{
            position: "fixed", inset: 0, background: "rgba(15,23,42,0.55)",
            display: "flex", alignItems: "center", justifyContent: "center",
            zIndex: 9002, padding: 20,
          }} onClick={() => !savingSwap && setSwapLeave(null)}>
            <div onClick={e => e.stopPropagation()} style={{
              width: 460, maxWidth: "100%", background: C.surface,
              borderRadius: 14, padding: 24,
              boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
              border: `1px solid ${C.border}`,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                <div style={{ width: 36, height: 36, borderRadius: "50%", background: "#ECFDF5", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18 }}>🔁</div>
                <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>{T.leavePageSwapLeaveDatesTitle || "Swap leave dates"}</p>
              </div>

              <div style={{ background: "#F8FAFC", border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 12px", fontSize: 13, color: C.text, marginBottom: 14, lineHeight: 1.5 }}>
                <div><strong>{swapLeave.leave_type}</strong></div>
                <div style={{ color: C.sub, fontSize: 12, marginTop: 2 }}>
                  Currently: {String(swapLeave.from_date).slice(0, 10)} → {String(swapLeave.to_date).slice(0, 10)}
                </div>
              </div>

              <p style={{ margin: "0 0 14px", fontSize: 12.5, color: C.sub, lineHeight: 1.5 }}>
                Move this approved leave to new dates. The change is recorded in the audit log.
              </p>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>{T.leavePageNewFromDate || "New from date"}</label>
                  <input type="date" value={swapForm.from_date}
                    onChange={e => setSwapForm(p => ({ ...p, from_date: e.target.value }))}
                    style={{
                      width: "100%", padding: "10px 12px", borderRadius: 7,
                      border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                      background: C.surface, color: C.text, outline: "none", boxSizing: "border-box",
                    }} />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>{T.leavePageNewToDate || "New to date"}</label>
                  <input type="date" value={swapForm.to_date}
                    onChange={e => setSwapForm(p => ({ ...p, to_date: e.target.value }))}
                    style={{
                      width: "100%", padding: "10px 12px", borderRadius: 7,
                      border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                      background: C.surface, color: C.text, outline: "none", boxSizing: "border-box",
                    }} />
                </div>
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 20 }}>
                <button onClick={() => setSwapLeave(null)} disabled={savingSwap}
                  style={{
                    padding: "9px 18px", borderRadius: 7,
                    border: `1px solid ${C.border}`, background: C.surface,
                    color: C.text, fontWeight: 600, fontSize: 13,
                    cursor: savingSwap ? "default" : "pointer", fontFamily: "inherit",
                  }}>
                  Cancel
                </button>
                <button onClick={saveSwapDates} disabled={savingSwap}
                  style={{
                    padding: "9px 22px", borderRadius: 7, border: "none",
                    background: "#047857", color: "#fff", fontWeight: 700, fontSize: 13,
                    cursor: savingSwap ? "default" : "pointer", fontFamily: "inherit",
                    opacity: savingSwap ? 0.55 : 1,
                  }}>
                  {savingSwap ? (T.leavePageSaving || "Saving...") : (T.leavePageSaveDates || "Save dates")}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ════════════ DELETE LEAVE — REASON CAPTURE MODAL ════════════ */}
        {deleteLeaveTarget && (() => {
          const r = deleteLeaveTarget;
          const close = () => { if (!deleting) { setDeleteLeaveTarget(null); setDeleteReason(""); } };
          return (
            <div style={{
              position: "fixed", inset: 0, background: "rgba(15,23,42,0.55)",
              display: "flex", alignItems: "center", justifyContent: "center",
              zIndex: 9001, padding: 20,
            }} onClick={close}>
              <div onClick={e => e.stopPropagation()} style={{
                width: 480, maxWidth: "100%", background: C.surface,
                borderRadius: 14, padding: 24,
                boxShadow: "0 20px 60px rgba(0,0,0,0.28)",
                border: `1px solid ${C.border}`,
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                  <div style={{ width: 36, height: 36, borderRadius: "50%", background: "#FEF2F2", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18 }}>🗑</div>
                  <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
                    Delete this leave request?
                  </p>
                </div>
                <div style={{ background: "#F8FAFC", border: `1px solid ${C.border}`, borderRadius: 8, padding: "10px 12px", fontSize: 13, color: C.text, marginBottom: 14, lineHeight: 1.5 }}>
                  <div><strong>{r.leave_type}</strong> · {String(r.from_date).slice(0,10)} → {String(r.to_date).slice(0,10)}</div>
                  <div style={{ color: C.sub, fontSize: 12, marginTop: 2 }}>{T.leavePageStatusLabel || "Status"}: {r.status}</div>
                </div>
                <p style={{ margin: "0 0 6px", fontSize: 12.5, color: C.sub, lineHeight: 1.5 }}>
                  Leaves affect payroll, so please tell us briefly why you're deleting this one. Your reason is saved in the audit log.
                </p>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                  Reason for deletion <span style={{ color: "#DC2626" }}>*</span>
                </label>
                <textarea
                  autoFocus
                  rows={3}
                  value={deleteReason}
                  onChange={e => setDeleteReason(e.target.value)}
                  placeholder={T.leavePageDeleteReasonPlaceholder || "e.g. applied the wrong dates, plans changed, duplicate request…"}
                  maxLength={500}
                  style={{
                    width: "100%", padding: "10px 12px", borderRadius: 7,
                    border: `1px solid ${C.border}`, fontSize: 14, fontFamily: "inherit",
                    background: C.surface, color: C.text, outline: "none",
                    boxSizing: "border-box", resize: "vertical", marginBottom: 6,
                  }} />
                <div style={{ fontSize: 11, color: C.sub, marginBottom: 18, textAlign: "right" }}>
                  {deleteReason.length}/500
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
                  <button onClick={close} disabled={deleting}
                    style={{
                      padding: "9px 18px", borderRadius: 7,
                      border: `1px solid ${C.border}`, background: C.surface,
                      color: C.text, fontWeight: 600, fontSize: 13,
                      cursor: deleting ? "default" : "pointer", fontFamily: "inherit",
                    }}>
                    Keep leave
                  </button>
                  <button onClick={confirmDeleteLeave} disabled={deleting || !deleteReason.trim()}
                    style={{
                      padding: "9px 22px", borderRadius: 7, border: "none",
                      background: deleting || !deleteReason.trim() ? "#FCA5A5" : "#B91C1C",
                      color: "#fff", fontWeight: 700, fontSize: 13,
                      cursor: deleting || !deleteReason.trim() ? "default" : "pointer",
                      fontFamily: "inherit",
                    }}>
                    {deleting ? (T.leavePageDeleting || "Deleting…") : (T.leavePageDeleteLeave || "Delete leave")}
                  </button>
                </div>
              </div>
            </div>
          );
        })()}

      </div>
      </div>
    </div>
  );

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden", background: C.bg, fontFamily: "'Segoe UI', Arial, sans-serif" }}>
      {pageContent}
    </div>
  );
}
