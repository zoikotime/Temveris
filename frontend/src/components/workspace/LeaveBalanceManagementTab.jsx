import React, { useEffect, useMemo, useState, useCallback } from "react";
import { useTheme } from "../../context/ThemeContext";
import { wsGet, wsSend } from "./_api";

// ── Leave Balance Management ──────────────────────────────────────────────────
// HR/Admin module (Workspace Settings) to grant / deduct / reset / set a custom
// per-employee leave allocation for ANY leave type. Balances are derived server
// side as remaining = allocated - used; this UI only ever changes the
// `allocated` override (POST /leave/admin/balances/adjust). The leave-type list
// is loaded from /leave/types so every current and future (custom) type appears
// automatically with no code change.

const ADJUSTMENT_TYPES = [
  { v: "grant",  label: "Grant Leave",   hint: "Add days on top of the current allocation" },
  { v: "deduct", label: "Deduct Leave",  hint: "Remove days (never below days already used)" },
  { v: "reset",  label: "Reset",         hint: "Clear the override — revert to company default" },
  { v: "custom", label: "Custom Balance", hint: "Set the allocation to an exact number" },
];

const STATUS_META = {
  healthy:    { label: "Healthy",    dot: "🟢", bg: "#ECFDF5", fg: "#047857", bd: "#A7F3D0" },
  low:        { label: "Low",        dot: "🟡", bg: "#FFFBEB", fg: "#B45309", bd: "#FDE68A" },
  no_balance: { label: "No balance", dot: "🔴", bg: "#FEF2F2", fg: "#B91C1C", bd: "#FECACA" },
  unlimited:  { label: "No cap",     dot: "🔵", bg: "#EFF6FF", fg: "#1D4ED8", bd: "#BFDBFE" },
};

const todayStr = () => new Date().toISOString().split("T")[0];
const fmtDateTime = (v) => { if (!v) return "—"; try { return new Date(v).toLocaleString(); } catch { return String(v); } };
const fmtDate     = (v) => { if (!v) return "—"; try { return new Date(v).toLocaleDateString(); } catch { return String(v); } };
const num = (n) => (n === null || n === undefined ? "—" : (Math.round(n * 10) / 10));

