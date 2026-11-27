/**
 * "Max days per month" for one existing leave type (Admin > Manage Leave Types).
 *
 * Saves through PUT /leave/admin/types/{type}/monthly-limit, which changes ONLY
 * this value — the type's other rules are untouched. Empty or 0 = no monthly
 * limit. `save(leaveType, valueOrNull)` resolves to the server response.
 */
import React, { useEffect, useState } from "react";

const toInput = (v) => (v == null ? "" : String(v));

export default function MonthlyLimitControl({ leaveType, value, save, onSaved, C, T = {} }) {
  const [draft, setDraft]   = useState(toInput(value));
  const [busy, setBusy]     = useState(false);
  const [status, setStatus] = useState(null);   // { ok: bool, text }
  useEffect(() => { setDraft(toInput(value)); }, [value]);

  const parsed  = draft.trim() === "" ? null : Number(draft);
  const invalid = parsed !== null && (Number.isNaN(parsed) || parsed < 0 || parsed > 31);
  const changed = (parsed || null) !== (value || null);

  const submit = async () => {
    if (invalid || !changed) return;
    setBusy(true);
    setStatus(null);
    try {
      const res = await save(leaveType, parsed || null);
      if (res && res.leave_type) {
        setStatus({ ok: true, text: T.saved || "Saved" });
        onSaved?.(res);
      } else {
        setStatus({ ok: false, text: (res && (res.detail || res.message)) || (T.saveFailed || "Save failed") });
      }
    } catch (e) {
      setStatus({ ok: false, text: e?.message || (T.saveFailed || "Save failed") });
    }
    setBusy(false);
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
      <label style={{ fontSize: 11, color: C.sub, fontWeight: 600, whiteSpace: "nowrap" }}
        htmlFor={`ml-${leaveType}`}>
        {T.maxPerMonth || "Max/month"}
      </label>
      <input id={`ml-${leaveType}`} type="number" min={0} max={31} step={0.5}
        value={draft} placeholder={T.noLimit || "No limit"}
        aria-label={`${leaveType} ${T.maxPerMonth || "max days per month"}`}
        onChange={e => { setDraft(e.target.value); setStatus(null); }}
        onKeyDown={e => { if (e.key === "Enter") submit(); }}
        style={{ width: 74, padding: "5px 8px", borderRadius: 6, fontSize: 12, fontFamily: "inherit",
          border: `1px solid ${invalid ? "#EF4444" : C.border}`, background: C.surface, color: C.text }} />
      <button type="button" onClick={submit} disabled={busy || invalid || !changed}
        style={{ padding: "5px 10px", borderRadius: 6, fontSize: 12, fontWeight: 600, fontFamily: "inherit",
          border: `1px solid ${C.border}`, background: C.surface, color: C.text,
          cursor: busy || invalid || !changed ? "not-allowed" : "pointer", opacity: busy || invalid || !changed ? 0.5 : 1 }}>
        {busy ? "…" : (T.save || "Save")}
      </button>
      {status && (
        <span role="status" style={{ fontSize: 11, fontWeight: 600, color: status.ok ? "#047857" : "#B91C1C" }}>
          {status.ok ? "✓ " : ""}{status.text}
        </span>
      )}
    </div>
  );
}
