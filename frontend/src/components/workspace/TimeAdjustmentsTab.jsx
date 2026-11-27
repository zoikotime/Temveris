import { orgToday } from "../../lib/orgTime";
// TimeAdjustmentsTab — admin tab in Workspace Settings that lets the
// workspace owner credit work-time back to an employee when the tracker
// lost it (power cut, system crash, network outage). Adds rows to the
// `time_adjustments` table via /employees/{id}/time-adjustments; reports
// + day-summary fold these into worked-hours totals automatically.
//
// Permission: admin-only (HR cannot write), per 2026-05-13 governance.
// The backend enforces this — this UI just renders informative copy when
// the caller isn't an admin.
//
// Layout:
//   • Header   — title + intro copy + total-employee count + total minutes added today
//   • Body     — Left: searchable employee list. Right: recent adjustments feed.
//   • Modal    — opens when an employee row is clicked; date picker + h/m + reason.
//
// Visual language matches the rest of Workspace Settings: theme-aware
// (light/dark), brand navy + emerald, glassmorphism cards.

import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { useTheme } from "../../context/ThemeContext";
import { API_BASE } from "../../config";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token()}` },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
  return res.json();
}
async function apiSend(method, path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
  return res.json();
}

function todayISO() {
  // The ORGANIZATION's calendar day — which is how the backend keys
  // adjustment_date (app/_tz.py). This hard-coded a +330-minute IST offset
  // while claiming to match the backend; it only did for one tenant.
  return orgToday();
}

function formatDuration(secs) {
  secs = Math.max(0, parseInt(secs || 0, 10));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

function timeAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!t) return "";
  const secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86_400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86_400)}d ago`;
}

