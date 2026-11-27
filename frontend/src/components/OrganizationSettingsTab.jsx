/**
 * Organization Settings — 3-tier settings cascade admin UI.
 * ---------------------------------------------------------
 * Edit any setting at Organization / Team / Employee scope. Each row shows
 * whether the value is overridden at the chosen scope or inherited from its
 * parent (employee → team → org → system default), and lets an admin reset an
 * override back to the inherited value.
 *
 * Backend: GET/PUT/DELETE /settings/scoped  (app/settings/router.py). The
 * cascade itself resolves in app/settings/scope.py; every consumer that polls
 * GET /settings (desktop tracker, dashboards) picks up the effective value.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "../config";
import { useTheme } from "../context/ThemeContext";
import SearchableSelect from "./SearchableSelect";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token") || "";
const authGet = (p) => fetch(`${API_BASE}${p}`, { headers: { Authorization: `Bearer ${token()}` } });
const authSend = (method, p, body) =>
  fetch(`${API_BASE}${p}`, {
    method,
    headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// ── Request diagnostics ─────────────────────────────────────────────────────
// Saving fires up to two requests (PUT /employees/{id} for employment type,
// PUT /settings/scoped for everything else). Both used to fail with a bare
// `throw new Error()`, so one generic "Failed to save settings" toast covered
// both — no URL, no status, no server message. That is not enough to tell a
// 403 from a 422 from a 500, which is exactly what we need to know.
//
// Every write now throws a RequestError carrying the method, URL, status and
// the response body. Diagnostics only: no request is retried, reshaped, or
// suppressed, and no backend behaviour changes.
class RequestError extends Error {
  constructor({ method, url, status, statusText, body }) {
    super(`${method} ${url} -> ${status} ${statusText || ""}${body ? ` :: ${body}` : ""}`);
    this.name = "RequestError";
    this.method = method;
    this.url = url;
    this.status = status;
    this.statusText = statusText;
    this.body = body;
  }
}

// Read the body ONCE (a Response body can only be consumed a single time).
// Prefer JSON because FastAPI puts the reason in `detail`, but fall back to raw
// text so an HTML error page, a proxy response, or an empty body is still shown
// rather than swallowed.
const readBody = async (res) => {
  try {
    const raw = await res.text();
    if (!raw) return "<empty body>";
    try { return JSON.stringify(JSON.parse(raw)); } catch { return raw; }
  } catch (e) {
    return `<body unreadable: ${e?.message || e}>`;
  }
};

const sendChecked = async (method, p, body) => {
  const url = `${API_BASE}${p}`;
  let res;
  try {
    res = await authSend(method, p, body);
  } catch (netErr) {
    // fetch() rejects only on network/CORS/DNS failure — there is no HTTP
    // status in that case, so status 0 distinguishes it from a server reply.
    throw new RequestError({
      method, url, status: 0, statusText: "network/CORS failure",
      body: netErr?.message || String(netErr),
    });
  }
  if (!res.ok) {
    throw new RequestError({
      method, url, status: res.status, statusText: res.statusText,
      body: await readBody(res),
    });
  }
  return res;
};

// Full object and stack to the console; a single-line summary in the toast,
// because the toast is a plain div and will collapse newlines.
const reportError = (showToast, label, err) => {
  console.error(
    `[OrganizationSettings] ${label}`,
    { name: err?.name, message: err?.message, method: err?.method, url: err?.url,
      status: err?.status, statusText: err?.statusText, body: err?.body },
    "\nstack:\n", err?.stack || "(no stack)",
    "\nerror object:", err,
  );
  const detail = err instanceof RequestError
    ? `${err.method} ${err.url} → ${err.status} ${err.statusText || ""} :: ${String(err.body).slice(0, 300)}`
    : `${err?.name || "Error"}: ${err?.message || String(err)}`;
  showToast?.(`${label} — ${detail} (full stack in console)`, "error");
};

const SCOPES = [
  { k: "org",      label: "Organization", icon: "🏢", hint: "Applies to everyone unless overridden below." },
  { k: "team",     label: "Team",         icon: "👥", hint: "Overrides the organization default for one team." },
  { k: "employee", label: "Employee",     icon: "👤", hint: "Overrides team & org defaults for one person." },
];

// ── local themed primitives (the SettingsPage ones aren't exported) ───────────
function Switch({ on, onChange, disabled, C }) {
  return (
    <button onClick={() => !disabled && onChange(!on)} disabled={disabled}
      style={{ width: 44, height: 24, borderRadius: 12, border: "none", flexShrink: 0,
        cursor: disabled ? "not-allowed" : "pointer", background: on ? C.brand : C.textLight,
        position: "relative", transition: "background .2s" }}>
      <span style={{ position: "absolute", top: 2, left: on ? 22 : 2, width: 20, height: 20,
        background: "#fff", borderRadius: "50%", transition: "left .2s", boxShadow: "0 1px 4px rgba(0,0,0,.25)" }} />
    </button>
  );
}

export default function OrganizationSettingsTab({ isAdmin, showToast }) {
  const { C } = useTheme();
  const [scope, setScope]       = useState("org");
  const [team, setTeam]         = useState("");   // team NAME (== User.team membership)
  const [userId, setUserId]     = useState("");
  const [teams, setTeams]       = useState([]);   // distinct team names
  const [employees, setEmployees] = useState([]);
  const [data, setData]         = useState(null);     // { keys:[], group_order:[] }
  const [drafts, setDrafts]     = useState({});       // key -> new value (dirty)
  const [loading, setLoading]   = useState(false);
  const [saving, setSaving]     = useState(false);
  const [collapsed, setCollapsed] = useState({});
  const [search, setSearch]     = useState("");
  // Options for the "Assigned shift" row. Shifts are org-scoped entities, not a
  // static enum, so the `shift_ref` control needs them fetched. The standalone
  // Shift Management screen was removed, so this lists whatever shifts an org
  // already had; "None" plus the shift start/end rows below cover the rest.
  const [shifts, setShifts]     = useState([]);

  // Selector data (employees + distinct team names). Team membership is the
  // User.team string, so teams come from the roster (unioned with any
  // teams-table names) rather than a teams.id list.
  //
  // `?active_only=true` is what keeps deactivated accounts out of the picker.
  // The unfiltered GET /employees deliberately includes them (Workers & Users
  // renders an Inactive badge; the offboarding directory resolves former
  // workers), so asking for the plain list put every deactivated worker in this
  // dropdown as a selectable settings target.
  const loadRoster = useCallback(() => {
    authGet("/employees?active_only=true").then(r => r.json()).then(emps => {
      if (!Array.isArray(emps)) return;
      // Defence in depth, not the fix: an older backend (or a proxy serving a
      // cached response) that ignores active_only must still not repopulate the
      // dropdown with revoked accounts.
      const selectable = emps.filter(e =>
        e && e.is_active !== false && String(e.role || "").toLowerCase() !== "super_admin");
      setEmployees([...selectable].sort((a, b) => (a.name || "").localeCompare(b.name || "")));
      // Drop a stale selection — e.g. the employee was deactivated in another
      // tab while this one sat open. Without this the panel keeps showing (and
      // saving) overrides for someone who is no longer selectable.
      setUserId(prev => (prev && !selectable.some(e => String(e.id) === String(prev)) ? "" : prev));
      const names = new Set(selectable.map(e => (e.team || "").trim()).filter(Boolean));
      authGet("/org/teams").then(r => r.json()).then(ts => {
        if (Array.isArray(ts)) ts.forEach(t => t.name && names.add(t.name.trim()));
      }).catch(() => {}).finally(() => setTeams([...names].sort()));
    }).catch(() => {});
  }, []);

  useEffect(() => {
    loadRoster();
    // Shift options for the `shift_ref` control. Failure is non-fatal — the row
    // then offers only "None", and every other setting still renders.
    authGet("/schedule/shifts").then(r => r.json()).then(rows => {
      if (Array.isArray(rows)) setShifts(rows);
    }).catch(() => {});
  }, [loadRoster]);

  // Re-read the roster whenever the Employee scope is opened. The tab unmounts
  // when you navigate away, so a fresh mount already refetches — but switching
  // Organization → Team → Employee is pure local state, and a worker deleted
  // elsewhere in the meantime would otherwise stay in the list until reload.
  useEffect(() => {
    if (scope === "employee") loadRoster();
  }, [scope, loadRoster]);

  const load = useCallback(() => {
    // Don't fetch until the scope target is chosen.
    if (scope === "team" && !team) { setData(null); return; }
    if (scope === "employee" && !userId) { setData(null); return; }
    setLoading(true);
    const qs = new URLSearchParams({ scope });
    if (scope === "team") qs.set("team", team);
    if (scope === "employee") qs.set("user_id", userId);
    authGet(`/settings/scoped?${qs.toString()}`)
      .then(r => r.ok ? r.json() : Promise.reject(r))
      .then(d => { setData(d); setDrafts({}); })
      .catch(() => showToast?.("Failed to load settings", "error"))
      .finally(() => setLoading(false));
  }, [scope, team, userId, showToast]);

  useEffect(() => { load(); }, [load]);

  // Employment type is the one field that does NOT live in the settings cascade
  // at Employee scope. It is a column on the employee record, already read by the
  // risk engine and the reports layer, so honouring a settings override here would
  // create a second source of truth for the same fact (Approach A). At Org/Team
  // scope it behaves as a normal cascade key — the default for new hires.
  const EMPLOYMENT_TYPE_KEY = "schedule.employment_type";
  const isEmploymentTypeOnEmployee = (row) =>
    row.key === EMPLOYMENT_TYPE_KEY && scope === "employee";

  const selectedEmployee = employees.find(e => String(e.id) === String(userId));

  // Option rows for the employee picker. `label` is what the admin reads; the
  // wider `search` text is what they can type against.
  const employeeOptions = useMemo(() => employees.map(u => ({
    value: String(u.id),
    label: `${u.name || u.email}${u.team ? ` — ${u.team}` : ""}`,
    search: [u.name, u.email, u.team, u.department, u.designation]
      .filter(Boolean).join(" "),
  })), [employees]);

  // effective value shown in a control: draft > override value > inherited parent
  const shown = (row) => {
    if (row.key in drafts) return drafts[row.key];
    if (isEmploymentTypeOnEmployee(row)) {
      return selectedEmployee?.employment_type || "full_time";
    }
    return row.is_override ? row.value : row.parent_value;
  };

  const setDraft = (key, val) => setDrafts(d => ({ ...d, [key]: String(val) }));

  const dirtyKeys = Object.keys(drafts);
  const canEdit = isAdmin && !(scope === "team" && !team) && !(scope === "employee" && !userId);

  const scopedBody = (extra) => ({
    scope,
    team: scope === "team" ? team : null,
    user_id: scope === "employee" ? Number(userId) : null,
    ...extra,
  });

  const save = async () => {
    if (!dirtyKeys.length || saving) return;
    setSaving(true);
    try {
      // Employment type at Employee scope goes to the employee record, not to a
      // settings row — see the Approach A note above. Everything else takes the
      // normal scoped-settings path, so a mixed save still works.
      const settingsDrafts = { ...drafts };
      let empTypeChange = null;
      if (scope === "employee" && EMPLOYMENT_TYPE_KEY in settingsDrafts) {
        empTypeChange = settingsDrafts[EMPLOYMENT_TYPE_KEY];
        delete settingsDrafts[EMPLOYMENT_TYPE_KEY];
      }

      if (empTypeChange !== null) {
        await sendChecked("PUT", `/employees/${Number(userId)}`,
                          { employment_type: empTypeChange });
        // Keep the local roster in step so the row shows the saved value without
        // a full refetch.
        setEmployees(list => list.map(e =>
          String(e.id) === String(userId) ? { ...e, employment_type: empTypeChange } : e));
      }
      if (Object.keys(settingsDrafts).length) {
        await sendChecked("PUT", "/settings/scoped", scopedBody({ settings: settingsDrafts }));
      }
      showToast?.(`Saved ${dirtyKeys.length} setting(s) at ${scope} scope.`);
      load();
    } catch (err) { reportError(showToast, "Failed to save settings", err); }
    setSaving(false);
  };

  const resetKey = async (key) => {
    try {
      await sendChecked("DELETE", "/settings/scoped", scopedBody({ keys: [key] }));
      showToast?.("Reset to inherited value.");
      setDrafts(d => { const n = { ...d }; delete n[key]; return n; });
      load();
    } catch (err) { reportError(showToast, "Failed to reset", err); }
  };

  // group -> rows, filtered by search
  const grouped = useMemo(() => {
    if (!data?.keys) return [];
    const q = search.trim().toLowerCase();
    const byGroup = {};
    for (const row of data.keys) {
      if (q && !row.label.toLowerCase().includes(q) && !row.key.toLowerCase().includes(q)) continue;
      (byGroup[row.group] ||= []).push(row);
    }
    const order = data.group_order || Object.keys(byGroup);
    return order.filter(g => byGroup[g]?.length).map(g => ({ group: g, rows: byGroup[g] }));
  }, [data, search]);

  // ── render helpers ──────────────────────────────────────────────────────────
  const control = (row) => {
    const v = shown(row);
    const disabled = !canEdit;
    if (row.type === "bool") {
      return <Switch on={v === "true" || v === true} onChange={b => setDraft(row.key, b)} disabled={disabled} C={C} />;
    }
    // Working days. Stored as a JSON array string so it travels through the same
    // Dict[str,str] settings payload as every other key — no API change.
    if (row.type === "multiselect") {
      const selected = parseList(v);
      const toggle = (opt) => {
        const next = selected.includes(opt)
          ? selected.filter(o => o !== opt)
          : [...(row.options || []).filter(o => selected.includes(o) || o === opt)];
        setDraft(row.key, JSON.stringify(next));
      };
      return (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end", maxWidth: 320 }}>
          {(row.options || []).map(o => {
            const on = selected.includes(o);
            return (
              <button key={o} onClick={() => !disabled && toggle(o)} disabled={disabled}
                title={row.option_labels?.[o] || o}
                style={{ padding: "5px 9px", borderRadius: 7, fontSize: 11, fontWeight: 700,
                  fontFamily: "inherit", cursor: disabled ? "not-allowed" : "pointer",
                  border: `1px solid ${on ? C.brand : C.border}`,
                  background: on ? C.brandBg : C.surface,
                  color: on ? C.brand : C.textSub }}>
                {(row.option_labels?.[o] || o).slice(0, 3)}
              </button>
            );
          })}
        </div>
      );
    }
    // Assigned shift. Options are org-scoped rows, not a static enum.
    if (row.type === "shift_ref") {
      return (
        <select value={v ?? ""} disabled={disabled} onChange={e => setDraft(row.key, e.target.value)}
          style={{ ...selStyle(C, disabled), minWidth: 180 }}>
          <option value="">None (use start/end below)</option>
          {shifts.map(s => (
            <option key={s.id} value={String(s.id)}>
              {s.name}{s.start_time ? ` · ${s.start_time}–${s.end_time}` : ""}
            </option>
          ))}
        </select>
      );
    }
    // Shift start / end. Empty is meaningful: "no scheduled shift", which is why
    // there is an explicit Clear rather than only a picker.
    if (row.type === "time") {
      return (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input type="time" value={v ?? ""} disabled={disabled}
            onChange={e => setDraft(row.key, e.target.value)}
            style={{ ...selStyle(C, disabled), width: 120 }} />
          {!disabled && v ? (
            <button onClick={() => setDraft(row.key, "")} title="Clear — no scheduled shift"
              style={{ background: "none", border: "none", color: C.textSub, cursor: "pointer",
                fontSize: 11, fontFamily: "inherit", padding: 0 }}>Clear</button>
          ) : null}
        </div>
      );
    }
    if (row.type === "enum") {
      return (
        <select value={v ?? ""} disabled={disabled} onChange={e => setDraft(row.key, e.target.value)}
          style={selStyle(C, disabled)}>
          {(row.options || []).map(o =>
            <option key={o} value={o}>{row.option_labels?.[o] || o}{row.unit && !row.option_labels ? ` ${row.unit}` : ""}</option>)}
        </select>
      );
    }
    // Free text, e.g. comma-separated recipient addresses. Without this branch a
    // "text" setting fell through to the number input below and could not hold
    // an email address at all.
    if (row.type === "text") {
      return (
        <input type="text" value={v ?? ""} disabled={disabled}
          placeholder="name@company.com, other@company.com"
          onChange={e => setDraft(row.key, e.target.value)}
          style={{ ...selStyle(C, disabled), width: 280 }} />
      );
    }
    // int (and any fallback)
    return (
      <input type="number" value={v ?? ""} disabled={disabled}
        min={row.min} max={row.max}
        onChange={e => setDraft(row.key, e.target.value)}
        style={{ ...selStyle(C, disabled), width: 110 }} />
    );
  };

  const badge = (row) => {
    // Employment type at Employee scope is stored on the employee record, so
    // "Inherited" would be a lie — it is always that person's own value.
    if (isEmploymentTypeOnEmployee(row)) {
      return <span style={pill(C.brandBg, C.brand)}>Employee record</span>;
    }
    const overridden = row.key in drafts ? true : row.is_override;
    if (overridden) {
      return (
        <span style={pill(C.brandBg, C.brand)}>
          Overridden{scope !== "org" ? "" : ""}
        </span>
      );
    }
    if (scope === "org") return <span style={pill(C.borderLight, C.textSub)}>System default</span>;
    return <span style={pill(C.borderLight, C.textSub)}>Inherited</span>;
  };

  return (
    <div style={{ padding: "28px 36px", maxWidth: 900 }}>
      <p style={{ fontSize: 14, color: C.textSub, marginBottom: 20, lineHeight: 1.6 }}>
        Configure how Temveris behaves for your company. Settings cascade{" "}
        <b>Employee → Team → Organization → System default</b> — an override at a narrower scope wins.
        Trackers and dashboards pick up changes automatically.
      </p>

      {/* Scope selector */}
      <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        {SCOPES.map(s => (
          <button key={s.k} onClick={() => { setScope(s.k); setDrafts({}); }}
            style={{ display: "flex", alignItems: "center", gap: 7, padding: "9px 16px", borderRadius: 10,
              border: `2px solid ${scope === s.k ? C.brand : C.border}`, cursor: "pointer",
              background: scope === s.k ? C.brandBg : C.surface, color: scope === s.k ? C.brand : C.textSub,
              fontWeight: scope === s.k ? 700 : 500, fontSize: 13, fontFamily: "inherit" }}>
            <span>{s.icon}</span>{s.label}
          </button>
        ))}
      </div>
      <p style={{ fontSize: 12, color: C.textSub, marginBottom: 14 }}>
        {SCOPES.find(s => s.k === scope)?.hint}
      </p>

      {/* Team / Employee target picker */}
      {scope === "team" && (
        <select value={team} onChange={e => setTeam(e.target.value)} style={{ ...selStyle(C, false), marginBottom: 16, minWidth: 260 }}>
          <option value="">Select a team…</option>
          {teams.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      )}
      {scope === "employee" && (
        // Searchable rather than a bare <select>: a real workspace roster runs to
        // dozens of names, and a native dropdown can only be jumped through by
        // first letter. Email is in the search text but not the label — two
        // people called "Priya" are told apart by typing the address, without
        // putting every address on screen.
        <SearchableSelect
          id="org-settings-employee"
          value={userId}
          onChange={setUserId}
          options={employeeOptions}
          C={C}
          ariaLabel="Employee"
          placeholder="Select an employee…"
          searchPlaceholder="Search employees by name, email or team…"
          emptyText="No employees match"
        />
      )}

      {/* Search */}
      {data && (
        <input placeholder="Search settings…" value={search} onChange={e => setSearch(e.target.value)}
          style={{ ...selStyle(C, false), width: "100%", marginBottom: 16 }} />
      )}

      {loading && <div style={{ color: C.textSub, padding: 24 }}>Loading…</div>}
      {!loading && !data && (scope !== "org") && (
        <div style={{ color: C.textSub, padding: "24px 4px" }}>
          Choose a {scope} above to view and override its settings.
        </div>
      )}

      {!loading && data && grouped.map(({ group, rows }) => {
        const isCol = !!collapsed[group];
        return (
          <div key={group} style={{ border: `1px solid ${C.border}`, borderRadius: 12, marginBottom: 14, overflow: "hidden", background: C.surface }}>
            <button onClick={() => setCollapsed(c => ({ ...c, [group]: !c[group] }))}
              style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "14px 18px", background: "transparent", border: "none", cursor: "pointer",
                fontFamily: "inherit", fontSize: 15, fontWeight: 700, color: C.text }}>
              <span>{group}</span>
              <span style={{ color: C.textSub, transform: isCol ? "none" : "rotate(90deg)", transition: "transform .2s" }}>▸</span>
            </button>
            {!isCol && (
              <div style={{ borderTop: `1px solid ${C.border}` }}>
                {rows.map(row => (
                  <div key={row.key} style={{ display: "flex", alignItems: "center", gap: 16,
                    padding: "14px 18px", borderTop: `1px solid ${C.borderLight}` }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 14, fontWeight: 600, color: C.text, display: "flex", alignItems: "center", gap: 8 }}>
                        {row.label} {badge(row)}
                      </div>
                      {row.description && <div style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>{row.description}</div>}
                      {scope !== "org" && !isEmploymentTypeOnEmployee(row) && !(row.key in drafts) && !row.is_override && row.parent_value != null && (
                        <div style={{ fontSize: 11, color: C.textLight, marginTop: 2 }}>Inheriting: {fmt(row, row.parent_value, shifts)}</div>
                      )}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
                      {control(row)}
                      {(row.is_override && !isEmploymentTypeOnEmployee(row)) && (
                        <button onClick={() => resetKey(row.key)} disabled={!canEdit}
                          title="Reset to inherited value"
                          style={{ background: "none", border: "none", color: C.brand, cursor: canEdit ? "pointer" : "default",
                            fontSize: 12, fontWeight: 600, fontFamily: "inherit", padding: 0, whiteSpace: "nowrap" }}>
                          ↺ Reset
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {/* Save bar */}
      {canEdit && dirtyKeys.length > 0 && (
        <div style={{ position: "sticky", bottom: 0, background: C.surface, borderTop: `1px solid ${C.border}`,
          padding: "14px 4px", display: "flex", alignItems: "center", gap: 14, marginTop: 8 }}>
          <span style={{ fontSize: 13, color: C.textSub }}>{dirtyKeys.length} unsaved change(s)</span>
          <button onClick={save} disabled={saving}
            style={{ padding: "10px 24px", borderRadius: 8, border: "none", background: C.brand, color: "#fff",
              fontWeight: 700, fontSize: 14, cursor: saving ? "wait" : "pointer", fontFamily: "inherit" }}>
            {saving ? "Saving…" : "Save changes"}
          </button>
          <button onClick={() => setDrafts({})} disabled={saving}
            style={{ padding: "10px 18px", borderRadius: 8, border: `1px solid ${C.border}`, background: "transparent",
              color: C.textSub, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
            Discard
          </button>
        </div>
      )}

      {!isAdmin && (
        <p style={{ fontSize: 12, color: C.textSub, marginTop: 12 }}>You have read-only access to these settings.</p>
      )}
    </div>
  );
}

// A JSON array string ("[\"MONDAY\",…]") -> array. Tolerates a plain CSV and
// anything malformed, so a hand-edited row can never blank the whole section.
function parseList(v) {
  if (Array.isArray(v)) return v;
  const s = String(v ?? "").trim();
  if (!s) return [];
  try {
    const parsed = JSON.parse(s);
    if (Array.isArray(parsed)) return parsed.map(String);
  } catch { /* fall through to CSV */ }
  return s.split(",").map(x => x.trim()).filter(Boolean);
}

// value formatting for the "Inheriting: …" hint
function fmt(row, v, shifts) {
  if (row.type === "bool") return v === "true" ? "On" : "Off";
  if (row.type === "multiselect") {
    const list = parseList(v);
    if (!list.length) return "None";
    return list.map(o => (row.option_labels?.[o] || o).slice(0, 3)).join(", ");
  }
  if (row.type === "shift_ref") {
    if (!v) return "None";
    const s = (shifts || []).find(x => String(x.id) === String(v));
    return s ? s.name : `Shift #${v}`;
  }
  // Empty is meaningful for the shift times — it means no scheduled shift, so
  // rendering it as blank would read as "unset" rather than "deliberately none".
  if (row.type === "time" && !v) return "Not set";
  if (row.type === "enum" && row.option_labels?.[v]) return row.option_labels[v];
  if (v === "" || v == null) return "Not set";
  return `${v}${row.unit ? ` ${row.unit}` : ""}`;
}

const selStyle = (C, disabled) => ({
  padding: "8px 12px", border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 13,
  color: C.text, background: disabled ? C.borderLight : C.surface,
  cursor: disabled ? "not-allowed" : "pointer", outline: "none", fontFamily: "inherit",
});

const pill = (bg, fg) => ({
  fontSize: 10, fontWeight: 700, color: fg, background: bg,
  padding: "2px 8px", borderRadius: 10, textTransform: "uppercase", letterSpacing: .3,
});