export default function LeaveBalanceManagementTab({ isAdmin, showToast }) {
  const { C } = useTheme();

  const [leaveTypes, setLeaveTypes] = useState([]);
  // Empty until the catalogue loads; set to this organization's first
              // configured type below. "Annual Leave" is not every tenant's.
  const [selectedType, setSelectedType] = useState("");
  const [rows, setRows]       = useState([]);
  const [entitlement, setEntitlement] = useState(0);
  const [loading, setLoading] = useState(false);

  // Client-side filters over the loaded rows.
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [deptFilter, setDeptFilter]     = useState("all");

  // Adjustment form
  // `leave_types` is a list: the same adjustment can be applied to several leave
  // types in one go (e.g. set every paid type to 0 for an intern). Each type is
  // still its own POST /leave/admin/balances/adjust, so audit history and the
  // backend contract are unchanged.
  const blankForm = {
    user_id: "", leave_types: ["Annual Leave"], adjustment_type: "grant",
    adjustment_value: "", reason: "", effective_date: todayStr(), notify: true,
  };
  const [form, setForm] = useState(blankForm);
  const [typePickerOpen, setTypePickerOpen] = useState(false);
  const [formBalances, setFormBalances] = useState(null);   // GET /admin/balances/{id}
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // History modal
  const [historyFor, setHistoryFor] = useState(null);       // { user_id, name }
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // ── styles ──
  const cardS   = { background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: 16, boxSizing: "border-box" };
  const inputS  = { width: "100%", padding: "9px 11px", borderRadius: 8, border: `1px solid ${C.border}`, fontSize: 13, color: C.text, background: C.surface, boxSizing: "border-box", fontFamily: "inherit", outline: "none" };
  const labelS  = { display: "block", fontSize: 12, fontWeight: 700, color: C.textSub, marginBottom: 6 };
  const primaryBtn = { padding: "10px 16px", borderRadius: 8, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
  const ghostBtn   = { padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontWeight: 700, fontSize: 12, cursor: "pointer", fontFamily: "inherit" };

  // ── loaders ──
  const loadTypes = useCallback(async () => {
    try {
      const d = await wsGet("/leave/types");
      const names = (Array.isArray(d) ? d : []).map(t => t.leave_type).filter(Boolean);
      setLeaveTypes(names);
      // Land on a type THIS organization actually has.
      setSelectedType(prev => (prev && names.includes(prev)) ? prev : (names[0] || ""));
    } catch { /* leave types list stays empty; dropdown falls back below */ }
  }, []);

  const loadRows = useCallback(async (lt) => {
    setLoading(true);
    try {
      const d = await wsGet(`/leave/admin/balances?leave_type=${encodeURIComponent(lt)}`);
      setRows(Array.isArray(d?.rows) ? d.rows : []);
      setEntitlement(d?.entitlement ?? 0);
    } catch (e) {
      showToast?.(`Could not load balances: ${e.message}`, "error");
      setRows([]);
    }
    setLoading(false);
  }, [showToast]);

  useEffect(() => { loadTypes(); }, [loadTypes]);
  // Nothing to load until the catalogue has named a type for this org.
  useEffect(() => { if (selectedType) loadRows(selectedType); },
            [selectedType, loadRows]);

  // On employee selection, load ALL of that employee's leave-type balances once.
  // The displayed type is re-sliced client-side via `current`, so switching the
  // form's leave type needs no refetch (the endpoint returns every type).
  useEffect(() => {
    if (!form.user_id) { setFormBalances(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const d = await wsGet(`/leave/admin/balances/${form.user_id}`);
        if (!cancelled) setFormBalances(d?.balances || null);
      } catch { if (!cancelled) setFormBalances(null); }
    })();
    return () => { cancelled = true; };
  }, [form.user_id]);

  const typeOptions = leaveTypes.length ? leaveTypes
                    : (selectedType ? [selectedType] : []);

  // ── derived: filtered rows + dashboard counts ──
  const departments = useMemo(
    () => Array.from(new Set(rows.map(r => r.department).filter(Boolean))).sort(),
    [rows]
  );
  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter(r => {
      if (q && !(`${r.name || ""} ${r.email || ""}`.toLowerCase().includes(q))) return false;
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (deptFilter !== "all" && r.department !== deptFilter) return false;
      return true;
    });
  }, [rows, search, statusFilter, deptFilter]);

  const stats = useMemo(() => ({
    total:     rows.length,
    zero:      rows.filter(r => r.status === "no_balance").length,
    low:       rows.filter(r => r.status === "low").length,
    overrides: rows.filter(r => r.has_override).length,
  }), [rows]);

  // ── form helpers ──
  const setF = (patch) => setForm(f => ({ ...f, ...patch }));

  // The final allocation one leave type would get from the form's adjustment.
  const previewFor = (lt) => {
    const cur = formBalances?.[lt];
    if (!cur) return null;
    const base = cur.allocated ?? 0;
    const used = cur.used ?? 0;
    const v = parseFloat(form.adjustment_value);
    if (form.adjustment_type === "reset")  return entitlementFor(lt);
    if (Number.isNaN(v)) return null;
    if (form.adjustment_type === "custom") return v;
    if (form.adjustment_type === "grant")  return base + v;
    if (form.adjustment_type === "deduct") return Math.max(used, base - v);
    return null;
  };
  // With exactly one type selected the panel behaves as before (snapshot +
  // single preview); with several it shows a per-type preview table.
  const singleType = form.leave_types.length === 1 ? form.leave_types[0] : null;
  const current = singleType ? (formBalances?.[singleType] || null) : null;   // {allocated, used, remaining, has_override}
  const finalPreview = singleType ? previewFor(singleType) : null;
  const toggleType = (lt) => setForm(f => ({
    ...f,
    leave_types: f.leave_types.includes(lt)
      ? f.leave_types.filter(t => t !== lt)
      : typeOptions.filter(t => t === lt || f.leave_types.includes(t)),   // keep catalogue order
  }));

  function entitlementFor(lt) {
    // Reset reverts to the company default. We don't have every type's policy
    // limit on the client, so show "default" textually when unknown; for the
    // table's selected type we do know the entitlement.
    if (lt === selectedType) return entitlement;
    return null;
  }

  const canSave = () => {
    if (!form.user_id) return false;
    if (!form.leave_types.length) return false;
    if (form.adjustment_type !== "reset") {
      const v = parseFloat(form.adjustment_value);
      if (Number.isNaN(v) || v < 0) return false;
    }
    return true;
  };

  const openAdjustFor = (row) => {
    setForm({
      ...blankForm,
      user_id: String(row.user_id),
      leave_types: [selectedType],
      effective_date: todayStr(),
    });
  };

  const submit = async () => {
    setSaving(true);
    const types = form.leave_types;
    const saved = [], failed = [];
    // One request per type, in order. A failure on one type does not stop the
    // others; the summary names exactly which types did not save.
    for (const lt of types) {
      try {
        const res = await wsSend("POST", "/leave/admin/balances/adjust", {
          user_id: parseInt(form.user_id, 10),
          leave_type: lt,
          adjustment_type: form.adjustment_type,
          adjustment_value: form.adjustment_type === "reset" ? 0 : parseFloat(form.adjustment_value),
          reason: form.reason,
          effective_date: form.effective_date || null,
          notify: !!form.notify,
        });
        saved.push(res);
      } catch (e) {
        failed.push(`${lt} (${e.message})`);
      }
    }
    if (saved.length === 1 && !failed.length) {
      const res = saved[0];
      showToast?.(
        `Saved · ${res.leave_type}: ${res.new_allocation === null ? "reset to default" : `${num(res.new_allocation)} allocated`}`
        + (res.remaining === null ? "" : ` · ${num(res.remaining)} remaining`),
        "success"
      );
    } else if (saved.length) {
      showToast?.(`Saved ${saved.length} of ${types.length} leave types`
        + (failed.length ? ` · failed: ${failed.join(", ")}` : ""), failed.length ? "error" : "success");
    } else {
      showToast?.(`Adjustment failed: ${failed.join(", ")}`, "error");
    }
    if (saved.length) {
      setConfirmOpen(false);
      // Refresh the table if it's showing a type we just changed, then reset
      // the form. Clearing user_id makes the [form.user_id] effect drop the
      // stale preview — no manual refetch needed (it would race the reset).
      if (saved.some(r => r.leave_type === selectedType)) loadRows(selectedType);
      setForm(f => ({ ...blankForm, leave_types: f.leave_types }));
    }
    setSaving(false);
  };

  const openHistory = async (row) => {
    setHistoryFor({ user_id: row.user_id, name: row.name });
    setHistoryLoading(true);
    setHistory([]);
    try {
      const d = await wsGet(`/leave/admin/balances/${row.user_id}/history`);
      setHistory(Array.isArray(d) ? d : []);
    } catch (e) {
      showToast?.(`Could not load history: ${e.message}`, "error");
    }
    setHistoryLoading(false);
  };

  if (!isAdmin) {
    return <div style={{ padding: 24, color: C.textSub }}>You don't have access to Leave Balance Management.</div>;
  }

  const StatusBadge = ({ s }) => {
    const m = STATUS_META[s] || STATUS_META.healthy;
    return (
      <span style={{ padding: "3px 9px", borderRadius: 999, border: `1px solid ${m.bd}`, background: m.bg, color: m.fg, fontSize: 11, fontWeight: 700, whiteSpace: "nowrap" }}>
        {m.dot} {m.label}
      </span>
    );
  };

  const StatCard = ({ label, value, tone }) => (
    <div style={{ ...cardS, padding: 14, flex: "1 1 140px", minWidth: 140 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 800, color: tone || C.text, marginTop: 4 }}>{value}</div>
    </div>
  );

  return (
    <div style={{ fontFamily: "inherit" }}>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <h2 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0 }}>Leave Balance Management</h2>
        <p style={{ fontSize: 14, color: C.textSub, margin: "6px 0 0", lineHeight: 1.5 }}>
          Grant, deduct, reset, or override an employee's leave allocation for any leave type.
          Balances recalculate instantly across the app — used days are never overwritten.
        </p>
      </div>

      {/* Dashboard cards */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <StatCard label={`Employees (${selectedType})`} value={stats.total} />
        <StatCard label="No balance" value={stats.zero} tone="#B91C1C" />
        <StatCard label="Low balance" value={stats.low} tone="#B45309" />
        <StatCard label="Overrides active" value={stats.overrides} tone={C.brand} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 340px", gap: 18, alignItems: "start" }}>
        {/* ── LEFT: table + filters ── */}
        <div style={cardS}>
          {/* Filters */}
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 14 }}>
            <div style={{ flex: "2 1 200px" }}>
              <label style={labelS}>Leave Type</label>
              <select value={selectedType} onChange={e => setSelectedType(e.target.value)} style={inputS}>
                {typeOptions.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div style={{ flex: "2 1 200px" }}>
              <label style={labelS}>Search</label>
              <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Name or email…" style={inputS} />
            </div>
            <div style={{ flex: "1 1 130px" }}>
              <label style={labelS}>Status</label>
              <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={inputS}>
                <option value="all">All</option>
                <option value="healthy">🟢 Healthy</option>
                <option value="low">🟡 Low</option>
                <option value="no_balance">🔴 No balance</option>
              </select>
            </div>
            <div style={{ flex: "1 1 130px" }}>
              <label style={labelS}>Department</label>
              <select value={deptFilter} onChange={e => setDeptFilter(e.target.value)} style={inputS}>
                <option value="all">All</option>
                {departments.map(d => <option key={d} value={d}>{d}</option>)}
              </select>
            </div>
          </div>

          {/* Table */}
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: "left", color: C.textSub, borderBottom: `1px solid ${C.border}` }}>
                  {["Employee", "Department", "Allocated", "Used", "Remaining", "Status", "Updated", ""].map((h, i) => (
                    <th key={i} style={{ padding: "10px 8px", fontWeight: 700, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={8} style={{ padding: 24, textAlign: "center", color: C.textSub }}>Loading…</td></tr>
                ) : filteredRows.length === 0 ? (
                  <tr><td colSpan={8} style={{ padding: 24, textAlign: "center", color: C.textSub }}>No employees match these filters.</td></tr>
                ) : filteredRows.map(r => (
                  <tr key={r.user_id} style={{ borderBottom: `1px solid ${C.border}` }}>
                    <td style={{ padding: "10px 8px" }}>
                      <div style={{ fontWeight: 700, color: C.text }}>{r.name || "—"}</div>
                      <div style={{ fontSize: 11, color: C.textSub }}>{r.email}</div>
                    </td>
                    <td style={{ padding: "10px 8px", color: C.textSub }}>{r.department || "—"}</td>
                    <td style={{ padding: "10px 8px", fontWeight: 700, color: C.text }}>
                      {r.uncapped && !r.has_override ? "∞" : num(r.allocated)}
                      {r.has_override && <span title="Manually overridden" style={{ marginLeft: 4, color: C.brand }}>•</span>}
                    </td>
                    <td style={{ padding: "10px 8px", color: C.textSub }}>{num(r.used)}</td>
                    <td style={{ padding: "10px 8px", fontWeight: 700, color: (r.uncapped && !r.has_override) || r.remaining > 0 ? C.text : "#B91C1C" }}>
                      {r.uncapped && !r.has_override ? "∞" : num(r.remaining)}
                    </td>
                    <td style={{ padding: "10px 8px" }}><StatusBadge s={r.status} /></td>
                    <td style={{ padding: "10px 8px", color: C.textSub, whiteSpace: "nowrap", fontSize: 11 }}>{r.updated_at ? fmtDateTime(r.updated_at) : "—"}</td>
                    <td style={{ padding: "10px 8px", whiteSpace: "nowrap" }}>
                      <button style={{ ...ghostBtn, marginRight: 6 }} onClick={() => openAdjustFor(r)}>Adjust</button>
                      <button style={ghostBtn} onClick={() => openHistory(r)}>History</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── RIGHT: adjustment form ── */}
        <div style={cardS}>
          <h3 style={{ fontSize: 16, fontWeight: 800, color: C.text, margin: "0 0 4px" }}>Adjust Leave Balance</h3>
          <p style={{ fontSize: 12, color: C.textSub, margin: "0 0 14px" }}>Used days are preserved — only the allocation changes.</p>

          <div style={{ marginBottom: 12 }}>
            <label style={labelS}>Employee *</label>
            <select value={form.user_id} onChange={e => setF({ user_id: e.target.value })} style={inputS}>
              <option value="">Select an employee…</option>
              {rows.map(r => <option key={r.user_id} value={r.user_id}>{r.name || r.email}</option>)}
            </select>
          </div>

          <div style={{ marginBottom: 12 }}>
            <label style={labelS}>Leave Type(s) *</label>
            <button type="button" aria-haspopup="listbox" aria-expanded={typePickerOpen}
              onClick={() => setTypePickerOpen(o => !o)}
              style={{ ...inputS, textAlign: "left", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {form.leave_types.length === 0 ? "Select leave types…"
                  : form.leave_types.length <= 2 ? form.leave_types.join(", ")
                  : `${form.leave_types.length} leave types selected`}
              </span>
              <span aria-hidden="true">{typePickerOpen ? "▲" : "▼"}</span>
            </button>
            {typePickerOpen && (
              <div role="listbox" aria-multiselectable="true"
                style={{ border: `1px solid ${C.border}`, borderRadius: 8, marginTop: 4, background: C.surface, maxHeight: 240, overflowY: "auto" }}>
                <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 10px", borderBottom: `1px solid ${C.border}`, fontSize: 12 }}>
                  <button type="button" onClick={() => setF({ leave_types: [...typeOptions] })}
                    style={{ background: "none", border: "none", color: C.brand, fontWeight: 700, cursor: "pointer", padding: 0, fontFamily: "inherit" }}>Select all</button>
                  <button type="button" onClick={() => setF({ leave_types: [] })}
                    style={{ background: "none", border: "none", color: C.textSub, fontWeight: 700, cursor: "pointer", padding: 0, fontFamily: "inherit" }}>Clear</button>
                </div>
                {typeOptions.map(t => (
                  <label key={t} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", fontSize: 13, color: C.text, cursor: "pointer" }}>
                    <input type="checkbox" checked={form.leave_types.includes(t)} onChange={() => toggleType(t)}
                      style={{ accentColor: C.brand, width: 15, height: 15 }} />
                    {t}
                  </label>
                ))}
              </div>
            )}
          </div>

          {/* Current snapshot — one type only; several types get the table below */}
          {form.user_id && singleType && (
            <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
              {[["Allocated", current?.allocated], ["Used", current?.used], ["Remaining", current?.remaining]].map(([l, v]) => (
                <div key={l} style={{ flex: 1, background: C.bg, borderRadius: 8, padding: "8px 6px", textAlign: "center" }}>
                  <div style={{ fontSize: 10, color: C.textSub, fontWeight: 700 }}>{l}</div>
                  <div style={{ fontSize: 16, fontWeight: 800, color: C.text }}>{current ? num(v) : "—"}</div>
                </div>
              ))}
            </div>
          )}

          <div style={{ marginBottom: 12 }}>
            <label style={labelS}>Adjustment Type *</label>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {ADJUSTMENT_TYPES.map(a => {
                const active = form.adjustment_type === a.v;
                return (
                  <button key={a.v} type="button" title={a.hint}
                    onClick={() => setF({ adjustment_type: a.v })}
                    style={{
                      padding: "9px 8px", borderRadius: 8, fontSize: 12, fontWeight: 700, cursor: "pointer",
                      border: `1.5px solid ${active ? C.brand : C.border}`,
                      background: active ? C.brand : C.surface, color: active ? "#fff" : C.text, fontFamily: "inherit",
                    }}>
                    {a.label}
                  </button>
                );
              })}
            </div>
          </div>

          {form.adjustment_type !== "reset" && (
            <div style={{ marginBottom: 12 }}>
              <label style={labelS}>{form.adjustment_type === "custom" ? "New balance (days) *" : "Days *"}</label>
              <input type="number" min="0" step="0.5" value={form.adjustment_value}
                onChange={e => setF({ adjustment_value: e.target.value })} placeholder="0" style={inputS} />
            </div>
          )}

          {/* Final preview */}
          {form.user_id && current && (
            <div style={{ background: C.bg, border: `1px dashed ${C.border}`, borderRadius: 8, padding: "10px 12px", marginBottom: 12, fontSize: 13, color: C.text }}>
              {form.adjustment_type === "reset"
                ? <>Resets to company default{entitlementFor(singleType) != null ? <> (<strong>{num(entitlementFor(singleType))}</strong> days)</> : ""}.</>
                : finalPreview != null
                  ? <>Final allocation: <strong>{num(finalPreview)}</strong> days · remaining ≈ <strong>{num(Math.max(0, finalPreview - (current.used ?? 0)))}</strong></>
                  : <span style={{ color: C.textSub }}>Enter a value to preview the result.</span>}
            </div>
          )}
          {form.user_id && form.leave_types.length > 1 && (
            <div style={{ background: C.bg, border: `1px dashed ${C.border}`, borderRadius: 8, padding: "8px 10px", marginBottom: 12, fontSize: 12, color: C.text }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }} data-testid="multi-type-preview">
                <thead>
                  <tr style={{ color: C.textSub, textAlign: "left" }}>
                    <th style={{ padding: "3px 4px", fontWeight: 700 }}>Leave type</th>
                    <th style={{ padding: "3px 4px", fontWeight: 700, textAlign: "right" }}>Now</th>
                    <th style={{ padding: "3px 4px", fontWeight: 700, textAlign: "right" }}>After</th>
                  </tr>
                </thead>
                <tbody>
                  {form.leave_types.map(lt => {
                    const after = previewFor(lt);
                    return (
                      <tr key={lt}>
                        <td style={{ padding: "3px 4px" }}>{lt}</td>
                        <td style={{ padding: "3px 4px", textAlign: "right" }}>{num(formBalances?.[lt]?.allocated)}</td>
                        <td style={{ padding: "3px 4px", textAlign: "right", fontWeight: 700 }}>
                          {form.adjustment_type === "reset" ? (after != null ? num(after) : "default") : num(after)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div style={{ marginBottom: 12 }}>
            <label style={labelS}>Reason</label>
            <textarea value={form.reason} onChange={e => setF({ reason: e.target.value })} rows={2}
              placeholder="e.g. Medical emergency, carry-forward, manager approval…"
              style={{ ...inputS, resize: "vertical", minHeight: 56 }} />
          </div>

          <div style={{ marginBottom: 12 }}>
            <label style={labelS}>Effective Date</label>
            <input type="date" value={form.effective_date} onChange={e => setF({ effective_date: e.target.value })} style={inputS} />
          </div>

          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: C.text, marginBottom: 16, cursor: "pointer" }}>
            <input type="checkbox" checked={form.notify} onChange={e => setF({ notify: e.target.checked })} style={{ accentColor: C.brand, width: 15, height: 15 }} />
            Notify employee (in-app)
          </label>

          <button style={{ ...primaryBtn, width: "100%", opacity: canSave() ? 1 : 0.5, cursor: canSave() ? "pointer" : "not-allowed" }}
            disabled={!canSave()} onClick={() => setConfirmOpen(true)}>
            Save Adjustment
          </button>
        </div>
      </div>

      {/* ── Confirmation dialog ── */}
      {confirmOpen && (
        <Modal onClose={() => setConfirmOpen(false)} C={C}>
          <h3 style={{ margin: "0 0 10px", fontSize: 17, fontWeight: 800, color: C.text }}>Confirm adjustment</h3>
          <p style={{ fontSize: 13, color: C.textSub, lineHeight: 1.6, margin: "0 0 16px" }}>
            {(() => {
              const emp = rows.find(r => String(r.user_id) === String(form.user_id));
              const name = emp?.name || emp?.email || "this employee";
              const typesLabel = form.leave_types.join(", ");
              const many = form.leave_types.length > 1;
              if (form.adjustment_type === "reset")
                return <>Reset <strong>{name}</strong>'s <strong>{typesLabel}</strong> allocation{many ? "s" : ""} to the company default?</>;
              const verb = { grant: "Grant", deduct: "Deduct", custom: "Set" }[form.adjustment_type];
              return <>{verb} <strong>{form.adjustment_value} day(s)</strong> {form.adjustment_type === "custom" ? "as" : form.adjustment_type === "grant" ? "to" : "from"} <strong>{name}</strong>'s <strong>{typesLabel}</strong> balance{many ? `s (${form.leave_types.length} types)` : ""}{!many && finalPreview != null ? <> → final <strong>{num(finalPreview)}</strong> days</> : ""}?</>;
            })()}
          </p>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button style={ghostBtn} onClick={() => setConfirmOpen(false)} disabled={saving}>Cancel</button>
            <button style={{ ...primaryBtn, opacity: saving ? 0.6 : 1 }} onClick={submit} disabled={saving}>
              {saving ? "Saving…" : "Confirm"}
            </button>
          </div>
        </Modal>
      )}

      {/* ── History modal ── */}
      {historyFor && (
        <Modal onClose={() => setHistoryFor(null)} C={C} wide>
          <h3 style={{ margin: "0 0 4px", fontSize: 17, fontWeight: 800, color: C.text }}>Adjustment history</h3>
          <p style={{ fontSize: 13, color: C.textSub, margin: "0 0 14px" }}>{historyFor.name}</p>
          {historyLoading ? (
            <div style={{ padding: 20, textAlign: "center", color: C.textSub }}>Loading…</div>
          ) : history.length === 0 ? (
            <div style={{ padding: 20, textAlign: "center", color: C.textSub }}>No adjustments recorded yet.</div>
          ) : (
            <div style={{ overflowX: "auto", maxHeight: 420 }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <thead>
                  <tr style={{ textAlign: "left", color: C.textSub, borderBottom: `1px solid ${C.border}` }}>
                    {["Date", "Leave Type", "Action", "Value", "Prev", "New", "Reason", "By"].map((h, i) => (
                      <th key={i} style={{ padding: "8px 6px", fontWeight: 700, whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {history.map(a => (
                    <tr key={a.id} style={{ borderBottom: `1px solid ${C.border}` }}>
                      <td style={{ padding: "8px 6px", whiteSpace: "nowrap", color: C.textSub }}>{fmtDate(a.created_at)}</td>
                      <td style={{ padding: "8px 6px", color: C.text }}>{a.leave_type}</td>
                      <td style={{ padding: "8px 6px", textTransform: "capitalize", color: C.text }}>{a.adjustment_type}</td>
                      <td style={{ padding: "8px 6px", color: C.text }}>{a.adjustment_value == null ? "—" : num(a.adjustment_value)}</td>
                      <td style={{ padding: "8px 6px", color: C.textSub }}>{a.previous_allocation == null ? "—" : num(a.previous_allocation)}</td>
                      <td style={{ padding: "8px 6px", fontWeight: 700, color: C.text }}>{a.new_allocation == null ? "default" : num(a.new_allocation)}</td>
                      <td style={{ padding: "8px 6px", color: C.textSub, maxWidth: 200 }}>{a.reason || "—"}</td>
                      <td style={{ padding: "8px 6px", color: C.textSub, whiteSpace: "nowrap" }}>{a.created_by_name || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 14 }}>
            <button style={ghostBtn} onClick={() => setHistoryFor(null)}>Close</button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Modal({ children, onClose, C, wide }) {
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000, padding: 16 }}>
      <div onClick={e => e.stopPropagation()} style={{ background: C.surface, borderRadius: 12, border: `1px solid ${C.border}`, padding: 22, width: "100%", maxWidth: wide ? 720 : 440, boxShadow: "0 20px 50px rgba(0,0,0,0.3)" }}>
        {children}
      </div>
    </div>
  );
}