export default function TimeAdjustmentsTab({ isAdmin, showToast }) {
  const { C, isDark } = useTheme();
  const [employees, setEmployees] = useState([]);
  const [recent, setRecent] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState(null); // selected employee for the modal
  const [reverting, setReverting] = useState(null); // id being reverted

  const load = useCallback(async () => {
    setLoading(true);
    // Load employees + recent adjustments independently so that one failure
    // (e.g. /time-adjustments/recent returning 500 on a fresh deploy before
    // the migration has been applied) doesn't blank out the other pane.
    const [empsR, adjR] = await Promise.allSettled([
      apiGet("/employees"),
      apiGet("/employees/time-adjustments/recent?days=14&limit=100"),
    ]);
    if (empsR.status === "fulfilled") {
      const v = empsR.value;
      setEmployees(Array.isArray(v) ? v : (v?.items || []));
    } else {
      showToast?.(empsR.reason?.message || "Could not load employees", "error");
    }
    if (adjR.status === "fulfilled") {
      setRecent((adjR.value && adjR.value.items) || []);
    } else {
      // Most likely the backend hasn't been restarted since the migration
      // landed — surface that explicitly so it's not mistaken for an empty
      // history.
      const msg = adjR.reason?.message || "Could not load adjustments";
      showToast?.(
        msg.includes("500")
          ? "Adjustments endpoint failed — has the backend been restarted after the migration?"
          : msg,
        "error",
      );
      setRecent([]);
    }
    setLoading(false);
  }, [showToast]);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return employees;
    return employees.filter((e) =>
      [(e.name || ""), (e.email || ""), (e.department || ""), (e.designation || "")]
        .join(" ").toLowerCase().includes(q),
    );
  }, [employees, search]);

  const todaysAdjustmentSecs = useMemo(() => {
    const today = todayISO();
    return recent
      .filter((r) => r.is_active && r.date === today)
      .reduce((acc, r) => acc + (r.seconds_added || 0), 0);
  }, [recent]);

  const onRevert = async (adj) => {
    if (!confirm(
      `Revert ${adj.duration} added to ${adj.employee_name || "this employee"} on ${adj.date}?\n\n` +
      `Reason was: "${adj.reason}"\n\nThe employee will be emailed about the revert.`
    )) return;
    setReverting(adj.id);
    try {
      await apiSend("DELETE", `/employees/time-adjustments/${adj.id}`);
      showToast?.("Adjustment reverted", "success");
      load();
    } catch (e) {
      showToast?.(e.message || "Could not revert", "error");
    } finally {
      setReverting(null);
    }
  };

  if (!isAdmin) {
    return (
      <div style={{ padding: 40, maxWidth: 600 }}>
        <div style={{
          background: C.bg, border: `1px solid ${C.border}`,
          borderRadius: 12, padding: 24, color: C.text,
        }}>
          <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>Admin-only</h3>
          <p style={{ margin: "10px 0 0", fontSize: 13, color: C.textSub, lineHeight: 1.6 }}>
            Time adjustments can only be added or reverted by the workspace owner.
            If an employee lost work time to a tracker outage, ask your admin to
            credit it from this tab.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: "28px 32px 60px", color: C.text }}>
      {/* ═════ HEADER ═════ */}
      <div style={{ marginBottom: 22 }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, letterSpacing: -0.2 }}>
          Time Adjustments
        </h2>
        <p style={{ margin: "8px 0 0", color: C.textSub, fontSize: 13, lineHeight: 1.6, maxWidth: 760 }}>
          Credit work-time back to an employee when the tracker missed it — power cuts,
          system crashes, network outages, anything genuinely worked but not recorded.
          Original tracker history stays untouched; only worked-hours totals update.
          Every change is audited and the employee receives an email.
        </p>
      </div>

      {/* ═════ STAT CARDS ═════ */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
        gap: 14, marginBottom: 22,
      }}>
        <StatCard C={C} label="Employees" value={employees.length} accent={C.brand} />
        <StatCard C={C} label="Adjustments (14d)" value={recent.filter(r => r.is_active).length}
                  accent="#44B97B" />
        <StatCard C={C} label="Added Today" value={formatDuration(todaysAdjustmentSecs)}
                  accent="#F59E0B" />
      </div>

      {/* ═════ TWO-COLUMN LAYOUT ═════ */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "minmax(0,1.1fr) minmax(0,1fr)",
        gap: 18,
      }}>
        {/* ── LEFT: employee list ─────────────────────────────────────── */}
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 12, overflow: "hidden",
        }}>
          <div style={{
            padding: "14px 16px",
            borderBottom: `1px solid ${C.borderLight}`,
            display: "flex", gap: 12, alignItems: "center",
          }}>
            <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
              Pick an employee
            </span>
            <span style={{ fontSize: 11, color: C.textSub }}>
              ({filtered.length} of {employees.length})
            </span>
            <div style={{ flex: 1 }} />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name, email, role…"
              style={{
                padding: "7px 10px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: C.bg,
                color: C.text, fontSize: 12, outline: "none", minWidth: 220,
                fontFamily: "inherit",
              }}
            />
          </div>
          <div style={{ maxHeight: 540, overflowY: "auto" }}>
            {loading ? (
              <div style={{ padding: 24, fontSize: 13, color: C.textSub }}>Loading…</div>
            ) : filtered.length === 0 ? (
              <div style={{ padding: 24, fontSize: 13, color: C.textSub }}>
                No employees match your search.
              </div>
            ) : filtered.map((emp) => (
              <button
                key={emp.id}
                type="button"
                onClick={() => setPicked(emp)}
                style={{
                  width: "100%", display: "flex", alignItems: "center", gap: 12,
                  padding: "12px 16px",
                  border: "none", borderBottom: `1px solid ${C.borderLight}`,
                  background: "transparent", textAlign: "left", cursor: "pointer",
                  fontFamily: "inherit",
                }}
                onMouseEnter={(e) => e.currentTarget.style.background = C.bg}
                onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}
              >
                <Avatar name={emp.name || emp.email} C={C} />
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: C.text,
                                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {emp.name || emp.email}
                  </span>
                  <span style={{ display: "block", fontSize: 11, color: C.textSub,
                                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {emp.designation || emp.department || emp.email}
                  </span>
                </span>
                <span style={{
                  padding: "5px 10px", borderRadius: 6,
                  background: C.brandBg || "rgba(17,73,106,0.10)",
                  color: C.brand, fontSize: 11, fontWeight: 700,
                }}>
                  + Add Time
                </span>
              </button>
            ))}
          </div>
        </div>

        {/* ── RIGHT: recent adjustments ───────────────────────────────── */}
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 12, overflow: "hidden",
        }}>
          <div style={{
            padding: "14px 16px",
            borderBottom: `1px solid ${C.borderLight}`,
            display: "flex", justifyContent: "space-between", alignItems: "center",
          }}>
            <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
              Recent Adjustments
            </span>
            <span style={{ fontSize: 11, color: C.textSub }}>last 14 days</span>
          </div>
          <div style={{ maxHeight: 540, overflowY: "auto" }}>
            {loading ? (
              <div style={{ padding: 24, fontSize: 13, color: C.textSub }}>Loading…</div>
            ) : recent.length === 0 ? (
              <div style={{ padding: 24, fontSize: 13, color: C.textSub, lineHeight: 1.6 }}>
                No time adjustments yet. When you credit time to an employee, it'll
                show up here with an audit trail.
              </div>
            ) : recent.map((adj) => (
              <div key={adj.id} style={{
                padding: "14px 16px",
                borderBottom: `1px solid ${C.borderLight}`,
                opacity: adj.is_active ? 1 : 0.55,
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                  <span style={{
                    padding: "3px 8px", borderRadius: 6,
                    background: adj.is_active ? "rgba(68,185,123,0.14)" : "rgba(148,163,184,0.18)",
                    color: adj.is_active ? "#10854b" : C.textSub,
                    fontSize: 11, fontWeight: 700, letterSpacing: 0.2,
                  }}>
                    {adj.is_active ? `+ ${adj.duration}` : `Reverted`}
                  </span>
                  <span style={{ fontSize: 12, fontWeight: 600, color: C.text }}>
                    {adj.employee_name || `User #${adj.user_id}`}
                  </span>
                  <span style={{ fontSize: 11, color: C.textSub }}>·</span>
                  <span style={{ fontSize: 11, color: C.textSub }}>{adj.date}</span>
                  <div style={{ flex: 1 }} />
                  <span style={{ fontSize: 10, color: C.textSub }}>{timeAgo(adj.created_at)}</span>
                </div>
                <div style={{
                  fontSize: 12, color: C.textSub, lineHeight: 1.5,
                  paddingLeft: 4, marginBottom: 8,
                  textDecoration: adj.is_active ? "none" : "line-through",
                }}>
                  "{adj.reason}"
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: C.textSub }}>
                  <span>Added by {adj.created_by_name || "Admin"}</span>
                  <div style={{ flex: 1 }} />
                  {adj.is_active && (
                    <button
                      type="button"
                      disabled={reverting === adj.id}
                      onClick={() => onRevert(adj)}
                      style={{
                        padding: "4px 10px", borderRadius: 6,
                        border: `1px solid ${C.border}`, background: "transparent",
                        color: C.textSub, fontSize: 11, cursor: "pointer",
                        fontFamily: "inherit",
                      }}>
                      {reverting === adj.id ? "Reverting…" : "Revert"}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ═════ ADD-TIME MODAL ═════ */}
      {picked && (
        <AddTimeModal
          C={C} isDark={isDark}
          employee={picked}
          onClose={() => setPicked(null)}
          onSaved={(newAdj) => {
            setPicked(null);
            // Optimistic prepend so the user sees the new row instantly.
            setRecent((prev) => [{
              ...newAdj,
              employee_name: picked.name,
              employee_email: picked.email,
              employee_department: picked.department,
            }, ...prev]);
            showToast?.("Time added", "success");
            load();
          }}
          onError={(msg) => showToast?.(msg, "error")}
        />
      )}
    </div>
  );
}

// ─── COMPONENTS ─────────────────────────────────────────────────────────────

function StatCard({ C, label, value, accent }) {
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 12, padding: "16px 18px",
      display: "flex", justifyContent: "space-between", alignItems: "center",
    }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 11, color: C.textSub, textTransform: "uppercase",
                      letterSpacing: 0.6, fontWeight: 700, marginBottom: 4 }}>
          {label}
        </div>
        <div style={{ fontSize: 24, fontWeight: 700, color: C.text, lineHeight: 1.1 }}>
          {value}
        </div>
      </div>
      <div style={{
        width: 38, height: 38, borderRadius: 10,
        background: `${accent}22`, display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <span style={{ width: 10, height: 10, borderRadius: "50%", background: accent,
                       boxShadow: `0 0 0 0 ${accent}66`, animation: "ztpulse 1.8s infinite" }} />
      </div>
      <style>{`@keyframes ztpulse{0%{box-shadow:0 0 0 0 ${accent}66}70%{box-shadow:0 0 0 8px ${accent}00}100%{box-shadow:0 0 0 0 ${accent}00}}`}</style>
    </div>
  );
}

