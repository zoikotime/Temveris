/**
 * Shift Management — the one standalone screen in the Work Schedule feature.
 * -------------------------------------------------------------------------
 * Shifts are org-scoped entities that need create / edit / delete / list, which
 * a key/value settings panel cannot express. Everything else about working
 * schedules stays in Organization Settings → Working Schedule, where it
 * participates in the existing Organization / Team / Employee cascade.
 *
 * Backend: GET/POST /schedule/shifts, PUT/DELETE /schedule/shifts/{id}
 * (app/schedule/router.py) — already org-scoped, 404 on a cross-org id.
 *
 * A shift defined here only takes effect for someone once it is selected in the
 * "Assigned shift" row of Organization Settings at org, team or employee scope.
 * Creating one changes nobody's schedule on its own.
 */
import React, { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../../config";
import { useTheme } from "../../context/ThemeContext";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token") || "";
const authGet = (p) => fetch(`${API_BASE}${p}`, { headers: { Authorization: `Bearer ${token()}` } });
const authSend = (method, p, body) =>
  fetch(`${API_BASE}${p}`, {
    method,
    headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

// 0=Mon … 6=Sun, matching Shift.days_of_week and date.weekday().
const DAYS = [
  { i: 0, s: "Mon" }, { i: 1, s: "Tue" }, { i: 2, s: "Wed" }, { i: 3, s: "Thu" },
  { i: 4, s: "Fri" }, { i: 5, s: "Sat" }, { i: 6, s: "Sun" },
];

const BLANK = { name: "", start_time: "09:00", end_time: "18:00", days_of_week: [0, 1, 2, 3, 4], color: "#3B82F6" };

export default function ShiftManagementTab({ isAdmin, showToast }) {
  const { C } = useTheme();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(null);   // shift id, or "new", or null
  const [form, setForm] = useState(BLANK);

  const load = useCallback(() => {
    setLoading(true);
    authGet("/schedule/shifts")
      .then(r => (r.ok ? r.json() : Promise.reject(r)))
      .then(d => setRows(Array.isArray(d) ? d : []))
      .catch(() => showToast?.("Failed to load shifts", "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => { load(); }, [load]);

  const startNew = () => { setForm(BLANK); setEditing("new"); };
  const startEdit = (s) => {
    setForm({
      name: s.name || "",
      start_time: (s.start_time || "09:00").slice(0, 5),
      end_time: (s.end_time || "18:00").slice(0, 5),
      days_of_week: Array.isArray(s.days_of_week) ? s.days_of_week : [0, 1, 2, 3, 4],
      color: s.color || "#3B82F6",
    });
    setEditing(s.id);
  };
  const cancel = () => { setEditing(null); setForm(BLANK); };

  const toggleDay = (i) => setForm(f => ({
    ...f,
    days_of_week: f.days_of_week.includes(i)
      ? f.days_of_week.filter(d => d !== i)
      : [...f.days_of_week, i].sort((a, b) => a - b),
  }));

  const valid = form.name.trim() && /^\d{2}:\d{2}$/.test(form.start_time)
    && /^\d{2}:\d{2}$/.test(form.end_time);

  const save = async () => {
    if (!valid || saving) return;
    setSaving(true);
    try {
      const body = { ...form, name: form.name.trim(), shift_type: "working" };
      const res = editing === "new"
        ? await authSend("POST", "/schedule/shifts", body)
        : await authSend("PUT", `/schedule/shifts/${editing}`, body);
      if (!res.ok) throw new Error();
      showToast?.(editing === "new" ? "Shift created." : "Shift updated.");
      cancel();
      load();
    } catch { showToast?.("Failed to save shift", "error"); }
    setSaving(false);
  };

  const remove = async (s) => {
    // DELETE deactivates rather than destroying, so an existing assignment that
    // references this shift keeps its history.
    if (!window.confirm(`Deactivate "${s.name}"? Employees assigned to it will fall back to their inherited schedule.`)) return;
    try {
      const res = await authSend("DELETE", `/schedule/shifts/${s.id}`);
      if (!res.ok) throw new Error();
      showToast?.("Shift deactivated.");
      load();
    } catch { showToast?.("Failed to deactivate shift", "error"); }
  };

  const dayLabel = (dow) => {
    const list = Array.isArray(dow) ? dow : [];
    if (!list.length) return "No days";
    if (list.length === 7) return "Every day";
    return DAYS.filter(d => list.includes(d.i)).map(d => d.s).join(", ");
  };

  return (
    <div style={{ padding: "28px 36px", maxWidth: 900 }}>
      <p style={{ fontSize: 14, color: C.textSub, marginBottom: 20, lineHeight: 1.6 }}>
        Define the shifts your organization runs — a fixed day shift, a Mon–Sat schedule,
        or a rotating 24×7 set. A shift only applies to someone once it is chosen in the{" "}
        <b>Assigned shift</b> row under <b>Organization Settings → Working Schedule</b>,
        at organization, team or employee scope. Creating a shift here changes nobody&apos;s
        schedule on its own.
      </p>

      {isAdmin && editing === null && (
        <button onClick={startNew}
          style={{ padding: "10px 18px", borderRadius: 8, border: "none", background: C.brand,
            color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer",
            fontFamily: "inherit", marginBottom: 18 }}>
          + New shift
        </button>
      )}

      {editing !== null && (
        <div style={{ border: `1px solid ${C.border}`, borderRadius: 12, padding: 18,
          background: C.surface, marginBottom: 20 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: C.text, marginBottom: 14 }}>
            {editing === "new" ? "New shift" : "Edit shift"}
          </div>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
            <Field label="Name" C={C}>
              <input value={form.name} placeholder="Morning"
                onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                style={{ ...inputStyle(C), width: 180 }} />
            </Field>
            <Field label="Start" C={C}>
              <input type="time" value={form.start_time}
                onChange={e => setForm(f => ({ ...f, start_time: e.target.value }))}
                style={{ ...inputStyle(C), width: 120 }} />
            </Field>
            <Field label="End" C={C}>
              <input type="time" value={form.end_time}
                onChange={e => setForm(f => ({ ...f, end_time: e.target.value }))}
                style={{ ...inputStyle(C), width: 120 }} />
            </Field>
            <Field label="Colour" C={C}>
              <input type="color" value={form.color}
                onChange={e => setForm(f => ({ ...f, color: e.target.value }))}
                style={{ ...inputStyle(C), width: 56, padding: 3, height: 34 }} />
            </Field>
          </div>

          <div style={{ marginTop: 14 }}>
            <div style={{ fontSize: 11, color: C.textSub, marginBottom: 6, fontWeight: 600 }}>Days</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {DAYS.map(d => {
                const on = form.days_of_week.includes(d.i);
                return (
                  <button key={d.i} onClick={() => toggleDay(d.i)}
                    style={{ padding: "5px 10px", borderRadius: 7, fontSize: 11, fontWeight: 700,
                      fontFamily: "inherit", cursor: "pointer",
                      border: `1px solid ${on ? C.brand : C.border}`,
                      background: on ? C.brandBg : C.surface, color: on ? C.brand : C.textSub }}>
                    {d.s}
                  </button>
                );
              })}
            </div>
          </div>

          {form.end_time <= form.start_time && (
            <div style={{ fontSize: 12, color: C.textSub, marginTop: 12 }}>
              Ends at or before it starts — this is treated as an overnight shift.
            </div>
          )}

          <div style={{ display: "flex", gap: 10, marginTop: 18 }}>
            <button onClick={save} disabled={!valid || saving}
              style={{ padding: "9px 20px", borderRadius: 8, border: "none",
                background: valid ? C.brand : C.textLight, color: "#fff", fontWeight: 700,
                fontSize: 13, cursor: valid && !saving ? "pointer" : "not-allowed",
                fontFamily: "inherit" }}>
              {saving ? "Saving…" : "Save shift"}
            </button>
            <button onClick={cancel} disabled={saving}
              style={{ padding: "9px 16px", borderRadius: 8, border: `1px solid ${C.border}`,
                background: "transparent", color: C.textSub, fontWeight: 600, fontSize: 13,
                cursor: "pointer", fontFamily: "inherit" }}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading && <div style={{ color: C.textSub, padding: 24 }}>Loading…</div>}

      {!loading && !rows.length && (
        <div style={{ color: C.textSub, padding: "24px 4px", fontSize: 14 }}>
          No shifts defined yet. Working schedules still work without one — use the
          shift start and end times under Working Schedule instead.
        </div>
      )}

      {!loading && rows.length > 0 && (
        <div style={{ border: `1px solid ${C.border}`, borderRadius: 12, overflow: "hidden",
          background: C.surface }}>
          {rows.map((s, i) => (
            <div key={s.id} style={{ display: "flex", alignItems: "center", gap: 14,
              padding: "14px 18px", borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}` }}>
              <span style={{ width: 10, height: 10, borderRadius: "50%", flexShrink: 0,
                background: s.color || C.brand }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14, fontWeight: 600, color: C.text }}>
                  {s.name}
                  {s.is_default && (
                    <span style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                      background: C.borderLight, padding: "2px 8px", borderRadius: 10,
                      marginLeft: 8, textTransform: "uppercase", letterSpacing: .3 }}>Default</span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                  {(s.start_time || "").slice(0, 5)}–{(s.end_time || "").slice(0, 5)} · {dayLabel(s.days_of_week)}
                </div>
              </div>
              {isAdmin && (
                <>
                  <button onClick={() => startEdit(s)}
                    style={linkBtn(C.brand)}>Edit</button>
                  <button onClick={() => remove(s)}
                    style={linkBtn(C.textSub)}>Deactivate</button>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {!isAdmin && (
        <p style={{ fontSize: 12, color: C.textSub, marginTop: 12 }}>
          You have read-only access to shifts.
        </p>
      )}
    </div>
  );
}

function Field({ label, children, C }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: C.textSub, marginBottom: 5, fontWeight: 600 }}>{label}</div>
      {children}
    </div>
  );
}

const inputStyle = (C) => ({
  padding: "8px 12px", border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 13,
  color: C.text, background: C.surface, outline: "none", fontFamily: "inherit",
});

const linkBtn = (color) => ({
  background: "none", border: "none", color, cursor: "pointer", fontSize: 12,
  fontWeight: 600, fontFamily: "inherit", padding: 0, whiteSpace: "nowrap",
});