function Avatar({ name, C }) {
  const initials = useMemo(() => {
    const parts = String(name || "?").trim().split(/\s+/);
    return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
  }, [name]);
  return (
    <div style={{
      width: 34, height: 34, borderRadius: "50%",
      background: C.brandBg || "rgba(17,73,106,0.12)",
      color: C.brand, display: "flex", alignItems: "center", justifyContent: "center",
      fontSize: 12, fontWeight: 700, flexShrink: 0,
    }}>
      {initials}
    </div>
  );
}

function AddTimeModal({ C, isDark, employee, onClose, onSaved, onError }) {
  const today = todayISO();
  const [date, setDate] = useState(today);
  const [hours, setHours] = useState(0);
  const [minutes, setMinutes] = useState(30);
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const reasonRef = useRef(null);

  const totalSecs = hours * 3600 + minutes * 60;
  const reasonOK = reason.trim().length >= 4;
  const dateOK = !!date && date <= today;
  const canSave = !saving && totalSecs > 0 && totalSecs <= 12 * 3600 && reasonOK && dateOK;

  useEffect(() => {
    const t = setTimeout(() => reasonRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, []);

  // Close on Esc
  useEffect(() => {
    const h = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [onClose]);

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      const created = await apiSend("POST", `/employees/${employee.id}/time-adjustments`, {
        date, hours, minutes, reason: reason.trim(),
      });
      onSaved(created);
    } catch (e) {
      onError(e.message || "Could not save adjustment");
      setSaving(false);
    }
  };

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 4000,
      background: "rgba(15,23,42,0.55)", backdropFilter: "blur(2px)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: 20,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: C.surface, color: C.text,
        width: "100%", maxWidth: 520, borderRadius: 14,
        border: `1px solid ${C.border}`,
        boxShadow: "0 24px 64px rgba(0,0,0,0.32)",
        overflow: "hidden", fontFamily: "inherit",
      }}>
        {/* Header */}
        <div style={{
          padding: "18px 22px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "center", gap: 14,
        }}>
          <Avatar name={employee.name || employee.email} C={C} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 11, color: C.textSub, fontWeight: 700,
                          letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 2 }}>
              Add work time for
            </div>
            <div style={{ fontSize: 15, fontWeight: 700, color: C.text,
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {employee.name || employee.email}
            </div>
            <div style={{ fontSize: 11, color: C.textSub,
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {employee.designation || employee.department || employee.email}
            </div>
          </div>
          <button type="button" onClick={onClose} style={{
            border: "none", background: "transparent", color: C.textSub,
            cursor: "pointer", padding: 6, fontSize: 18, fontFamily: "inherit",
          }}>×</button>
        </div>

        {/* Body */}
        <div style={{ padding: "20px 22px" }}>
          <Field label="Date">
            <input
              type="date"
              value={date}
              max={today}
              onChange={(e) => setDate(e.target.value)}
              style={inputStyle(C)}
            />
            {!dateOK && (
              <div style={{ fontSize: 11, color: "#dc2626", marginTop: 4 }}>
                Date must be today or earlier.
              </div>
            )}
          </Field>

          <Field label="Time to credit">
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <Spinner C={C} value={hours}   onChange={setHours}   min={0} max={12} unit="h" />
              <Spinner C={C} value={minutes} onChange={setMinutes} min={0} max={59} step={5} unit="m" />
              <div style={{
                marginLeft: "auto",
                padding: "8px 12px", borderRadius: 8,
                background: C.bg, color: C.brand,
                fontSize: 13, fontWeight: 700,
                border: `1px dashed ${C.border}`,
              }}>
                = {formatDuration(totalSecs)}
              </div>
            </div>
            {totalSecs > 12 * 3600 && (
              <div style={{ fontSize: 11, color: "#dc2626", marginTop: 6 }}>
                Single entry capped at 12 hours. Split a longer correction into multiple entries.
              </div>
            )}
            {/* Quick presets — common power-cut durations. */}
            <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
              {[
                ["15m", 0, 15], ["30m", 0, 30], ["1h", 1, 0],
                ["1h 30m", 1, 30], ["2h", 2, 0], ["3h", 3, 0],
              ].map(([lbl, h, m]) => (
                <button key={lbl} type="button"
                  onClick={() => { setHours(h); setMinutes(m); }}
                  style={{
                    padding: "5px 10px", borderRadius: 6,
                    border: `1px solid ${C.border}`, background: "transparent",
                    color: C.textSub, fontSize: 11, fontFamily: "inherit", cursor: "pointer",
                  }}>{lbl}</button>
              ))}
            </div>
          </Field>

          <Field label="Reason (visible to the employee)">
            <textarea
              ref={reasonRef}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. Power cut between 11:00 and 13:00 — verified by team lead"
              rows={3}
              maxLength={500}
              style={{ ...inputStyle(C), resize: "vertical", lineHeight: 1.5, padding: "9px 11px" }}
            />
            <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
              <span style={{ fontSize: 10, color: reasonOK ? C.textSub : "#dc2626" }}>
                {reasonOK ? "Looks good." : "At least 4 characters."}
              </span>
              <span style={{ fontSize: 10, color: C.textSub }}>{reason.length} / 500</span>
            </div>
          </Field>
        </div>

        {/* Footer */}
        <div style={{
          padding: "14px 22px", borderTop: `1px solid ${C.borderLight}`,
          display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12,
        }}>
          <span style={{ fontSize: 11, color: C.textSub, lineHeight: 1.5, maxWidth: 280 }}>
            The employee will receive an email about this addition. You can revert later.
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" onClick={onClose} style={{
              padding: "9px 16px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: "transparent",
              color: C.text, fontSize: 13, fontWeight: 600,
              cursor: "pointer", fontFamily: "inherit",
            }}>Cancel</button>
            <button
              type="button"
              disabled={!canSave}
              onClick={save}
              style={{
                padding: "9px 18px", borderRadius: 8,
                border: "none",
                background: canSave ? C.brand : C.border,
                color: "#fff", fontSize: 13, fontWeight: 700,
                cursor: canSave ? "pointer" : "not-allowed",
                fontFamily: "inherit", letterSpacing: 0.2,
              }}>
              {saving ? "Adding…" : `Add ${formatDuration(totalSecs)}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <label style={{ display: "block", fontSize: 11, fontWeight: 700,
                       letterSpacing: 0.4, textTransform: "uppercase",
                       marginBottom: 6, opacity: 0.78 }}>
        {label}
      </label>
      {children}
    </div>
  );
}

function inputStyle(C) {
  return {
    width: "100%", padding: "9px 11px",
    borderRadius: 8, border: `1px solid ${C.border}`,
    background: C.bg, color: C.text,
    fontSize: 13, outline: "none", fontFamily: "inherit",
    boxSizing: "border-box",
  };
}

function Spinner({ C, value, onChange, min, max, step = 1, unit }) {
  const dec = () => onChange(Math.max(min, value - step));
  const inc = () => onChange(Math.min(max, value + step));
  return (
    <div style={{
      display: "flex", alignItems: "center",
      border: `1px solid ${C.border}`, borderRadius: 8,
      background: C.bg, overflow: "hidden",
    }}>
      <SpinBtn C={C} onClick={dec} disabled={value <= min}>−</SpinBtn>
      <div style={{
        minWidth: 64, textAlign: "center",
        padding: "8px 4px", fontSize: 16, fontWeight: 700, color: C.text,
      }}>
        {value}<span style={{ marginLeft: 3, fontSize: 11, color: C.textSub, fontWeight: 600 }}>{unit}</span>
      </div>
      <SpinBtn C={C} onClick={inc} disabled={value >= max}>+</SpinBtn>
    </div>
  );
}

function SpinBtn({ C, onClick, disabled, children }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} style={{
      width: 30, height: 36, border: "none",
      background: "transparent", color: disabled ? C.borderLight : C.textSub,
      fontSize: 16, fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer",
      fontFamily: "inherit",
    }}>{children}</button>
  );
}
