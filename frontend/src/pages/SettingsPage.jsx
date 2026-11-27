import React, { useState, useEffect, useRef, useMemo } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { API_BASE } from "../config";
import API from "../api/axios";
import * as holidayApi from "../api/holidays";
import AdminSidebar from "../components/AdminSidebar";
import Gate, { FeatureLocked } from "../entitlements/Gate";
import TopNavbar from "../components/TopNavbar";
import PlatformNotificationsTab from "../components/PlatformNotificationsTab";
import WorkspaceProfileTab from "../components/WorkspaceProfileTab";
import OrganizationSettingsTab from "../components/OrganizationSettingsTab";
import TimeAdjustmentsTab from "../components/workspace/TimeAdjustmentsTab";
import LeaveBalanceManagementTab from "../components/workspace/LeaveBalanceManagementTab";
import {
  AuditComplianceTab,
  BillingSubscriptionTab,
  ConnectedSystemsTab,
  PoliciesTab,
  RolesPermissionsTab,
  SecurityTab,
} from "../components/workspace/WorkspaceSettingsTabs";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { SETTINGS_SLUG_MAP } from "../App";

// Reverse map: internal tab key → canonical (renamed) URL slug.
// Used when the user clicks a sub-nav button so the URL always shows the
// renamed slug, never the legacy "manage-teams"/"manage-employees" one.
const SETTINGS_KEY_TO_SLUG = {
  "workspace-profile":  "workspace-profile",
  "organization-settings": "organization-settings",
  "manage-teams":       "departments-teams",
  "manage-employees":   "workers-users",
  "roles-permissions":  "roles-permissions",
  "policies":           "policies",
  "leave-balance":      "leave-balance",
  "public-holidays":    "public-holidays",
  "time-adjustments":   "time-adjustments",
  "billing-subscription": "billing-subscription",
  "security":           "security",
  "connected-systems":  "connected-systems",
  "audit-compliance":   "audit-compliance",
};

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

// ── STATIC DATA (stored as stable keys; translated at display time) ──────────
const SCREENSHOT_INTERVALS = [
  "Every 30 secs","Every 1 min","Every 2 mins","Every 5 mins",
  "Every 10 mins","Every 15 mins","Every 20 mins",
  "Every 30 mins","Every 45 mins",
  "Every 1 hour","Every 2 hours","Every 3 hours",
];

const SCREENSHOT_INTERVAL_KEYS = {
  "Every 30 secs": "every30Secs",
  "Every 1 min": "every1Min",
  "Every 2 mins": "every2Mins",
  "Every 5 mins": "every5Mins",
  "Every 10 mins": "every10Mins",
  "Every 15 mins": "every15Mins",
  "Every 20 mins": "every20Mins",
  "Every 30 mins": "every30Mins",
  "Every 45 mins": "every45Mins",
  "Every 1 hour": "every1Hour",
  "Every 2 hours": "every2Hours",
  "Every 3 hours": "every3Hours",
};

const IDLE_THRESHOLDS = [
  "1 minute", "2 minutes", "3 minutes", "5 minutes",
  "10 minutes", "15 minutes", "20 minutes", "30 minutes",
];

const IDLE_THRESHOLD_KEYS = {
  "1 minute": "oneMinute",
  "2 minutes": "twoMinutes",
  "3 minutes": "threeMinutes",
  "5 minutes": "fiveMinutes",
  "10 minutes": "tenMinutes",
  "15 minutes": "fifteenMinutes",
  "20 minutes": "twentyMinutes",
  "30 minutes": "thirtyMinutes",
};

const WORK_HOUR_OPTIONS = [
  "6:00", "6:30", "7:00", "7:30", "8:00", "8:30", "8:45", "9:00", "9:30", "10:00",
];

// ── REUSABLE COMPONENTS ───────────────────────────────────────────────────────
// Each sub-component below MUST destructure `C` from useTheme() — they are
// defined at module scope so they don't capture the outer SettingsPage's `C`.
// Forgetting this destructure was the root cause of the "C is not defined"
// crash that took out the whole Settings page after the colour-token cleanup.
const Chk = ({ checked, onChange, disabled, label, sub, premium }) => {
  const { T } = useLanguage();
  const { C } = useTheme();
  return (
    <label style={{ display:"flex", alignItems:"flex-start", gap:10, cursor:disabled?"default":"pointer", marginBottom:10, opacity:disabled?0.6:1 }}>
      <input type="checkbox" checked={!!checked} onChange={e => !disabled && onChange(e.target.checked)}
        style={{ marginTop:2, accentColor:C.brand, width:15, height:15, flexShrink:0, cursor:disabled?"default":"pointer" }} />
      <span style={{ fontSize:14, color:C.text, lineHeight:1.5 }}>
        {label}
        {premium && <span style={{ marginLeft:6, fontSize:10, fontWeight:800, color:C.danger, background:C.dangerBg, padding:"1px 6px", borderRadius:4, verticalAlign:"middle" }}>{T.premium || "Premium"}</span>}
        {sub && <span style={{ display:"block", fontSize:12, color:C.textSub, marginTop:2 }}>{sub}</span>}
      </span>
    </label>
  );
};

const Sel = ({ value, onChange, options, disabled, style={}, labelKeys }) => {
  const { T } = useLanguage();
  const { C } = useTheme();
  const labelFor = (o) => {
    const k = labelKeys?.[o];
    return (k && T[k]) || o;
  };
  return (
    <select value={value} onChange={e => !disabled && onChange(e.target.value)} disabled={disabled}
      style={{ padding:"8px 32px 8px 12px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:13,
        color:C.text, background:disabled?C.borderLight:C.surface, cursor:disabled?"not-allowed":"pointer",
        outline:"none", fontFamily:"inherit", appearance:"auto", ...style }}>
      {options.map(o => <option key={o} value={o}>{labelFor(o)}</option>)}
    </select>
  );
};

const Toggle = ({ on, onChange, disabled }) => {
  const { C } = useTheme();
  return (
    <button onClick={() => !disabled && onChange(!on)} disabled={disabled}
      style={{ width:44, height:24, borderRadius:12, border:"none", cursor:disabled?"not-allowed":"pointer",
        background:on?C.brand:C.textLight, position:"relative", transition:"background .2s", flexShrink:0 }}>
      <span style={{ position:"absolute", top:2, left:on?22:2, width:20, height:20,
        background:"#fff", borderRadius:"50%", transition:"left .2s", boxShadow:"0 1px 4px rgba(0,0,0,0.25)" }} />
    </button>
  );
};

const SectionBox = ({ title, premium, children }) => {
  const { T } = useLanguage();
  const { C } = useTheme();
  return (
    <div style={{ background:C.bg, border:`1px solid ${C.border}`, borderRadius:10, padding:"16px 18px", marginBottom:14 }}>
      <p style={{ fontSize:15, fontWeight:700, color:C.text, marginBottom:14, display:"flex", alignItems:"center", gap:8 }}>
        {title}
        {premium && <span style={{ fontSize:10, fontWeight:800, color:C.danger, background:C.dangerBg, padding:"2px 8px", borderRadius:4 }}>{T.premium || "Premium"}</span>}
      </p>
      {children}
    </div>
  );
};

// ── SUB-NAV ───────────────────────────────────────────────────────────────────
//
// Order follows the Workspace Settings spec: Company & Workspace Profile is
// the first surface a new admin lands on so the workspace identity is set
// before departments and workers are added. Internal keys for the renamed
// tabs (manage-teams, manage-employees) stay unchanged — only the user-
// facing labels shift, which keeps any deep-linked bookmarks valid.
const SUB_NAV = [
  { k:"workspace-profile",     icon:"🏢", label:"Company & Workspace Profile" },
  { k:"organization-settings", icon:"⚙️", label:"Organization Settings" },
  { k:"manage-teams",          icon:"👥", label:"Departments & Teams" },
  { k:"manage-employees",      icon:"👤", label:"Workers & Users" },
  { k:"roles-permissions",     icon:"🔐", label:"Roles & Permissions" },
  { k:"policies",              icon:"📋", label:"Policies" },
  { k:"leave-balance",         icon:"⚖️", label:"Leave Balance Management" },
  { k:"public-holidays",       icon:"🌍", label:"Public Holidays" },
  { k:"time-adjustments",      icon:"⏱️", label:"Time Adjustments" },
  { k:"billing-subscription",  icon:"💳", label:"Billing & Subscription" },
  { k:"security",              icon:"🛡️", label:"Security" },
  { k:"connected-systems",     icon:"🔌", label:"Connected Systems" },
  { k:"audit-compliance",      icon:"📜", label:"Audit & Compliance" },
];

// Workspace Profile tab lives in components/WorkspaceProfileTab.jsx —
// extracted because the Figma design grew it past 500 lines.

// ── DEPARTMENTS & TEAMS TAB ───────────────────────────────────────────────────
// Internal function name (ManageTeamsTab) preserved per spec §5 — do not rename
// internal symbols just to match UI labels unless part of a planned migration.
function ManageTeamsTab({ isAdmin, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const [teams, setTeams]           = useState([]);
  const [loading, setLoading]       = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [showCreateTeam, setShowCreateTeam] = useState(false);
  const [newName, setNewName]       = useState("");
  const [newTeamName, setNewTeamName]     = useState("");
  const [newTeamParent, setNewTeamParent] = useState("");
  const [saving, setSaving]         = useState(false);
  const [menuOpen, setMenuOpen]     = useState(null);
  const [editTeam, setEditTeam]     = useState(null);
  const [membersTeam, setMembersTeam] = useState(null);
  const menuRef = useRef(null);

  const load = () => {
    setLoading(true);
    fetch(`${API_BASE}/org/teams`, { headers: { Authorization: `Bearer ${TOKEN()}` } })
      .then(r => r.json())
      .then(d => { if (Array.isArray(d)) setTeams(d); })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);
  useEffect(() => {
    const h = (e) => { if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(null); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/teams`, {
        method:"POST",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({ name: newName.trim() }),
      });
      if (res.ok) { showToast(T.departmentCreated || "Department created!"); setNewName(""); setShowCreate(false); load(); }
      else { const d = await res.json(); showToast(d.detail || (T.settingsPageFailed || "Failed"), "error"); }
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  // Add Team — secondary CTA per Workspace Settings spec §4.3 + AC#5.
  //
  // The existing data model stores both Departments and Teams in the same
  // `teams` table; a parent_id column is a planned follow-on migration
  // (see spec §5 — "Do not rename database tables just to match UI labels").
  // For now the parent department is forwarded in the POST payload so the
  // backend can pick it up the moment the column exists. Today's API
  // ignores the field and creates a top-level row; the UI still visually
  // groups by name convention.
  const handleCreateTeam = async () => {
    if (!newTeamName.trim()) return;
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/teams`, {
        method:"POST",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({
          name: newTeamName.trim(),
          parent_id: newTeamParent ? Number(newTeamParent) : null,
        }),
      });
      if (res.ok) {
        showToast(T.teamCreated || "Team created");
        setNewTeamName(""); setNewTeamParent("");
        setShowCreateTeam(false);
        load();
      } else {
        const d = await res.json(); showToast(d.detail || (T.settingsPageFailed || "Failed"), "error");
      }
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  const handleDelete = async (id) => {
    if (!window.confirm(T.confirmDeleteDepartment || "Delete this department?")) return;
    try {
      const res = await fetch(`${API_BASE}/org/teams/${id}`, {
        method:"DELETE", headers:{ Authorization:`Bearer ${TOKEN()}` },
      });
      if (res.ok) { showToast(T.departmentDeleted || "Department deleted"); load(); }
      else showToast(T.settingsPageFailedToDelete || "Failed to delete", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setMenuOpen(null);
  };

  const handleEdit = async () => {
    if (!editTeam?.name?.trim()) return;
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/teams/${editTeam.id}`, {
        method:"PUT",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({ name: editTeam.name }),
      });
      if (res.ok) { showToast(T.departmentUpdated || "Department updated!"); setEditTeam(null); load(); }
      else showToast(T.settingsPageFailed || "Failed", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  const avatar = (name) => name.trim().split(" ").map(w=>w[0]).join("").toUpperCase().slice(0,2);

  return (
    <div style={{ padding:"24px 32px", maxWidth:900 }}>
      <div style={{ display:"flex", justifyContent:"flex-end", gap:10, marginBottom:24 }}>
        {isAdmin && (
          <>
            {/* Secondary CTA — Add Team (spec §4.3 + AC#5). */}
            <button onClick={() => setShowCreateTeam(true)}
              style={{ padding:"10px 18px", background:C.surface, color:C.brand,
                       border:`1px solid ${C.brand}`, borderRadius:8,
                       fontWeight:700, fontSize:14, cursor:"pointer",
                       fontFamily:"inherit" }}>
              {T.addTeam || "Add Team"}
            </button>
            <button onClick={() => setShowCreate(true)}
              style={{ padding:"10px 22px", background:C.brand, color:"#fff", border:"none", borderRadius:8, fontWeight:700, fontSize:14, cursor:"pointer", fontFamily:"inherit" }}>
              {T.addDepartment || "Add Department"}
            </button>
          </>
        )}
      </div>

      {showCreate && (
        <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.35)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <div style={{ background:C.surface, borderRadius:12, padding:28, width:400, boxShadow:"0 20px 60px rgba(0,0,0,0.2)", border:`1px solid ${C.border}` }}>
            <p style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:16 }}>{T.addDepartment || "Add Department"}</p>
            <label style={{ fontSize:13, fontWeight:600, color:C.text, display:"block", marginBottom:6 }}>{T.departmentName || "Department Name"}</label>
            <input value={newName} onChange={e => setNewName(e.target.value)} placeholder={T.settingsPageDepartmentNamePlaceholder || "e.g. Technology, Engineering, Sales, Operations"}
              style={{ width:"100%", padding:"9px 12px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:14, fontFamily:"inherit", outline:"none", boxSizing:"border-box", marginBottom:18, background:C.surface, color:C.text }} />
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={() => setShowCreate(false)}
                style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>{T.cancel || "Cancel"}</button>
              <button onClick={handleCreate} disabled={saving || !newName.trim()}
                style={{ padding:"9px 22px", borderRadius:7, border:"none", background:C.brand, color:"#fff", fontWeight:700, fontSize:13, cursor:"pointer", fontFamily:"inherit", opacity:(!newName.trim()||saving)?0.6:1 }}>
                {saving?(T.creating||"Creating..."):(T.confirm||"Create")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add Team modal (secondary CTA per spec §4.3 + AC#5). */}
      {showCreateTeam && (
        <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.35)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <div style={{ background:C.surface, borderRadius:12, padding:28, width:440, boxShadow:"0 20px 60px rgba(0,0,0,0.2)", border:`1px solid ${C.border}` }}>
            <p style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:6 }}>{T.addTeam || "Add Team"}</p>
            <p style={{ fontSize:12, color:C.textSub, marginBottom:16 }}>
              {T.addTeamHint || "Create working groups within a department, such as Platform Team, QA Team, or London Support Team."}
            </p>
            <label style={{ fontSize:13, fontWeight:600, color:C.text, display:"block", marginBottom:6 }}>{T.teamName || "Team Name"}</label>
            <input value={newTeamName} onChange={e => setNewTeamName(e.target.value)} placeholder={T.settingsPageTeamNamePlaceholder || "e.g. Platform Team, QA Team"}
              style={{ width:"100%", padding:"9px 12px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:14, fontFamily:"inherit", outline:"none", boxSizing:"border-box", marginBottom:14, background:C.surface, color:C.text }} />
            <label style={{ fontSize:13, fontWeight:600, color:C.text, display:"block", marginBottom:6 }}>{T.parentTeam || "Parent team (optional)"}</label>
            <select value={newTeamParent} onChange={e => setNewTeamParent(e.target.value)}
              style={{ width:"100%", padding:"9px 12px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:14, fontFamily:"inherit", outline:"none", boxSizing:"border-box", marginBottom:18, background:C.surface, color:C.text, cursor:"pointer" }}>
              <option value="">{T.noTeam || "No parent (top level)"}</option>
              {teams.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={() => { setShowCreateTeam(false); setNewTeamName(""); setNewTeamParent(""); }}
                style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>{T.cancel || "Cancel"}</button>
              <button onClick={handleCreateTeam} disabled={saving || !newTeamName.trim()}
                style={{ padding:"9px 22px", borderRadius:7, border:"none", background:C.brand, color:"#fff", fontWeight:700, fontSize:13, cursor:"pointer", fontFamily:"inherit", opacity:(!newTeamName.trim()||saving)?0.6:1 }}>
                {saving?(T.creating||"Creating..."):(T.confirm||"Create")}
              </button>
            </div>
          </div>
        </div>
      )}

      {editTeam && (
        <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.35)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <div style={{ background:C.surface, borderRadius:12, padding:28, width:400, boxShadow:"0 20px 60px rgba(0,0,0,0.2)", border:`1px solid ${C.border}` }}>
            <p style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:16 }}>{T.editDepartment || "Rename Department"}</p>
            <input value={editTeam.name} onChange={e => setEditTeam(t=>({...t,name:e.target.value}))}
              style={{ width:"100%", padding:"9px 12px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:14, fontFamily:"inherit", outline:"none", boxSizing:"border-box", marginBottom:18, background:C.surface, color:C.text }} />
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={() => setEditTeam(null)} style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>{T.cancel || "Cancel"}</button>
              <button onClick={handleEdit} disabled={saving}
                style={{ padding:"9px 22px", borderRadius:7, border:"none", background:C.brand, color:"#fff", fontWeight:700, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>
                {saving?(T.saving||"Saving..."):(T.save||"Save")}
              </button>
            </div>
          </div>
        </div>
      )}

      {membersTeam && (
        <TeamMembersModal
          team={membersTeam}
          onClose={() => setMembersTeam(null)}
          onChanged={load}
          showToast={showToast}
        />
      )}

      <div style={{ background:C.surface, borderRadius:10, border:`1px solid ${C.border}` }}>
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr 40px", padding:"10px 24px", borderBottom:`1px solid ${C.border}`, background:C.surfaceAlt || C.bg, borderTopLeftRadius:10, borderTopRightRadius:10 }}>
          {[T.departmentName||"Department Name",T.workers||"Workers",T.departmentLead||"Department Lead"].map(h => (
            <span key={h} style={{ fontSize:12, fontWeight:700, color:C.textSub, textTransform:"uppercase", letterSpacing:0.5 }}>{h}</span>
          ))}
          <span />
        </div>
        {loading ? (
          <div style={{ padding:"48px 0", textAlign:"center", color:C.textSub }}>{T.loading || "Loading..."}</div>
        ) : teams.length === 0 ? (
          <div style={{ padding:"60px 0", textAlign:"center", color:C.textLight }}>
            <div style={{ fontSize:36, marginBottom:12 }}>👥</div>
            <p style={{ fontWeight:600, color:C.textSub }}>{T.noDepartmentsFound || "No departments yet"}</p>
            <p style={{ fontSize:13, color:C.textLight }}>{T.createNewDepartmentHint || 'Create departments such as Technology, Engineering, Sales, Operations, or Finance, then organize teams under them.'}</p>
          </div>
        ) : teams.map(team => (
          <div key={team.id} style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr 40px", padding:"14px 24px", borderBottom:`1px solid ${C.borderLight}`, alignItems:"center", position:"relative" }}>
            <div style={{ display:"flex", alignItems:"center", gap:12 }}>
              <div style={{ width:36, height:36, borderRadius:"50%", background:"#0BA5EC", display:"flex", alignItems:"center", justifyContent:"center", color:"#fff", fontWeight:800, fontSize:13, flexShrink:0 }}>
                {avatar(team.name)}
              </div>
              <span style={{ fontWeight:700, fontSize:14, color:C.text }}>{team.name}</span>
            </div>
            <span style={{ fontSize:14, color:C.text }}>{team.employee_count} {team.employee_count===1 ? (T.worker || "Worker") : (T.workers || "Workers")}</span>
            <span style={{ fontSize:14, color:team.head_name?"#344054":"#98A2B3", fontStyle:team.head_name?"normal":"italic" }}>
              {team.head_name || (T.noDepartmentLeadAssigned || "No department lead assigned!")}
            </span>
            {isAdmin && (
              <div style={{ position:"relative" }} ref={menuOpen===team.id?menuRef:null}>
                <button onClick={() => setMenuOpen(menuOpen===team.id?null:team.id)}
                  style={{ background:"none", border:"none", cursor:"pointer", fontSize:20, color:"#667085", padding:"4px 8px", lineHeight:1 }}>⋮</button>
                {menuOpen===team.id && (
                  <div style={{ position:"absolute", right:0, top:"110%", background:C.surface, border:`1px solid ${C.border}`, borderRadius:8, boxShadow:"0 8px 24px rgba(0,0,0,0.12)", zIndex:100, minWidth:160 }}>
                    <button onClick={() => { setMembersTeam({id:team.id,name:team.name,head_user_id:team.head_user_id}); setMenuOpen(null); }}
                      style={{ display:"block", width:"100%", padding:"9px 16px", border:"none", background:"none", textAlign:"left", fontSize:13, cursor:"pointer", color:C.text, fontFamily:"inherit" }}>👥 {T.manageMembers || "Manage Members"}</button>
                    <button onClick={() => { setEditTeam({id:team.id,name:team.name}); setMenuOpen(null); }}
                      style={{ display:"block", width:"100%", padding:"9px 16px", border:"none", background:"none", textAlign:"left", fontSize:13, cursor:"pointer", color:C.text, fontFamily:"inherit" }}>✏️ {T.rename || "Rename"}</button>
                    <button onClick={() => handleDelete(team.id)}
                      style={{ display:"block", width:"100%", padding:"9px 16px", border:"none", background:"none", textAlign:"left", fontSize:13, cursor:"pointer", color:C.danger, fontFamily:"inherit" }}>🗑 {T.delete || "Delete"}</button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── TEAM MEMBERS MODAL ────────────────────────────────────────────────────────
// Used by the Departments & Teams tab to add/remove users for a team.
// Backend: /org/teams/{team_id}/members (GET/POST/DELETE) and
// /org/teams/{team_id}/assignable-users (GET). Multitenancy-compatible —
// the User.team string match is implicitly scoped by the caller's
// organization_id once that column is live.
function TeamMembersModal({ team, onClose, onChanged, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const [members, setMembers]       = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [selected, setSelected]     = useState(new Set());
  const [loading, setLoading]       = useState(true);
  const [saving, setSaving]         = useState(false);
  const [filter, setFilter]         = useState("");
  const [leadId, setLeadId]         = useState(team.head_user_id ?? "");

  const reload = async () => {
    setLoading(true);
    try {
      const [mRes, aRes] = await Promise.all([
        fetch(`${API_BASE}/org/teams/${team.id}/members`,         { headers:{ Authorization:`Bearer ${TOKEN()}` } }),
        fetch(`${API_BASE}/org/teams/${team.id}/assignable-users`, { headers:{ Authorization:`Bearer ${TOKEN()}` } }),
      ]);
      const m = await mRes.json();
      const a = await aRes.json();
      if (Array.isArray(m)) setMembers(m);
      if (Array.isArray(a)) setCandidates(a);
    } catch {
      showToast?.(T.networkError || "Network error", "error");
    }
    setSelected(new Set());
    setLoading(false);
  };
  useEffect(() => { reload(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [team.id]);

  const toggle = (id) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const handleAdd = async () => {
    if (selected.size === 0) return;
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/teams/${team.id}/members`, {
        method:"POST",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({ user_ids: Array.from(selected) }),
      });
      if (res.ok) {
        const d = await res.json();
        showToast?.((T.membersAdded || "Members added") + ` (${d.added ?? selected.size})`);
        await reload();
        onChanged?.();
      } else {
        const d = await res.json().catch(() => ({}));
        showToast?.(d.detail || (T.settingsPageFailedToAddMembers || "Failed to add members"), "error");
      }
    } catch {
      showToast?.(T.networkError || "Network error", "error");
    }
    setSaving(false);
  };

  const handleRemove = async (userId) => {
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/teams/${team.id}/members/${userId}`, {
        method:"DELETE",
        headers:{ Authorization:`Bearer ${TOKEN()}` },
      });
      if (res.ok) {
        showToast?.(T.memberRemoved || "Member removed");
        await reload();
        onChanged?.();
      } else {
        const d = await res.json().catch(() => ({}));
        showToast?.(d.detail || (T.settingsPageFailedToRemoveMember || "Failed to remove member"), "error");
      }
    } catch {
      showToast?.(T.networkError || "Network error", "error");
    }
    setSaving(false);
  };

  const handleSetLead = async (value) => {
    setLeadId(value);
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/teams/${team.id}`, {
        method:"PUT",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({ name: team.name, head_user_id: value === "" ? null : Number(value) }),
      });
      if (res.ok) {
        showToast?.(value === "" ? (T.leadCleared || "Department lead cleared") : (T.leadAssigned || "Department lead assigned"));
        onChanged?.();
      } else {
        const d = await res.json().catch(() => ({}));
        showToast?.(d.detail || (T.settingsPageFailedToSave || "Failed to save"), "error");
      }
    } catch {
      showToast?.(T.networkError || "Network error", "error");
    }
    setSaving(false);
  };

  const norm = (s) => (s || "").toLowerCase();
  const visibleCandidates = candidates.filter(u =>
    !filter.trim() ||
    norm(u.name).includes(norm(filter)) ||
    norm(u.email).includes(norm(filter))
  );

  const avatar = (name) => (name||"").trim().split(" ").map(w=>w[0]).join("").toUpperCase().slice(0,2);

  return (
    <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.35)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
      <div style={{ background:C.surface, borderRadius:12, padding:24, width:640, maxHeight:"80vh", display:"flex", flexDirection:"column", boxShadow:"0 20px 60px rgba(0,0,0,0.2)", border:`1px solid ${C.border}` }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:8 }}>
          <p style={{ fontSize:16, fontWeight:700, color:C.text, margin:0 }}>
            {(T.manageMembers || "Manage Members")} — {team.name}
          </p>
          <button onClick={onClose}
            style={{ background:"none", border:"none", fontSize:22, color:C.textSub, cursor:"pointer", lineHeight:1, padding:"0 4px" }}>×</button>
        </div>
        <p style={{ fontSize:12, color:C.textSub, marginTop:0, marginBottom:16 }}>
          {T.manageMembersHint || "Add or remove workers for this team. Each worker belongs to one team at a time."}
        </p>

        {!loading && (
          <div style={{ display:"flex", alignItems:"center", gap:10, marginBottom:16 }}>
            <label style={{ fontSize:13, fontWeight:600, color:C.text, whiteSpace:"nowrap" }}>{T.departmentLead || "Department Lead"}</label>
            <select value={leadId} onChange={e => handleSetLead(e.target.value)} disabled={saving}
              style={{ flex:1, padding:"8px 10px", border:`1px solid ${C.border}`, borderRadius:7, fontSize:13, fontFamily:"inherit", outline:"none", background:C.surface, color:C.text, cursor:saving?"not-allowed":"pointer" }}>
              <option value="">{T.noLeadOption || "No lead assigned"}</option>
              {members.map(u => <option key={u.id} value={u.id}>{u.name}</option>)}
            </select>
          </div>
        )}

        {loading ? (
          <div style={{ padding:"32px 0", textAlign:"center", color:C.textSub }}>{T.loading || "Loading..."}</div>
        ) : (
          <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16, flex:1, overflow:"hidden" }}>
            {/* Current members */}
            <div style={{ border:`1px solid ${C.border}`, borderRadius:8, display:"flex", flexDirection:"column", overflow:"hidden" }}>
              <div style={{ padding:"10px 12px", borderBottom:`1px solid ${C.border}`, background:C.surfaceAlt || C.bg, fontSize:12, fontWeight:700, color:C.textSub, textTransform:"uppercase", letterSpacing:0.4 }}>
                {(T.currentMembers || "Current members")} ({members.length})
              </div>
              <div style={{ overflowY:"auto", flex:1 }}>
                {members.length === 0 ? (
                  <div style={{ padding:"24px 12px", textAlign:"center", color:C.textLight, fontSize:13 }}>
                    {T.noMembersYet || "No members yet"}
                  </div>
                ) : members.map(u => (
                  <div key={u.id} style={{ display:"flex", alignItems:"center", gap:10, padding:"8px 12px", borderBottom:`1px solid ${C.borderLight}` }}>
                    <div style={{ width:28, height:28, borderRadius:"50%", background:"#0BA5EC", color:"#fff", display:"flex", alignItems:"center", justifyContent:"center", fontSize:11, fontWeight:700, flexShrink:0 }}>{avatar(u.name)}</div>
                    <div style={{ flex:1, minWidth:0 }}>
                      <div style={{ fontSize:13, fontWeight:600, color:C.text, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{u.name}</div>
                      <div style={{ fontSize:11, color:C.textSub, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{u.email}</div>
                    </div>
                    <button onClick={() => handleRemove(u.id)} disabled={saving}
                      title={T.remove || "Remove"}
                      style={{ background:"none", border:`1px solid ${C.border}`, color:C.danger, cursor:"pointer", borderRadius:6, fontSize:11, padding:"3px 8px", fontFamily:"inherit" }}>
                      {T.remove || "Remove"}
                    </button>
                  </div>
                ))}
              </div>
            </div>

            {/* Add candidates */}
            <div style={{ border:`1px solid ${C.border}`, borderRadius:8, display:"flex", flexDirection:"column", overflow:"hidden" }}>
              <div style={{ padding:"10px 12px", borderBottom:`1px solid ${C.border}`, background:C.surfaceAlt || C.bg }}>
                <div style={{ fontSize:12, fontWeight:700, color:C.textSub, textTransform:"uppercase", letterSpacing:0.4, marginBottom:6 }}>
                  {T.addMembers || "Add members"}
                </div>
                <input value={filter} onChange={e => setFilter(e.target.value)}
                  placeholder={T.searchUsers || "Search by name or email"}
                  style={{ width:"100%", padding:"6px 10px", border:`1px solid ${C.border}`, borderRadius:6, fontSize:12, background:C.surface, color:C.text, fontFamily:"inherit", outline:"none", boxSizing:"border-box" }} />
              </div>
              <div style={{ overflowY:"auto", flex:1 }}>
                {visibleCandidates.length === 0 ? (
                  <div style={{ padding:"24px 12px", textAlign:"center", color:C.textLight, fontSize:13 }}>
                    {T.noUsersAvailable || "No users available"}
                  </div>
                ) : visibleCandidates.map(u => (
                  <label key={u.id} style={{ display:"flex", alignItems:"center", gap:10, padding:"8px 12px", borderBottom:`1px solid ${C.borderLight}`, cursor:"pointer" }}>
                    <input type="checkbox" checked={selected.has(u.id)} onChange={() => toggle(u.id)}
                      style={{ width:15, height:15, accentColor:C.brand, cursor:"pointer", flexShrink:0 }} />
                    <div style={{ width:28, height:28, borderRadius:"50%", background:"#6B7280", color:"#fff", display:"flex", alignItems:"center", justifyContent:"center", fontSize:11, fontWeight:700, flexShrink:0 }}>{avatar(u.name)}</div>
                    <div style={{ flex:1, minWidth:0 }}>
                      <div style={{ fontSize:13, fontWeight:600, color:C.text, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{u.name}</div>
                      <div style={{ fontSize:11, color:C.textSub, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
                        {u.email}{u.current_team ? ` · ${u.current_team}` : ""}
                      </div>
                    </div>
                  </label>
                ))}
              </div>
            </div>
          </div>
        )}

        <div style={{ display:"flex", gap:10, justifyContent:"flex-end", marginTop:18 }}>
          <button onClick={onClose}
            style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>
            {T.close || "Close"}
          </button>
          <button onClick={handleAdd} disabled={saving || selected.size === 0}
            style={{ padding:"9px 22px", borderRadius:7, border:"none", background:C.brand, color:"#fff", fontWeight:700, fontSize:13, cursor:(saving||selected.size===0)?"not-allowed":"pointer", fontFamily:"inherit", opacity:(saving||selected.size===0)?0.6:1 }}>
            {saving ? (T.saving || "Saving...") : ((T.addSelected || "Add selected") + (selected.size ? ` (${selected.size})` : ""))}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── WORKERS & USERS TAB ───────────────────────────────────────────────────────
// Internal function name (ManageEmployeesTab) preserved per spec §5 — do not
// rename internal symbols just to match UI labels unless part of a planned
// migration.
function ManageEmployeesTab({ isAdmin, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const [employees, setEmployees] = useState([]);
  const [teams, setTeams]         = useState([]);
  const [loading, setLoading]     = useState(true);
  const [showInvite, setShowInvite] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [editEmp, setEditEmp]     = useState(null);
  const [saving, setSaving]       = useState(false);
  const [inviteForm, setInviteForm] = useState({
    name:"", email:"", role:"employee", designation:"", phone:"", team:"", department:"",
  });
  const importFileRef = useRef(null);
  const [importState, setImportState] = useState({ phase: "idle", message: "", rows: [], dryRun: false });

  const load = async () => {
    setLoading(true);
    try {
      const [eRes, tRes] = await Promise.all([
        fetch(`${API_BASE}/employees`, { headers:{ Authorization:`Bearer ${TOKEN()}` } }),
        fetch(`${API_BASE}/org/teams`,  { headers:{ Authorization:`Bearer ${TOKEN()}` } }),
      ]);
      const emps = await eRes.json();
      const tms  = await tRes.json();
      if (Array.isArray(emps)) {
        const sorted = [...emps].sort((a, b) =>
          (a.name || "").localeCompare((b.name || ""), undefined, { sensitivity: "base" })
        );
        setEmployees(sorted);
      }
      if (Array.isArray(tms))  setTeams(tms);
    } catch {}
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const handleInvite = async () => {
    if (!inviteForm.name.trim() || !inviteForm.email.trim()) {
      return showToast(T.nameEmailRequired || "Name and email are required", "error");
    }
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/employees`, {
        method:"POST",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify(inviteForm),
      });
      const d = await res.json();
      if (res.ok) {
        showToast(d.invite_sent ? (T.workerInvitedSuccess || "Worker invited successfully!") : (T.workerAddedEmailFailed || "Worker added (email delivery failed — check logs)"));
        setShowInvite(false);
        setInviteForm({ name:"", email:"", role:"employee", designation:"", phone:"", team:"", department:"" });
        load();
      } else showToast(d.detail || (T.settingsPageFailedToAddEmployee || "Failed to add employee"), "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  const handleUpdate = async () => {
    if (!editEmp) return;
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/employees/${editEmp.id}`, {
        method:"PUT",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({
          role:        editEmp.role,
          designation: editEmp.designation,
          phone:       editEmp.phone,
          team:        editEmp.team,
          department:  editEmp.department,
          is_active:   editEmp.is_active,
        }),
      });
      if (res.ok) { showToast(T.workerUpdated || "Worker updated"); setEditEmp(null); load(); }
      else showToast(T.settingsPageFailedToUpdate || "Failed to update", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  const handleDelete = async (id, name) => {
    if (!window.confirm((T.confirmDeleteEmployeeAction || "Delete {name}? This action cannot be undone.").replace("{name}", name))) return;
    try {
      const res = await fetch(`${API_BASE}/employees/${id}`, {
        method:"DELETE", headers:{ Authorization:`Bearer ${TOKEN()}` },
      });
      if (res.ok) { showToast(T.settingsPageEmployeeDeleted || "Employee deleted"); load(); }
      else showToast(T.settingsPageFailedToDelete || "Failed to delete", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
  };

  const handleResend = async (id) => {
    try {
      const res = await fetch(`${API_BASE}/employees/${id}/resend-invite`, {
        method:"POST", headers:{ Authorization:`Bearer ${TOKEN()}` },
      });
      const d = await res.json();
      showToast(d.invite_sent ? (T.inviteResent || "Invite resent!") : (T.tokenRefreshedEmailFailed || "Token refreshed (email delivery failed)"));
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
  };

  // ── Import Workers (spec §4.4: Import Workers, NOT "Import Employees") ──
  //
  // CSV is parsed on the client and each row is POSTed to /employees.
  // Required columns: name, email. Optional: role, designation, phone,
  // department, team. Rows missing required fields are skipped with a
  // visible error count rather than failing the whole import — that way
  // an admin can fix the offending rows and re-upload without losing
  // the good ones already created.
  const parseCsv = (text) => {
    const lines = text.replace(/\r/g, "").split("\n").filter(l => l.trim());
    if (lines.length < 2) return { ok: false, error: "empty" };
    const headers = lines[0].split(",").map(h => h.trim().toLowerCase());
    if (!headers.includes("name") || !headers.includes("email")) {
      return { ok: false, error: "missing_columns" };
    }
    const rows = lines.slice(1).map(line => {
      const cells = line.split(",");
      const row = {};
      headers.forEach((h, i) => { row[h] = (cells[i] || "").trim(); });
      return row;
    });
    return { ok: true, rows };
  };

  const handleImportFile = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const parsed = parseCsv(String(reader.result || ""));
      if (!parsed.ok && parsed.error === "missing_columns") {
        return showToast(T.importWorkersMissingColumns || "CSV is missing required columns: name, email.", "error");
      }
      if (!parsed.ok) {
        return showToast(T.importWorkersInvalidCsv || "Couldn't read that file as CSV — please check the header row.", "error");
      }
      const valid = parsed.rows.filter(r => r.name && r.email);
      if (valid.length === 0) {
        return showToast(T.importWorkersEmpty || "CSV has no rows to import.", "error");
      }
      setImportState({ phase: "review", message: "", rows: valid, dryRun: false });
    };
    reader.onerror = () => showToast(T.importWorkersInvalidCsv || "Couldn't read that file as CSV.", "error");
    reader.readAsText(file);
  };

  const runImport = async () => {
    const { rows, dryRun } = importState;
    setImportState(s => ({ ...s, phase: "running",
      message: (T.importWorkersInvitationCount || "Sending invitations to {count} worker(s)...").replace("{count}", String(rows.length)),
    }));
    let created = 0, failed = 0, skipped = 0;
    for (const row of rows) {
      const payload = {
        name:        row.name,
        email:       row.email,
        role:        (row.role || "employee").toLowerCase(),
        designation: row.designation || "",
        phone:       row.phone || "",
        department:  row.department || "",
        team:        row.team || "",
      };
      if (dryRun) { created++; continue; }
      try {
        const res = await fetch(`${API_BASE}/employees`, {
          method:"POST",
          headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
          body: JSON.stringify(payload),
        });
        if (res.ok) created++;
        else if (res.status === 409) skipped++;   // duplicate email
        else failed++;
      } catch { failed++; }
    }
    const summary = (T.importWorkersResultSummary || "{created} created, {failed} failed, {skipped} skipped.")
      .replace("{created}", String(created))
      .replace("{failed}", String(failed))
      .replace("{skipped}", String(skipped));
    setImportState({ phase: "done", message: summary, rows: [], dryRun: false });
    showToast(summary, failed > 0 ? "error" : "success");
    if (!dryRun && created > 0) load();
  };

  const StatusBadge = ({ status }) => {
    const map = {
      active:   { bg:"#DCFCE7", color:"#2F8F5C", label:(T.active || "Active") },
      invited:  { bg:"#FEF9C3", color:"#92400E", label:(T.settingsPageInvited || "Invited") },
      inactive: { bg:"#F1F5F9", color:"#475569", label:(T.inactive || "Inactive") },
    };
    const s = map[status] || map.inactive;
    return (
      <span style={{ padding:"2px 10px", borderRadius:20, fontSize:11, fontWeight:700, background:s.bg, color:s.color }}>
        {s.label}
      </span>
    );
  };

  const inp = { width:"100%", padding:"8px 12px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:13, fontFamily:"inherit", outline:"none", boxSizing:"border-box", background:C.surface, color:C.text, colorScheme: isDark ? "dark" : "light" };
  const sel = { ...inp, background:C.surface, cursor:"pointer", width:undefined };

  return (
    <div style={{ padding:"24px 32px" }}>
      <div style={{ display:"flex", justifyContent:"flex-end", gap:10, marginBottom:20 }}>
        {isAdmin && (
          <>
            <button onClick={() => { setImportState({ phase: "idle", message: "", rows: [], dryRun: false }); setShowImport(true); }}
              style={{ padding:"10px 18px", background:C.surface, color:C.brand,
                       border:`1px solid ${C.brand}`, borderRadius:8,
                       fontWeight:700, fontSize:14, cursor:"pointer",
                       fontFamily:"inherit" }}>
              {T.importWorkers || "Import Workers"}
            </button>
            <button onClick={() => setShowInvite(true)}
              style={{ padding:"10px 22px", background:C.brand, color:"#fff", border:"none", borderRadius:8, fontWeight:700, fontSize:14, cursor:"pointer", fontFamily:"inherit" }}>
              + {T.addWorker || "Add Worker"}
            </button>
          </>
        )}
      </div>

      {/* Import Workers modal (spec §4.4 — bulk add path). */}
      {showImport && (
        <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.4)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <div style={{ background:C.surface, borderRadius:12, padding:28, width:520, boxShadow:"0 20px 60px rgba(0,0,0,0.2)", border:`1px solid ${C.border}` }}>
            <p style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:6 }}>{T.importWorkers || "Import Workers"}</p>
            <p style={{ fontSize:13, color:"#475569", marginBottom:14 }}>
              {T.importWorkersDescription || "Upload a CSV with columns: name, email, role, designation, phone, department, team. We'll create or invite each row."}
            </p>

            <input
              type="file" accept=".csv,text/csv" ref={importFileRef}
              onChange={e => handleImportFile(e.target.files?.[0])}
              style={{ display: "block", marginBottom: 12 }}
            />

            {importState.phase === "review" && (
              <>
                <div style={{ background: "#F8FAFC", border: "1px solid #E2E8F0", borderRadius: 8,
                              padding: "10px 12px", fontSize: 13, color: "#0F172A", marginBottom: 12 }}>
                  {importState.rows.length} {importState.rows.length === 1 ? (T.worker || "worker") : (T.workers || "workers")} ready to import.
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, marginBottom: 12, color: C.text }}>
                  <input type="checkbox" checked={importState.dryRun}
                         onChange={e => setImportState(s => ({ ...s, dryRun: e.target.checked }))} />
                  {T.importWorkersDryRun || "Validate without inviting"}
                </label>
              </>
            )}

            {importState.phase === "running" && (
              <div style={{ background: "#F8FAFC", padding: "10px 12px", borderRadius: 8,
                            fontSize: 13, color: "#0F172A", marginBottom: 12 }}>
                {importState.message || (T.importWorkersInProgress || "Importing workers...")}
              </div>
            )}

            {importState.phase === "done" && (
              <div style={{ background: "#ECFDF5", border: "1px solid #BBF7D0", borderRadius: 8,
                            padding: "10px 12px", fontSize: 13, color: "#065F46", marginBottom: 12 }}>
                {importState.message}
              </div>
            )}

            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={() => { setShowImport(false); setImportState({ phase: "idle", message: "", rows: [], dryRun: false }); }}
                style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>
                {T.close || "Close"}
              </button>
              <button onClick={runImport}
                disabled={importState.phase !== "review" || importState.rows.length === 0}
                style={{ padding:"9px 22px", borderRadius:7, border:"none",
                  background:C.brand, color:"#fff", fontWeight:700, fontSize:13,
                  cursor: importState.phase === "review" ? "pointer" : "not-allowed",
                  fontFamily:"inherit",
                  opacity: importState.phase === "review" ? 1 : 0.6 }}>
                {T.importWorkersStart || "Start import"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Invite Modal */}
      {showInvite && (
        <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.4)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <div style={{ background:C.surface, borderRadius:12, padding:28, width:500, boxShadow:"0 20px 60px rgba(0,0,0,0.2)", maxHeight:"90vh", overflowY:"auto", border:`1px solid ${C.border}` }}>
            <p style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:20 }}>{T.addWorker || "Add Worker"}</p>
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:14, marginBottom:14 }}>
              {[
                { lbl:`${T.fullNameRequired || "Full Name"} *`, key:"name", ph:(T.settingsPagePhFullName || "e.g. John Smith") },
                { lbl:`${T.emailAddressRequired || "Email Address"} *`, key:"email", ph:(T.settingsPagePhEmail || "john@company.com"), type:"email" },
                { lbl:(T.designation || "Designation"), key:"designation", ph:(T.settingsPagePhSoftwareEngineer || "e.g. Software Engineer") },
                { lbl:(T.phone || "Phone"), key:"phone", ph:(T.settingsPagePhPhoneNumber || "+91 9876543210") },
                { lbl:(T.department || "Department"), key:"department", ph:(T.settingsPagePhDepartment || "e.g. Engineering") },
              ].map(f => (
                <div key={f.key}>
                  <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{f.lbl}</label>
                  <input type={f.type||"text"} value={inviteForm[f.key]}
                    onChange={e => setInviteForm(p=>({...p,[f.key]:e.target.value}))}
                    placeholder={f.ph} style={inp} />
                </div>
              ))}
              <div>
                <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{T.role || "Role"}</label>
                <select value={inviteForm.role} onChange={e => setInviteForm(p=>({...p,role:e.target.value}))} style={{ ...inp, cursor:"pointer" }}>
                  <option value="employee">{T.employee || "Employee"}</option>
                  <option value="hr">{T.hr || "HR"}</option>
                  <option value="admin">{T.adminLabel || "Admin"}</option>
                </select>
              </div>
              <div>
                <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{T.team_label || "Team"}</label>
                <select value={inviteForm.team} onChange={e => setInviteForm(p=>({...p,team:e.target.value}))} style={{ ...inp, cursor:"pointer" }}>
                  <option value="">{T.noTeam || "No team"}</option>
                  {teams.map(t => <option key={t.id} value={t.name}>{t.name}</option>)}
                </select>
              </div>
            </div>
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end", marginTop:8 }}>
              <button onClick={() => setShowInvite(false)}
                style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={handleInvite} disabled={saving || !inviteForm.name.trim() || !inviteForm.email.trim()}
                style={{ padding:"9px 22px", borderRadius:7, border:"none", background:C.brand, color:"#fff", fontWeight:700, fontSize:13, cursor:"pointer", fontFamily:"inherit",
                  opacity:(!inviteForm.name.trim()||!inviteForm.email.trim()||saving)?0.6:1 }}>
                {saving?(T.sending||"Sending..."):(T.sendInvite||"Send Invite")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Edit Modal */}
      {editEmp && (
        <div style={{ position:"fixed", inset:0, background:isDark?"rgba(0,0,0,0.6)":"rgba(0,0,0,0.4)", zIndex:9000, display:"flex", alignItems:"center", justifyContent:"center" }}>
          <div style={{ background:C.surface, borderRadius:12, padding:28, width:500, boxShadow:"0 20px 60px rgba(0,0,0,0.2)", border:`1px solid ${C.border}` }}>
            <p style={{ fontSize:16, fontWeight:700, color:C.text, marginBottom:4 }}>{T.editWorker || "Edit Worker"}</p>
            <p style={{ fontSize:13, color:"#667085", marginBottom:20 }}>{editEmp.email}</p>
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:14, marginBottom:14 }}>
              <div>
                <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{T.role || "Role"}</label>
                <select value={editEmp.role} onChange={e => setEditEmp(p=>({...p,role:e.target.value}))} style={{ ...inp, cursor:"pointer" }}>
                  <option value="employee">{T.employee || "Employee"}</option>
                  <option value="hr">{T.hr || "HR"}</option>
                  <option value="admin">{T.adminLabel || "Admin"}</option>
                </select>
              </div>
              <div>
                <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{T.team_label || "Team"}</label>
                <select value={editEmp.team||""} onChange={e => setEditEmp(p=>({...p,team:e.target.value}))} style={{ ...inp, cursor:"pointer" }}>
                  <option value="">{T.noTeam || "No team"}</option>
                  {teams.map(t => <option key={t.id} value={t.name}>{t.name}</option>)}
                </select>
              </div>
              {[
                { lbl: T.designation || "Designation", key:"designation" },
                { lbl: T.department || "Department",  key:"department" },
                { lbl: T.phone || "Phone",       key:"phone" },
              ].map(f => (
                <div key={f.key}>
                  <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{f.lbl}</label>
                  <input value={editEmp[f.key]||""} onChange={e => setEditEmp(p=>({...p,[f.key]:e.target.value}))} style={inp} />
                </div>
              ))}
              <div>
                <label style={{ fontSize:12, fontWeight:600, color:C.text, display:"block", marginBottom:5 }}>{T.status || "Status"}</label>
                <select value={editEmp.is_active?"active":"inactive"} onChange={e => setEditEmp(p=>({...p,is_active:e.target.value==="active"}))} style={{ ...inp, cursor:"pointer" }}>
                  <option value="active">{T.active || "Active"}</option>
                  <option value="inactive">{T.inactive || "Inactive"}</option>
                </select>
              </div>
            </div>
            <div style={{ display:"flex", gap:10, justifyContent:"flex-end" }}>
              <button onClick={() => setEditEmp(null)}
                style={{ padding:"9px 20px", borderRadius:7, border:`1px solid ${C.border}`, background:C.surface, color:C.text, fontWeight:600, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={handleUpdate} disabled={saving}
                style={{ padding:"9px 22px", borderRadius:7, border:"none", background:C.brand, color:"#fff", fontWeight:700, fontSize:13, cursor:"pointer", fontFamily:"inherit" }}>
                {saving?(T.saving||"Saving..."):(T.saveChanges||"Save Changes")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Employee Table */}
      <div style={{ background:C.surface, borderRadius:10, border:`1px solid ${C.border}`, overflow:"hidden" }}>
        <div style={{ display:"grid", gridTemplateColumns:"2fr 2fr 1fr 1fr 1fr 1fr 140px", padding:"10px 20px", borderBottom:`1px solid ${C.border}`, background:C.surfaceAlt || C.bg }}>
          {[T.name||"Name",T.email||"Email",T.role||"Role",T.team_label||"Team",T.department||"Dept",T.status||"Status",T.actions||"Actions"].map(h => (
            <span key={h} style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:"uppercase", letterSpacing:0.5 }}>{h}</span>
          ))}
        </div>
        {loading ? (
          <div style={{ padding:"48px 0", textAlign:"center", color:C.textSub }}>{T.loadingWorkers || "Loading workers..."}</div>
        ) : employees.length === 0 ? (
          <div style={{ padding:"60px 0", textAlign:"center", color:C.textLight }}>
            <div style={{ fontSize:36, marginBottom:12 }}>👤</div>
            <p style={{ fontWeight:600, color:C.textSub }}>{T.noWorkersFound || "No workers yet"}</p>
            <p style={{ fontSize:13, color:C.textLight }}>{T.addWorkerHint || 'Add workers, contractors, employees, administrators, and other users who need access to this workspace.'}</p>
          </div>
        ) : employees.map(emp => (
          <div key={emp.id} style={{ display:"grid", gridTemplateColumns:"2fr 2fr 1fr 1fr 1fr 1fr 140px", padding:"12px 20px", borderBottom:`1px solid ${C.borderLight}`, alignItems:"center" }}>
            <span style={{ fontWeight:600, fontSize:13, color:C.text }}>{emp.name}</span>
            <span style={{ fontSize:12, color:C.textSub, overflow:"hidden", textOverflow:"ellipsis" }}>{emp.email}</span>
            <span style={{ fontSize:13, color:C.text, textTransform:"capitalize" }}>{emp.role}</span>
            <span style={{ fontSize:13, color:C.text }}>{emp.team||"—"}</span>
            <span style={{ fontSize:13, color:C.text }}>{emp.department||"—"}</span>
            <StatusBadge status={emp.onboarding_status} />
            <div style={{ display:"flex", gap:4, alignItems:"center", flexWrap:"wrap" }}>
              {emp.onboarding_status==="invited" && isAdmin && (
                <button onClick={() => handleResend(emp.id)} title={T.resendButton || "Resend"}
                  style={{ background:"none", border:`1px solid ${C.border}`, borderRadius:6, fontSize:11, color:C.text, cursor:"pointer", padding:"3px 8px", fontFamily:"inherit" }}>
                  {T.resendButton || "Resend"}
                </button>
              )}
              {isAdmin && (
                <>
                  <button onClick={() => setEditEmp({...emp})} title={T.settingsPageEditEmployee || "Edit employee"}
                    style={{ background:"none", border:"none", color:C.brand, cursor:"pointer", fontSize:15, padding:"2px 4px" }}>✏️</button>
                  <button onClick={() => handleDelete(emp.id, emp.name)} title={T.settingsPageDeleteEmployee || "Delete employee"}
                    style={{ background:"none", border:"none", color:C.danger, cursor:"pointer", fontSize:15, padding:"2px 4px" }}>🗑</button>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── REBRAND SETTINGS TAB ──────────────────────────────────────────────────────
function RebrandTab({ isAdmin, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const [companyName, setCompanyName] = useState("");
  const [logoUrl, setLogoUrl]         = useState(null);
  const [saving, setSaving]           = useState(false);
  const fileRef = useRef(null);

  useEffect(() => {
    fetch(`${API_BASE}/org/settings`, { headers:{ Authorization:`Bearer ${TOKEN()}` } })
      .then(r => r.json())
      .then(d => {
        if (d.company_name) setCompanyName(d.company_name);
        if (d.logo_url)     setLogoUrl(d.logo_url);
      })
      .catch(() => {});
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await fetch(`${API_BASE}/org/settings`, {
        method:"PUT",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({ company_name: companyName }),
      });
      if (res.ok) showToast(T.organisationInfoSaved || "Organization info saved!");
      else showToast(T.settingsPageFailedToSave || "Failed to save", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  const handleLogoUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("logo", file);
    try {
      const res = await fetch(`${API_BASE}/org/logo`, {
        method:"POST", headers:{ Authorization:`Bearer ${TOKEN()}` }, body:form,
      });
      const d = await res.json();
      if (res.ok) { setLogoUrl(d.logo_url); showToast(T.logoUploaded || "Logo uploaded!"); }
      else showToast(d.detail || (T.settingsPageUploadFailed || "Upload failed"), "error");
    } catch { showToast(T.settingsPageUploadFailed || "Upload failed", "error"); }
  };

  const handleRemoveLogo = async () => {
    try {
      const res = await fetch(`${API_BASE}/org/logo`, {
        method:"DELETE", headers:{ Authorization:`Bearer ${TOKEN()}` },
      });
      if (res.ok) { setLogoUrl(null); showToast(T.logoRemoved || "Logo removed"); }
      else showToast(T.settingsPageFailed || "Failed", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
  };

  const fullLogoUrl = logoUrl ? (logoUrl.startsWith("http") ? logoUrl : `${API_BASE}${logoUrl}`) : null;

  return (
    <div style={{ padding:"40px 80px", maxWidth:700 }}>
      <div style={{ marginBottom:32 }}>
        <label style={{ fontSize:14, fontWeight:600, color:C.text, display:"block", marginBottom:8 }}>{T.companyName || "Company Name"}</label>
        <input value={companyName} onChange={e => setCompanyName(e.target.value)}
          disabled={!isAdmin} placeholder={T.companyNamePlaceholder || "Your company name"}
          style={{ width:"100%", padding:"10px 14px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:14, fontFamily:"inherit", outline:"none", boxSizing:"border-box", background:isAdmin?C.surface:(C.surfaceAlt || C.bg), color:C.text, colorScheme: isDark ? "dark" : "light" }} />
      </div>
      <div style={{ marginBottom:32 }}>
        <label style={{ fontSize:14, fontWeight:600, color:C.text, display:"block", marginBottom:4 }}>{T.companyLogo || "Company Logo"}</label>
        <p style={{ fontSize:13, color:C.textSub, marginBottom:16 }}>{T.logoUploadDescription || "You can replace the Temveris logo by uploading your company logo."}</p>
        <div style={{ display:"flex", alignItems:"center", gap:20 }}>
          <div style={{ width:80, height:80, border:`1px solid ${C.border}`, borderRadius:8, overflow:"hidden", display:"flex", alignItems:"center", justifyContent:"center", background:C.surfaceAlt || C.bg, flexShrink:0 }}>
            {fullLogoUrl ? (
              <img src={fullLogoUrl} alt={T.settingsPageCompanyLogoAlt || "Company logo"} style={{ width:"100%", height:"100%", objectFit:"contain" }} />
            ) : (
              <span style={{ fontSize:28 }}>🏢</span>
            )}
          </div>
          {isAdmin && (
            <div style={{ display:"flex", flexDirection:"column", gap:8 }}>
              <input type="file" accept="image/jpeg,image/png,image/gif,image/webp" ref={fileRef} style={{ display:"none" }} onChange={handleLogoUpload} />
              <button onClick={() => fileRef.current?.click()}
                style={{ background:"none", border:"none", color:C.brand, fontWeight:600, fontSize:14, cursor:"pointer", fontFamily:"inherit", padding:0, textAlign:"left" }}>
                {T.uploadLogo || "Upload your company logo"}
              </button>
              {fullLogoUrl && (
                <button onClick={handleRemoveLogo}
                  style={{ background:"none", border:"none", color:C.danger, fontWeight:600, fontSize:14, cursor:"pointer", fontFamily:"inherit", padding:0, textAlign:"left" }}>
                  {T.removeLogo || "Remove logo"}
                </button>
              )}
            </div>
          )}
        </div>
        <p style={{ fontSize:12, color:C.textLight, marginTop:10 }}>{T.logoFormatHelp || "Company logo should be in jpg or png format."}</p>
      </div>
      {isAdmin && (
        <button onClick={handleSave} disabled={saving}
          style={{ padding:"11px 26px", background:C.brand, color:"#fff", border:"none", borderRadius:8, fontWeight:700, fontSize:14, cursor:"pointer", fontFamily:"inherit" }}>
          {saving?(T.saving||"Saving..."):(T.save||"Save")}
        </button>
      )}
    </div>
  );
}

// ── RISK USER SETTINGS TAB ────────────────────────────────────────────────────
function RiskUserSettingsTab({ isAdmin, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const [s, setS]         = useState({ risk_alert_enabled:"true", work_hours_threshold:"8:45" });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving]   = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/settings`, { headers:{ Authorization:`Bearer ${TOKEN()}` } })
      .then(r => r.json())
      .then(d => {
        if (d.settings) setS(prev => ({ ...prev, ...d.settings }));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const set = (key, val) => setS(p => ({ ...p, [key]: val }));

  const THRESHOLD_OPTIONS = [
    { label:(T.settingsPageSixHours || "6 hours"),      value:"6:00" },
    { label:(T.settingsPageSixHrs30Min || "6 hrs 30 min"), value:"6:30" },
    { label:(T.settingsPageSevenHours || "7 hours"),      value:"7:00" },
    { label:(T.settingsPageSevenHrs30Min || "7 hrs 30 min"), value:"7:30" },
    { label:(T.settingsPageEightHours || "8 hours"),      value:"8:00" },
    { label:(T.settingsPageEightHrs30Min || "8 hrs 30 min"), value:"8:30" },
    { label:(T.settingsPageEightHrs45Min || "8 hrs 45 min"), value:"8:45" },
    { label:(T.settingsPageNineHours || "9 hours"),      value:"9:00" },
    { label:(T.settingsPageNineHrs30Min || "9 hrs 30 min"), value:"9:30" },
    { label:(T.settingsPageTenHours || "10 hours"),     value:"10:00" },
  ];

  const handleSave = async () => {
    if (saving) return;
    setSaving(true);
    // Optimistic toast — the request is small and the server is fast now,
    // so the user shouldn't have to wait to see confirmation.
    showToast(T.savingRiskSettings || "Saving risk settings...");
    try {
      const res = await fetch(`${API_BASE}/settings/update`, {
        method:"PUT",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify({ settings: {
          risk_alert_enabled:   s.risk_alert_enabled,
          work_hours_threshold: s.work_hours_threshold,
        }}),
      });
      if (res.ok) showToast(T.riskSettingsSaved || "Risk settings saved!");
      else showToast(T.settingsPageFailedToSave || "Failed to save", "error");
    } catch { showToast(T.settingsPageNetworkError || "Network error", "error"); }
    setSaving(false);
  };

  if (loading) return <div style={{ padding:48, textAlign:"center", color:C.textSub }}>{T.loadingSettings || "Loading..."}</div>;

  return (
    <div style={{ padding:"32px 40px", maxWidth:680 }}>
      <p style={{ fontSize:14, color:C.textSub, marginBottom:28, lineHeight:1.7 }}>
        {T.riskSettingsDescription || "Configure the thresholds that trigger HR risk alerts for employee work patterns."}
      </p>

      <div style={{ background:C.bg, border:`1px solid ${C.border}`, borderRadius:10, padding:"20px 24px", marginBottom:16 }}>
        <p style={{ fontSize:15, fontWeight:700, color:C.text, marginBottom:20 }}>{T.alertConfiguration || "Alert Configuration"}</p>

        <div style={{ display:"flex", alignItems:"center", gap:12, marginBottom:24, padding:"14px 16px", background:C.surface, border:`1px solid ${C.border}`, borderRadius:8 }}>
          <Toggle on={s.risk_alert_enabled !== "false"} onChange={v => set("risk_alert_enabled", String(v))} disabled={!isAdmin} />
          <div>
            <p style={{ fontSize:14, fontWeight:600, color:C.text, margin:0 }}>{T.enableRiskAlerts || "Enable Risk Alerts"}</p>
            <p style={{ fontSize:12, color:C.textSub, margin:0, marginTop:2 }}>
              {T.enableRiskAlertsHelp || "Send an HR email notification when an employee works below the minimum threshold"}
            </p>
          </div>
        </div>

        <div style={{ marginBottom:20 }}>
          <label style={{ fontSize:13, fontWeight:600, color:C.text, display:"block", marginBottom:6 }}>
            {T.minimumWorkHoursThreshold || "Minimum Work Hours Threshold"}
          </label>
          <p style={{ fontSize:12, color:C.textSub, marginBottom:8 }}>
            {T.workHoursThresholdHelp || "Risk alert fires if an employee's daily work time falls below this value"}
          </p>
          <select
            value={s.work_hours_threshold}
            onChange={e => set("work_hours_threshold", e.target.value)}
            disabled={!isAdmin}
            style={{ padding:"9px 16px", border:`1px solid ${C.border}`, borderRadius:8, fontSize:13, color:C.text,
              background: isAdmin ? C.surface : C.bg,
              cursor:isAdmin?"pointer":"not-allowed", outline:"none", fontFamily:"inherit", minWidth:200,
              colorScheme: isDark ? "dark" : "light" }}>
            {THRESHOLD_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
      </div>

      {isAdmin && (
        <button onClick={handleSave} disabled={saving}
          style={{ padding:"11px 26px", background:C.brand, color:"#fff", border:"none", borderRadius:8, fontWeight:700, fontSize:14, cursor:"pointer", fontFamily:"inherit" }}>
          {saving?(T.saving||"Saving..."):(T.save||"Save")}
        </button>
      )}
    </div>
  );
}

// ── LEAVE SETTINGS TAB ────────────────────────────────────────────────────────
function LeaveSettingsTab({ isAdmin, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const [policies, setPolicies] = useState([]);
  const [loading, setLoading]   = useState(true);
  const [saving, setSaving]     = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/leave/policies`, { headers:{ Authorization:`Bearer ${TOKEN()}` } })
      .then(r => r.json())
      .then(d => { if (Array.isArray(d)) setPolicies(d); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const updatePolicy = (idx, key, val) =>
    setPolicies(prev => prev.map((p, i) => i === idx ? { ...p, [key]: val } : p));

  const hasNoAnnualCap = (p) => !!(p?.no_annual_limit || (p?.is_earned && Number(p?.annual_limit ?? 0) === 0));

  const handleSave = async () => {
    setSaving(true);
    const payload = policies.map(p => hasNoAnnualCap(p)
      ? { ...p, annual_limit: 0, doc_required: false, monthly_accrual: 0 }
      : p
    );
    try {
      const res = await fetch(`${API_BASE}/leave/policies`, {
        method:"PUT",
        headers:{ Authorization:`Bearer ${TOKEN()}`, "Content-Type":"application/json" },
        body: JSON.stringify(payload),
      });
      const d = await res.json();
      if (res.ok) showToast(T.leavePoliciesSaved || "Leave policies saved!");
      else showToast(d.detail || (T.failedToSave || "Failed to save"), "error");
    } catch { showToast(T.networkError || "Network error", "error"); }
    setSaving(false);
  };

  if (loading) return <div style={{ padding:48, textAlign:"center", color:C.textSub }}>{T.loading || "Loading..."}</div>;

  const numInp = (val, onChange) => ({
    type:"number", value:val,
    onChange: e => isAdmin && onChange(+e.target.value),
    disabled: !isAdmin,
    style:{
      width:80, padding:"6px 10px", border:`1px solid ${C.border}`, borderRadius:7,
      fontSize:13, fontFamily:"inherit", outline:"none",
      background: isAdmin ? C.surface : (C.surfaceAlt || C.bg),
      color: C.text,
      colorScheme: isDark ? "dark" : "light",
      textAlign:"center",
    },
  });

  return (
    <div style={{ padding:"24px 32px" }}>
      <p style={{ fontSize:14, color:C.textSub, marginBottom:20, lineHeight:1.7 }}>
        {T.leaveSettingsDescription || "Configure leave type allowances, notice requirements, documentation policies, and monthly accrual rates."}
      </p>

      <div style={{ background:C.surface, borderRadius:10, border:`1px solid ${C.border}`, overflow:"hidden", marginBottom:20 }}>
        <div style={{ display:"grid", gridTemplateColumns:"2fr 1.2fr 1fr 1fr 1fr", padding:"11px 20px", borderBottom:`1px solid ${C.border}`, background:C.surfaceAlt || C.bg }}>
          {[T.leaveType||"Leave Type",T.annualLimitDays||"Annual Limit (days)",T.noticeDays||"Min Notice Days",T.documentRequired||"Doc Required",T.monthlyAccrual||"Monthly Accrual"].map(h => (
            <span key={h} style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:"uppercase", letterSpacing:0.4 }}>{h}</span>
          ))}
        </div>
        {policies.map((p, i) => {
          const noAnnualCap = hasNoAnnualCap(p);
          return (
            <div key={p.leave_type} style={{ display:"grid", gridTemplateColumns:"2fr 1.2fr 1fr 1fr 1fr", padding:"14px 20px", borderBottom:`1px solid ${C.borderLight}`, alignItems:"center" }}>
              <div>
                <span style={{ fontWeight:600, fontSize:14, color:C.text }}>{p.leave_type}</span>
              </div>
              {noAnnualCap ? (
                <span style={{ fontSize:13, fontWeight:700, color:C.brand }}>{T.noAnnualCap || "No annual cap"}</span>
              ) : (
                <input {...numInp(p.annual_limit, v => updatePolicy(i, "annual_limit", v))} min={0} max={365} />
              )}
              <input {...numInp(p.notice_days,  v => updatePolicy(i, "notice_days", v))}  min={0} max={60} />
              <div style={{ display:"flex", alignItems:"center" }}>
                <input type="checkbox" checked={noAnnualCap ? false : p.doc_required} disabled={!isAdmin || noAnnualCap}
                  onChange={e => isAdmin && !noAnnualCap && updatePolicy(i, "doc_required", e.target.checked)}
                  style={{ width:16, height:16, accentColor:C.brand, cursor:isAdmin && !noAnnualCap ? "pointer" : "not-allowed" }} />
              </div>
              {noAnnualCap ? (
                <span style={{ fontSize:13, color:C.textSub }}>-</span>
              ) : (
                <input {...numInp(p.monthly_accrual, v => updatePolicy(i, "monthly_accrual", v))} min={0} max={5} step="0.25" />
              )}
            </div>
          );
        })}
      </div>

      <div style={{ fontSize:12, color:C.textSub, marginBottom:20, lineHeight:1.6 }}>
        <p>{T.leaveFieldsHelp || "Annual Limit: max days per year. | Notice Days: minimum days advance notice required. | Monthly Accrual: days earned per month (0 = fixed allocation)."}</p>
        <p>{T.noAnnualCapHelp || "No annual cap: employees apply by listing the extra dates already worked; HR verifies those dates during approval."}</p>
      </div>

      {isAdmin && (
        <button onClick={handleSave} disabled={saving}
          style={{ padding:"11px 26px", background:C.brand, color:"#fff", border:"none", borderRadius:8, fontWeight:700, fontSize:14, cursor:"pointer", fontFamily:"inherit" }}>
          {saving?(T.saving||"Saving..."):(T.save||"Save")}
        </button>
      )}
    </div>
  );
}

// ── PUBLIC HOLIDAYS TAB ───────────────────────────────────────────────────────
// Workspace-level holiday governance. Preserves the original Nager.Date
// integration + /api/workspace/public-holidays profile storage. Admin and
// employee holiday views (Dashboard / AdminDashboard) are intentionally
// unchanged — only this Settings surface gets the richer governance UI
// (KPI cards, tabbed Holiday list / Import & conflicts / Applicability
// rules / Audit trail, manual add, conflict resolution).
const HOLIDAY_STORAGE_KEY       = "zt_org_holiday_country";
const HOLIDAY_MANUAL_KEY        = "zt_org_holiday_manual";
const HOLIDAY_APPLICABILITY_KEY = "zt_org_holiday_applicability";
const HOLIDAY_AUDIT_KEY         = "zt_org_holiday_audit";
// Imported (read-only) holidays the admin has resolved via the Conflicts
// view. Stored as IDs so `allHolidays` can filter them out on every render.
const HOLIDAY_SUPPRESSED_KEY    = "zt_org_holiday_suppressed";

const HOLIDAY_FLAG_MAP = {
  US:"🇺🇸", IN:"🇮🇳", GB:"🇬🇧", CA:"🇨🇦", AU:"🇦🇺",
  DE:"🇩🇪", FR:"🇫🇷", JP:"🇯🇵", CN:"🇨🇳", BR:"🇧🇷",
  IT:"🇮🇹", ES:"🇪🇸", NL:"🇳🇱", SE:"🇸🇪", NO:"🇳🇴",
  IE:"🇮🇪", MX:"🇲🇽", ZA:"🇿🇦", NZ:"🇳🇿", SG:"🇸🇬",
};

// ── Country calendars ────────────────────────────────────────────────────────
// This module used to carry its OWN 21-entry Indian calendar for 2024-2027 plus
// a fixed-date fallback, because Nager.Date does not cover India. Meanwhile
// AdminDashboard had no fallback at all and `app/holidays/router.py` shipped a
// THIRD, 13-entry list that disagreed on the dates of Holi and Bakrid.
//
// All three are gone. The authoritative calendar lives in
// `backend/app/holidays/catalog.py` and reaches both admin pages through
// `api/holidays.js`. Nothing here talks to an external holiday provider, and
// there is nothing left to keep in sync.

const HOLIDAY_TYPE_COLORS = {
  Public:      { bg:"#DBEAFE", fg:"#1E40AF" },
  Bank:        { bg:"#DBEAFE", fg:"#1E40AF" },
  Optional:    { bg:"#F3F4F6", fg:"#4B5563" },
  Observance:  { bg:"#F3F4F6", fg:"#4B5563" },
  Authorities: { bg:"#E0E7FF", fg:"#3730A3" },
  School:      { bg:"#FEF3C7", fg:"#92400E" },
  Company:     { bg:"#EDE9FE", fg:"#5B21B6" },
};

const readJSON = (key, fallback) => {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; }
  catch { return fallback; }
};
const writeJSON = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* localStorage unavailable */ }
};

function PublicHolidaysTab({ isAdmin, showToast }) {
  const { T } = useLanguage();
  const { C, isDark } = useTheme();
  const navigate = useNavigate();

  // ── Preserved backend state ────────────────────────────────────────────────
  const [countries, setCountries]               = useState([]);
  const [country, setCountry]                   = useState(() => {
    try { return localStorage.getItem(HOLIDAY_STORAGE_KEY) || ""; } catch { return ""; }
  });
  const [year, setYear]                         = useState(new Date().getFullYear());
  const [holidays, setHolidays]                 = useState([]);
  const [profiles, setProfiles]                 = useState([]);
  const [loadingCountries, setLoadingCountries] = useState(true);
  const [loadingHolidays, setLoadingHolidays]   = useState(false);
  const [error, setError]                       = useState(null);

  // Searchable country dropdown
  const [countryOpen, setCountryOpen]   = useState(false);
  const [countryQuery, setCountryQuery] = useState("");
  const countryDropdownRef              = useRef(null);
  const countrySearchRef                = useRef(null);

  // ── New UI state ───────────────────────────────────────────────────────────
  const [activeTab, setActiveTab]       = useState("list"); // list | import | applicability | audit
  const [viewMode, setViewMode]         = useState("table");
  const [searchQuery, setSearchQuery]   = useState("");
  const [regionFilter, setRegionFilter] = useState("all");
  const [typeFilter, setTypeFilter]     = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");

  const [manualHolidays, setManualHolidays] = useState(() => readJSON(HOLIDAY_MANUAL_KEY, []));
  const [applicability, setApplicability]   = useState(() => readJSON(HOLIDAY_APPLICABILITY_KEY, {
    leave: true, attendance: true, overtime: true, calendar: true, payroll: false,
  }));
  const [auditEvents, setAuditEvents]       = useState(() => readJSON(HOLIDAY_AUDIT_KEY, []));
  const [suppressedIds, setSuppressedIds]   = useState(() => {
    const arr = readJSON(HOLIDAY_SUPPRESSED_KEY, []);
    return new Set(Array.isArray(arr) ? arr : []);
  });

  const [showAdd, setShowAdd] = useState(false);
  const [draft, setDraft]     = useState({
    name: "", date: "", country: "", region: "Federal",
    type: "Company", payImpact: "Paid",
  });

  // ── Country dropdown listeners ─────────────────────────────────────────────
  useEffect(() => {
    if (!countryOpen) return;
    const onClick = (e) => {
      if (countryDropdownRef.current && !countryDropdownRef.current.contains(e.target)) setCountryOpen(false);
    };
    const onKey = (e) => { if (e.key === "Escape") setCountryOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [countryOpen]);

  useEffect(() => {
    if (countryOpen && countrySearchRef.current) countrySearchRef.current.focus();
    if (!countryOpen) setCountryQuery("");
  }, [countryOpen]);

  // ── Supported countries ────────────────────────────────────────────────────
  // From the Temveris catalog, which merges the curated calendars (India, UAE)
  // over the external provider's list and stays useful when that provider is
  // unreachable. This page used to call the provider directly and stitch India
  // in by hand; Manage Holidays did neither, which is why the two pages
  // disagreed about which countries existed.
  useEffect(() => {
    let cancelled = false;
    holidayApi.fetchCountries()
      .then((payload) => {
        if (cancelled) return;
        setCountries((payload.countries || []).map((c) => ({
          name: c.country_name,
          countryCode: c.country_code,
          support: c.support,
        })));
        if (payload.provider_available === false) {
          setError(T.settingsPageProviderUnavailable
            || "The external holiday provider is unavailable — countries Temveris maintains itself are still listed.");
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCountries([]);
          setError(T.settingsPageCouldNotLoadCountryList || "Could not load country list");
        }
      })
      .finally(() => { if (!cancelled) setLoadingCountries(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Workspace public-holiday profiles ──────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/workspace/public-holidays`, { headers: { Authorization: `Bearer ${TOKEN()}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (cancelled || !d) return;
        setProfiles(Array.isArray(d.profiles) ? d.profiles : []);
        const first = Array.isArray(d.profiles) ? d.profiles[0] : null;
        if (first && !country) {
          setCountry(first.country_code || "");
          setYear(first.year || new Date().getFullYear());
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Holidays for current country/year ──────────────────────────────────────
  // One call, one code path. India no longer needs a special case here: the
  // backend decides between a curated calendar and the external provider, so
  // this page cannot fall out of step with Manage Holidays the way it did when
  // each carried its own Indian calendar.
  useEffect(() => {
    if (!country) { setHolidays([]); setError(null); return; }
    let cancelled = false;
    setLoadingHolidays(true);
    setError(null);
    holidayApi.fetchCountryCalendar(country, year)
      .then((payload) => {
        if (cancelled) return;
        setHolidays(payload.holidays || []);
        if ((payload.holidays || []).length === 0) {
          // An empty calendar is a RESULT, not a failure — said differently
          // from a provider outage, which the single old error string could not.
          setError(T.settingsPageNoHolidaysFoundForCountryYear
            || "No public holidays were found for this country and year");
        } else if (payload.partial) {
          setError(T.settingsPagePartialCalendar
            || "Only fixed-date holidays are available for this year. Add the rest manually.");
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setHolidays([]);
        setError(holidayApi.holidayErrorMessage(err));
      })
      .finally(() => { if (!cancelled) setLoadingHolidays(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [country, year]);

  // Persist local-only state
  useEffect(() => writeJSON(HOLIDAY_MANUAL_KEY, manualHolidays), [manualHolidays]);
  useEffect(() => writeJSON(HOLIDAY_APPLICABILITY_KEY, applicability), [applicability]);
  useEffect(() => writeJSON(HOLIDAY_AUDIT_KEY, auditEvents), [auditEvents]);
  useEffect(() => writeJSON(HOLIDAY_SUPPRESSED_KEY, [...suppressedIds]), [suppressedIds]);

  // ── Helpers ────────────────────────────────────────────────────────────────
  const logAudit = (eventType, message, meta = {}) => {
    const ev = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      type: eventType,
      message,
      actor: localStorage.getItem("zt_user_name") || localStorage.getItem("email") || "Admin",
      ts: new Date().toISOString(),
      meta,
    };
    setAuditEvents((prev) => [ev, ...prev].slice(0, 200));
  };

  const handleCountryChange = (next) => {
    setCountry(next);
    try { localStorage.setItem(HOLIDAY_STORAGE_KEY, next); } catch { /* localStorage unavailable */ }
    if (showToast && next) {
      const c = countries.find((x) => x.countryCode === next);
      showToast(`Holidays set to ${c ? c.name : next}`);
    }
  };

  const saveCountryProfile = async () => {
    if (!country) return showToast && showToast(T.settingsPageSelectCountryFirst || "Select a country first", "error");
    const selected = countries.find((x) => x.countryCode === country);
    try {
      const res = await fetch(`${API_BASE}/api/workspace/public-holidays/profile`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${TOKEN()}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          country_code: country,
          country_name: selected?.name || country,
          year,
        }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || "Failed");
      setProfiles(d.profiles || []);
      logAudit(
        "holiday.imported",
        `imported ${selected?.name || country} ${year} calendar — ${holidays.length} holidays`,
        { country: selected?.name || country, year, count: holidays.length },
      );
      showToast && showToast(T.settingsPageCountryProfileSaved || "Country holiday profile saved");
    } catch (e) {
      showToast && showToast(e.message || (T.settingsPageFailedToSaveHolidayProfile || "Failed to save holiday profile"), "error");
    }
  };

  const fmtDate = (iso) => {
    try { return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
    catch { return iso; }
  };
  const fmtDay = (iso) => {
    try { return new Date(iso).toLocaleDateString(undefined, { weekday: "long" }); } catch { return ""; }
  };
  const fmtTs = (iso) => {
    try {
      const d = new Date(iso);
      const today = new Date();
      const sameDay = d.toDateString() === today.toDateString();
      const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (sameDay) return `Today, ${time}`;
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) + ", " + time;
    } catch { return iso; }
  };

  // Combine the catalog calendar with this browser's manual entries.
  // Suppressed IDs are imported holidays the admin has resolved via the
  // Conflicts view — filter them out so the conflict disappears from the UI.
  //
  // The catalog rows already arrive normalized (name / date / holiday_type /
  // description / estimated), so there is no provider-specific field juggling
  // left here — that mapping lived in two places and disagreed in both.
  const allHolidays = useMemo(() => {
    const imported = (holidays || [])
      .map((h) => ({
        id: `catalog-${h.date}-${h.name}`,
        name: h.name,
        date: h.date,
        country: country,
        // Read from the dedicated `regional` field, NOT from holiday_type.
        // These are independent: India's New Year's Day is Optional and
        // nationwide, while Scotland's 2 January is Public and regional.
        // Inferring one from the other labelled every restricted Indian
        // holiday "Regional", which was simply false.
        region: h.regional
          ? ((h.regions || []).join(", ") || "Regional")
          : "Federal",
        regional: !!h.regional,
        type: h.holiday_type,
        source: "Import",
        payImpact: "Paid",
        status: "Active",
        estimated: !!h.estimated,
        isManual: false,
      }))
      .filter((h) => !suppressedIds.has(h.id));
    const manual = (manualHolidays || [])
      .filter((h) => !suppressedIds.has(h.id))
      .map((h) => ({ ...h, isManual: true }));
    return [...imported, ...manual];
  }, [holidays, manualHolidays, country, suppressedIds]);

  // Auto-detect conflicts (same country+date with different names)
  const conflicts = useMemo(() => {
    const byKey = {};
    const out = [];
    for (const h of allHolidays) {
      const k = `${h.country}-${h.date}`;
      if (byKey[k]) {
        if ((byKey[k].name || "").toLowerCase() !== (h.name || "").toLowerCase()) {
          out.push({ id: k + "-" + out.length, date: h.date, country: h.country, existing: byKey[k], imported: h });
        }
      } else { byKey[k] = h; }
    }
    return out;
  }, [allHolidays]);

  const conflictKeys = useMemo(() => {
    const set = new Set();
    conflicts.forEach((c) => {
      set.add(`${c.country}-${c.date}-${c.existing.name}`);
      set.add(`${c.country}-${c.date}-${c.imported.name}`);
    });
    return set;
  }, [conflicts]);

  // Apply filters + tag conflicting rows
  const filteredHolidays = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return allHolidays
      .map((h) => ({ ...h, status: conflictKeys.has(`${h.country}-${h.date}-${h.name}`) ? "Conflict" : h.status }))
      .filter((h) => {
        if (q && !(h.name || "").toLowerCase().includes(q)) return false;
        if (regionFilter !== "all" && h.region !== regionFilter) return false;
        if (typeFilter   !== "all" && h.type   !== typeFilter)   return false;
        if (statusFilter !== "all" && h.status !== statusFilter) return false;
        return true;
      });
  }, [allHolidays, searchQuery, regionFilter, typeFilter, statusFilter, conflictKeys]);

  // KPIs
  const countriesCovered = useMemo(() => {
    const s = new Set([...(profiles || []).map((p) => p.country_code), ...(country ? [country] : [])]);
    return [...s].filter(Boolean);
  }, [profiles, country]);

  const activeHolidayCount = filteredHolidays.length;
  const todayISO = new Date().toISOString().slice(0, 10);
  const nextHoliday = useMemo(() => {
    const upcoming = allHolidays.filter((h) => h.date >= todayISO).sort((a, b) => a.date.localeCompare(b.date));
    return upcoming[0] || null;
  }, [allHolidays, todayISO]);
  const lastImport = useMemo(() => auditEvents.find((e) => e.type === "holiday.imported"), [auditEvents]);

  // Manual add / archive
  const saveManual = () => {
    if (!draft.name.trim() || !draft.date) {
      showToast && showToast(T.settingsPageHolidayNameDateRequired || "Holiday name and date are required", "error");
      return;
    }
    const next = {
      id: `manual-${Date.now().toString(36)}`,
      name: draft.name.trim(),
      date: draft.date,
      country: draft.country || country || "US",
      region: draft.region || "All regions",
      type: draft.type || "Company",
      source: "Manual",
      payImpact: draft.payImpact || "Paid",
      status: "Active",
    };
    setManualHolidays((prev) => [...prev, next]);
    logAudit("holiday.created", `created manual holiday — ${next.name} · ${fmtDate(next.date)} · ${next.region}`, next);
    showToast && showToast(`Added ${next.name}`);
    setShowAdd(false);
    setDraft({ name: "", date: "", country: "", region: "Federal", type: "Company", payImpact: "Paid" });
  };

  const archiveManual = (id) => {
    const target = manualHolidays.find((h) => h.id === id);
    if (!target) return;
    setManualHolidays((prev) => prev.filter((h) => h.id !== id));
    logAudit("holiday.archived", `archived holiday — ${target.name} · ${fmtDate(target.date)}`, target);
    showToast && showToast(`Archived ${target.name}`);
  };

  // Conflict resolution actions. Manual entries are deleted outright;
  // imported entries can't be mutated upstream, so we record their IDs in
  // `suppressedIds` and `allHolidays` filters them out on the next render.
  const dropHoliday = (h) => {
    if (!h) return;
    if (h.isManual) {
      setManualHolidays((p) => p.filter((x) => x.id !== h.id));
    } else {
      setSuppressedIds((prev) => {
        const next = new Set(prev);
        next.add(h.id);
        return next;
      });
    }
  };

  const resolveConflict = (conflictId, action) => {
    const conf = conflicts.find((c) => c.id === conflictId);
    if (!conf) return;
    if (action === "keep-existing") {
      dropHoliday(conf.imported);
      logAudit("holiday.conflict_resolved", `resolved conflict — kept ${conf.existing.name}`, conf);
    } else if (action === "keep-imported") {
      dropHoliday(conf.existing);
      logAudit("holiday.conflict_resolved", `resolved conflict — replaced with ${conf.imported.name}`, conf);
    } else if (action === "archive") {
      dropHoliday(conf.existing);
      dropHoliday(conf.imported);
      logAudit("holiday.archived", `archived conflict pair — ${conf.existing.name} / ${conf.imported.name}`, conf);
    } else {
      return;
    }
    showToast && showToast(T.settingsPageConflictResolved || "Conflict resolved");
  };

  // ── Static data for filter dropdowns ───────────────────────────────────────
  const yearOptions   = [year - 1, year, year + 1];
  const regionOptions = ["all", ...Array.from(new Set(allHolidays.map((h) => h.region).filter(Boolean)))];
  const typeOptions   = ["all", ...Array.from(new Set(allHolidays.map((h) => h.type).filter(Boolean)))];
  const statusOptions = ["all", "Active", "Conflict", "Archived"];

  // Warning banner palette
  const warnBgC   = isDark ? "#3D2A0A" : "#FEF3C7";
  const warnBdrC  = isDark ? "#92400E" : "#FDE68A";
  const warnTextC = isDark ? "#FBBF24" : "#92400E";

  return (
    <div style={{ padding: "28px 36px", maxWidth: 1300, fontFamily: "inherit" }}>
      {/* ════════════════════════ HEADER ════════════════════════ */}
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", marginBottom: 18 }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
          <div style={{ width: 38, height: 38, borderRadius: 10, background: C.brandAccentBg, flexShrink: 0 }} />
          <div>
            <h2 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0 }}>
              {T.publicHolidays || "Public Holidays"}
            </h2>
            <p style={{ fontSize: 13, color: C.textSub, marginTop: 4, lineHeight: 1.5, maxWidth: 640 }}>
              {T.settingsPagePublicHolidaysIntro || "Manage country, regional, and company-recognized holidays used by leave, attendance, scheduling, and compliance workflows."}
            </p>
          </div>
        </div>
        {isAdmin && (
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <button onClick={() => setActiveTab("audit")}  style={phBtnSecondary(C)}>{T.settingsPageViewAuditLog || "View Audit Log"}</button>
            <button onClick={() => setActiveTab("import")} style={phBtnSecondary(C)}>{T.settingsPageImport || "Import"}</button>
            <button onClick={() => setShowAdd(true)}        style={phBtnPrimary(C)}>{T.settingsPageAddHoliday || "Add Holiday"}</button>
          </div>
        )}
      </div>

      {/* ════════════════════════ WARNING BANNER ════════════════════════ */}
      {conflicts.length > 0 && (
        <div style={{
          background: warnBgC, border: `1px solid ${warnBdrC}`, color: warnTextC,
          borderRadius: 10, padding: "14px 18px", marginBottom: 18,
          display: "flex", justifyContent: "space-between", alignItems: "center", gap: 14,
        }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 2 }}>
              Needs review — {conflicts.length} import conflict{conflicts.length === 1 ? "" : "s"} require resolution before activating the {year}{country ? " " + country : ""} calendar.
            </div>
            <div style={{ fontSize: 12, opacity: 0.85 }}>
              {T.settingsPageConflictsAffectRecords || "Conflicts affect existing approved attendance records. Review, keep, replace, or archive before activation."}
            </div>
          </div>
          <button onClick={() => setActiveTab("import")}
            style={{ ...phBtnSecondary(C), color: warnTextC, borderColor: warnTextC + "55", background: "transparent", whiteSpace: "nowrap" }}>
            {T.settingsPageResolveConflicts || "Resolve conflicts"}
          </button>
        </div>
      )}

      {/* ════════════════════════ KPI CARDS ════════════════════════ */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 12, marginBottom: 18 }}>
        <KpiCard C={C} label={T.settingsPageCountriesCovered || "Countries covered"} value={countriesCovered.length} sub={countriesCovered.slice(0, 4).join(" · ") || "—"} />
        <KpiCard C={C} label={T.settingsPageActiveHolidays || "Active holidays"}   value={activeHolidayCount}
          sub={`across ${countriesCovered.length} countr${countriesCovered.length === 1 ? "y" : "ies"} · ${year}`} />
        <KpiCard C={C} label={T.settingsPageNextHoliday || "Next holiday"}
          value={nextHoliday ? new Date(nextHoliday.date).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—"}
          sub={nextHoliday ? `${nextHoliday.name} · ${nextHoliday.country}` : (T.settingsPageNoUpcoming || "No upcoming")} />
        <KpiCard C={C} label={T.settingsPageConflicts || "Conflicts"} value={conflicts.length}
          sub={conflicts.length ? `${country || ""} ${year} import · unresolved` : (T.settingsPageAllClear || "All clear")}
          accent={conflicts.length ? warnTextC : C.text} />
        <KpiCard C={C} label={T.settingsPageLastImport || "Last import"}
          value={lastImport ? fmtTs(lastImport.ts).split(",")[0] : "—"}
          sub={lastImport ? (lastImport.message.length > 36 ? lastImport.message.slice(0, 36) + "…" : lastImport.message) : (T.settingsPageNothingImportedYet || "Nothing imported yet")} />
      </div>

      {/* ════════════════════════ FILTERS ════════════════════════ */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 180px 160px 110px 140px 140px", gap: 10, marginBottom: 12 }}>
        <input type="text" placeholder={T.settingsPageSearchHolidays || "Search holidays…"} value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} style={phInput(C)} />
        <div ref={countryDropdownRef} style={{ position: "relative" }}>
          <button type="button" onClick={() => { if (!loadingCountries && isAdmin) setCountryOpen((o) => !o); }}
            disabled={loadingCountries || !isAdmin}
            style={{ ...phInput(C), textAlign: "left", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, cursor: isAdmin && !loadingCountries ? "pointer" : "not-allowed" }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {country ? (countries.find((x) => x.countryCode === country)?.name || country) : (loadingCountries ? (T.settingsPageLoadingEllipsis || "Loading…") : (T.settingsPageAllCountries || "All countries"))}
            </span>
            <span style={{ fontSize: 10, opacity: 0.6, transform: countryOpen ? "rotate(180deg)" : "none", transition: "transform .15s" }}>▼</span>
          </button>
          {countryOpen && (
            <div style={{ position: "absolute", top: "calc(100% + 4px)", left: 0, right: 0, background: C.surface, border: `1.5px solid ${C.border}`, borderRadius: 8, boxShadow: "0 8px 24px rgba(0,0,0,0.12)", zIndex: 50, maxHeight: 340, display: "flex", flexDirection: "column" }}>
              <div style={{ padding: 8, borderBottom: `1px solid ${C.border}` }}>
                <input ref={countrySearchRef} type="text" value={countryQuery} onChange={(e) => setCountryQuery(e.target.value)} placeholder={T.settingsPageSearchCountry || "Search country…"} style={{ ...phInput(C), padding: "7px 10px", fontSize: 12 }} />
              </div>
              <div style={{ overflowY: "auto", flex: 1 }}>
                {(() => {
                  const q = countryQuery.trim().toLowerCase();
                  const filtered = q ? countries.filter((c) => (c.name || "").toLowerCase().includes(q) || (c.countryCode || "").toLowerCase().includes(q)) : countries;
                  if (filtered.length === 0) return <div style={{ padding: "16px", fontSize: 12, color: C.textSub, textAlign: "center" }}>{T.settingsPageNoCountriesMatch || "No countries match"}</div>;
                  return filtered.map((c) => (
                    <div key={c.countryCode} onClick={() => { handleCountryChange(c.countryCode); setCountryOpen(false); }}
                      style={{ padding: "8px 12px", fontSize: 13, color: C.text, cursor: "pointer", display: "flex", justifyContent: "space-between", background: c.countryCode === country ? `${C.brand}1A` : "transparent" }}
                      onMouseEnter={(e) => { if (c.countryCode !== country) e.currentTarget.style.background = C.bg; }}
                      onMouseLeave={(e) => { if (c.countryCode !== country) e.currentTarget.style.background = "transparent"; }}>
                      <span>{c.name}</span><span style={{ color: C.textLight, fontSize: 11 }}>{c.countryCode}</span>
                    </div>
                  ));
                })()}
              </div>
            </div>
          )}
        </div>
        <select value={regionFilter} onChange={(e) => setRegionFilter(e.target.value)} style={phInput(C)}>
          {regionOptions.map((r) => <option key={r} value={r}>{r === "all" ? (T.settingsPageAllRegions || "All regions") : r}</option>)}
        </select>
        <select value={year} onChange={(e) => setYear(parseInt(e.target.value, 10))} style={phInput(C)}>
          {yearOptions.map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} style={phInput(C)}>
          {typeOptions.map((t) => <option key={t} value={t}>{t === "all" ? (T.settingsPageAllTypes || "All types") : t}</option>)}
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={phInput(C)}>
          {statusOptions.map((s) => <option key={s} value={s}>{s === "all" ? (T.settingsPageAllStatuses || "All statuses") : s}</option>)}
        </select>
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
        <div style={{ display: "inline-flex", background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, padding: 3 }}>
          {["table", "calendar"].map((m) => (
            <button key={m} onClick={() => setViewMode(m)}
              style={{ padding: "6px 14px", borderRadius: 6, fontSize: 12, fontWeight: 700, border: "none", cursor: "pointer", fontFamily: "inherit", background: viewMode === m ? C.brandAccentBg : "transparent", color: viewMode === m ? C.brand : C.textSub }}>
              {m === "table" ? (T.settingsPageTable || "Table") : (T.settingsPageCalendar || "📅 Calendar")}
            </button>
          ))}
        </div>
      </div>

      {/* ════════════════════════ TABS ════════════════════════ */}
      <div style={{ display: "flex", gap: 22, borderBottom: `1px solid ${C.border}`, marginBottom: 18 }}>
        {[
          { k: "list",          label: (T.settingsPageHolidayListTab || "Holiday list") },
          { k: "import",        label: (T.settingsPageImportConflictsTab || "Import & conflicts") },
          { k: "applicability", label: (T.settingsPageApplicabilityRulesTab || "Applicability rules") },
          { k: "audit",         label: (T.settingsPageAuditTrailTab || "Audit trail") },
        ].map((t) => (
          <button key={t.k} onClick={() => setActiveTab(t.k)}
            style={{
              padding: "10px 2px", border: "none", background: "transparent", fontFamily: "inherit",
              fontSize: 13, fontWeight: 700, cursor: "pointer",
              color: activeTab === t.k ? C.text : C.textSub,
              borderBottom: activeTab === t.k ? `2.5px solid ${C.brand}` : "2.5px solid transparent",
              marginBottom: -1,
            }}>
            {t.label}
          </button>
        ))}
      </div>

      {/* ════════════════════════ TAB CONTENT ════════════════════════ */}
      {error && (
        <div style={{ background: "#FEF2F2", border: "1px solid #FECACA", color: "#991B1B", borderRadius: 10, padding: "12px 16px", fontSize: 13, marginBottom: 16 }}>
          {error}
        </div>
      )}

      {/* This page is a REFERENCE view: the country calendar it shows is
          read-only, and the manual holidays, applicability rules and audit
          trail below live in this browser only (localStorage) — they are not
          workspace settings and no other admin can see them.
          Saying so is the honest fix for now: the alternative was leaving a
          governance UI that looked server-backed and was not. The calendar that
          actually drives attendance, leave and payroll is the organization's
          own, edited in Manage Holidays. */}
      {["import", "applicability", "audit"].includes(activeTab) && (
        <div style={{
          background: isDark ? "#1F2937" : "#F8FAFC",
          border: `1px solid ${C.border}`, borderRadius: 10,
          padding: "12px 16px", fontSize: 12.5, color: C.textSub, marginBottom: 16,
          display: "flex", gap: 10, alignItems: "flex-start",
        }}>
          <span aria-hidden="true" style={{ fontWeight: 700, color: C.text }}>i</span>
          <span>
            {T.settingsPageLocalOnlyNotice
              || "These notes are saved in this browser only — they are not shared with your team and do not change anyone's calendar."}{" "}
            <button
              type="button"
              onClick={() => navigate("/admin?tab=manageholiday")}
              style={{ background: "none", border: "none", padding: 0, color: C.brand, fontWeight: 700, cursor: "pointer", fontFamily: "inherit", fontSize: 12.5, textDecoration: "underline" }}
            >
              {T.settingsPageOpenManageHolidays || "Manage Holidays"}
            </button>{" "}
            {T.settingsPageIsTheRealCalendar
              || "is where holidays are added for everyone."}
          </span>
        </div>
      )}

      {activeTab === "list" && (
        <HolidayListView
          C={C} isDark={isDark}
          rows={filteredHolidays}
          loadingHolidays={loadingHolidays}
          country={country}
          fmtDate={fmtDate} fmtDay={fmtDay}
          isAdmin={isAdmin}
          onArchive={archiveManual}
          onResolve={() => setActiveTab("import")}
          viewMode={viewMode}
          year={year}
        />
      )}

      {activeTab === "import" && (
        <ImportConflictsView
          C={C}
          countries={countries} loadingCountries={loadingCountries}
          country={country} year={year}
          onPreview={saveCountryProfile}
          conflicts={conflicts}
          onResolveConflict={resolveConflict}
          fmtDate={fmtDate}
          warnBg={warnBgC} warnBdr={warnBdrC} warnText={warnTextC}
          isAdmin={isAdmin}
        />
      )}

      {activeTab === "applicability" && (
        <ApplicabilityRulesView
          C={C} isDark={isDark}
          profiles={profiles} country={country}
          applicability={applicability} setApplicability={setApplicability}
          isAdmin={isAdmin}
          onLog={logAudit}
        />
      )}

      {activeTab === "audit" && (
        <AuditTrailView C={C} events={auditEvents} fmtTs={fmtTs} />
      )}

      {/* ════════════════════════ ADD HOLIDAY MODAL ════════════════════════ */}
      {showAdd && (
        <PhModal C={C} title={T.settingsPageAddHolidayModalTitle || "Add holiday"} onClose={() => setShowAdd(false)}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <PhField C={C} label={T.settingsPageHolidayName || "Holiday name"}>
              <input type="text" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} style={phInput(C)} placeholder={T.settingsPageFoundersDayPlaceholder || "e.g. Company Founders Day"} />
            </PhField>
            <PhField C={C} label={T.settingsPageDateLabel || "Date"}>
              <input type="date" value={draft.date} onChange={(e) => setDraft({ ...draft, date: e.target.value })} style={phInput(C)} />
            </PhField>
            <PhField C={C} label={T.settingsPageCountryLabel || "Country"}>
              <select value={draft.country || country} onChange={(e) => setDraft({ ...draft, country: e.target.value })} style={phInput(C)}>
                {countries.map((c) => <option key={c.countryCode} value={c.countryCode}>{c.name} ({c.countryCode})</option>)}
              </select>
            </PhField>
            <PhField C={C} label={T.settingsPageRegionLabel || "Region"}>
              <input type="text" value={draft.region} onChange={(e) => setDraft({ ...draft, region: e.target.value })} style={phInput(C)} placeholder={T.settingsPageRegionPlaceholder || "Federal / All regions / State"} />
            </PhField>
            <PhField C={C} label={T.settingsPageTypeLabel || "Type"}>
              <select value={draft.type} onChange={(e) => setDraft({ ...draft, type: e.target.value })} style={phInput(C)}>
                {["Company", "Public", "Optional", "Observance", "Bank"].map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </PhField>
            <PhField C={C} label={T.settingsPagePayImpactLabel || "Pay impact"}>
              <select value={draft.payImpact} onChange={(e) => setDraft({ ...draft, payImpact: e.target.value })} style={phInput(C)}>
                {["Paid", "Unpaid", "Time-and-half", "Double-time"].map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </PhField>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 18 }}>
            <button onClick={() => setShowAdd(false)} style={phBtnSecondary(C)}>{T.cancel || "Cancel"}</button>
            <button onClick={saveManual}              style={phBtnPrimary(C)}>{T.settingsPageSaveHoliday || "Save holiday"}</button>
          </div>
        </PhModal>
      )}
    </div>
  );
}

// ── Public-Holidays helper components / styles ────────────────────────────────
function KpiCard({ C, label, value, sub, accent }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 10, fontWeight: 800, color: C.textLight, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 800, color: accent || C.text, lineHeight: 1.1, marginBottom: 4 }}>{value}</div>
      <div style={{ fontSize: 11, color: C.textSub, lineHeight: 1.3, minHeight: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sub}</div>
    </div>
  );
}

function HolidayListView({ C, isDark, rows, loadingHolidays, country, fmtDate, fmtDay, isAdmin, onArchive, onResolve, viewMode, year }) {
  const { T } = useLanguage();
  if (!country) {
    return (
      <div style={{ background: C.surface, border: `1px dashed ${C.border}`, borderRadius: 12, padding: "40px 20px", textAlign: "center", color: C.textSub }}>
        <div style={{ fontSize: 36, marginBottom: 8 }}>🌍</div>
        <p style={{ fontSize: 14, fontWeight: 600, color: C.text, marginBottom: 4 }}>{T.settingsPagePickCountry || "Pick a country to see its public holidays"}</p>
        <p style={{ fontSize: 12, color: C.textSub }}>{T.settingsPageCountryExamples || "India (IN), United States (US), United Kingdom (GB), and 100+ more."}</p>
      </div>
    );
  }
  if (loadingHolidays) return <div style={{ textAlign: "center", padding: 32, color: C.textSub }}>{T.settingsPageLoadingHolidays || "Loading holidays…"}</div>;

  if (viewMode === "calendar") {
    return (
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12, padding: "20px 24px" }}>
        <CalendarGrid C={C} rows={rows} year={year} />
      </div>
    );
  }

  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12, overflow: "hidden" }}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(180px, 1.8fr) 100px 100px 80px 110px 100px 100px 100px 110px 100px", padding: "12px 18px", borderBottom: `1px solid ${C.border}`, background: C.bg, fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase" }}>
        <div>{T.settingsPageHolidayName || "Holiday name"}</div><div>{T.settingsPageDateLabel || "Date"}</div><div>{T.settingsPageDayCol || "Day"}</div><div>{T.settingsPageCountryLabel || "Country"}</div><div>{T.settingsPageRegionLabel || "Region"}</div>
        <div>{T.settingsPageTypeLabel || "Type"}</div><div>{T.settingsPageSourceCol || "Source"}</div><div>{T.settingsPagePayImpactLabel || "Pay impact"}</div><div>{T.settingsPageStatusCol || "Status"}</div><div style={{ textAlign: "right" }}>{T.actions || "Actions"}</div>
      </div>
      {rows.length === 0 && (
        <div style={{ padding: "32px 18px", textAlign: "center", color: C.textSub, fontSize: 13 }}>{T.settingsPageNoHolidaysMatch || "No holidays match your filters."}</div>
      )}
      {rows.map((h) => (
        <div key={h.id} style={{ display: "grid", gridTemplateColumns: "minmax(180px, 1.8fr) 100px 100px 80px 110px 100px 100px 100px 110px 100px", padding: "14px 18px", borderBottom: `1px solid ${C.borderLight}`, fontSize: 13, color: C.text, alignItems: "center" }}>
          <div>
            <div style={{ fontWeight: 700, color: C.text }}>{h.name}</div>
            {h.status === "Conflict" && (
              <div style={{ marginTop: 4, display: "inline-block", fontSize: 9, fontWeight: 800, color: "#92400E", background: "#FEF3C7", padding: "2px 6px", borderRadius: 4, letterSpacing: 0.4, textTransform: "uppercase" }}>{T.settingsPageConflictBadge || "Conflict"}</div>
            )}
          </div>
          <div style={{ color: C.textSub, fontSize: 12 }}>{fmtDate(h.date)}</div>
          <div style={{ color: C.textSub, fontSize: 12 }}>{fmtDay(h.date)}</div>
          <div><PhBadge bg={isDark ? "#0E2B3D" : "#F0F9FF"} fg={isDark ? "#7DD3FC" : "#075985"}>{HOLIDAY_FLAG_MAP[h.country] || ""} {h.country}</PhBadge></div>
          <div style={{ color: C.textSub, fontSize: 12 }}>{h.region}</div>
          <div><PhTypeBadge type={h.type} /></div>
          <div><PhBadge bg={isDark ? "#1F2937" : "#F3F4F6"} fg={isDark ? "#D1D5DB" : "#4B5563"}>{h.source}</PhBadge></div>
          <div><PhBadge bg={isDark ? "#0D2A1A" : "#D1FAE5"} fg={isDark ? "#5FCC93" : "#065F46"}>{h.payImpact}</PhBadge></div>
          <div>
            {h.status === "Conflict"
              ? <PhBadge bg={isDark ? "#3D1F1A" : "#FEE2E2"} fg={isDark ? "#F87171" : "#991B1B"}>{T.settingsPageConflictBadge || "Conflict"}</PhBadge>
              : h.status === "Archived"
                ? <PhBadge bg={isDark ? "#1F2937" : "#F3F4F6"} fg={isDark ? "#D1D5DB" : "#4B5563"}>{T.settingsPageArchivedBadge || "Archived"}</PhBadge>
                : <PhBadge bg={isDark ? "#0D2A1A" : "#D1FAE5"} fg={isDark ? "#5FCC93" : "#065F46"}>{T.active || "Active"}</PhBadge>}
          </div>
          <div style={{ textAlign: "right", fontSize: 12 }}>
            {h.status === "Conflict" ? (
              <button onClick={onResolve} style={phLink("#B91C1C")}>{T.settingsPageResolve || "Resolve"}</button>
            ) : h.isManual && isAdmin ? (
              <>
                <button style={phLink(C.brand)}>{T.edit || "Edit"}</button>
                <span style={{ color: C.textLight, padding: "0 4px" }}>·</span>
                <button onClick={() => onArchive(h.id)} style={phLink("#B91C1C")}>{T.settingsPageArchive || "Archive"}</button>
              </>
            ) : (
              <span style={{ color: C.textLight, fontSize: 11 }}>—</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function CalendarGrid({ C, rows, year }) {
  const months = Array.from({ length: 12 }, (_, m) => m);
  const byMonth = months.map((m) => rows.filter((r) => {
    const d = new Date(r.date);
    return d.getMonth() === m && d.getFullYear() === year;
  }));
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
      {months.map((m) => {
        const monthName = new Date(year, m, 1).toLocaleString(undefined, { month: "long" });
        return (
          <div key={m} style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 8, padding: "12px 14px" }}>
            <div style={{ fontSize: 12, fontWeight: 800, color: C.text, marginBottom: 8 }}>{monthName} {year}</div>
            {byMonth[m].length === 0 ? (
              <div style={{ fontSize: 11, color: C.textLight }}>No holidays</div>
            ) : byMonth[m].map((h) => (
              <div key={h.id} style={{ fontSize: 12, color: C.textSub, padding: "4px 0", borderBottom: `1px solid ${C.borderLight}` }}>
                <span style={{ fontWeight: 700, color: C.text }}>{new Date(h.date).getDate()}</span> · {h.name}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function ImportConflictsView({ C, countries, loadingCountries, country, year, onPreview, conflicts, onResolveConflict, fmtDate, warnBg, warnBdr, warnText, isAdmin }) {
  const { T } = useLanguage();
  const fileRef = useRef(null);
  return (
    <div>
      <div style={{ background: warnBg, border: `1px solid ${warnBdr}`, color: warnText, borderRadius: 10, padding: "12px 16px", marginBottom: 18, fontSize: 12 }}>
        <span style={{ fontWeight: 700 }}>{T.settingsPageNoSilentOverwrite || "No silent overwrite:"}</span> {T.settingsPageNoSilentOverwriteBody || "Imported holidays must go through conflict review before activation. Duplicate detection is automatic for same country + region + date combinations."}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 22 }}>
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12, padding: "18px 20px" }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 12 }}>{T.settingsPageImportCountryCalendar || "Import country calendar"}</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
            <PhField C={C} label="Country">
              <div style={{ ...phInput(C), padding: "10px 12px", color: country ? C.text : C.textLight }}>
                {country ? (countries.find((c) => c.countryCode === country)?.name || country) : (loadingCountries ? (T.settingsPageLoadingEllipsis || "Loading…") : (T.settingsPageSelectCountryAbove || "Select a country above"))}
              </div>
            </PhField>
            <PhField C={C} label="Year"><div style={{ ...phInput(C), padding: "10px 12px" }}>{year}</div></PhField>
            <PhField C={C} label={T.settingsPageRegionStateLabel || "Region / State"}><div style={{ ...phInput(C), padding: "10px 12px", color: C.textSub }}>{T.settingsPageAllFederal || "All (Federal)"}</div></PhField>
            <PhField C={C} label={T.settingsPageSourceCol || "Source"}><div style={{ ...phInput(C), padding: "10px 12px", color: C.textSub }}>{T.settingsPageApprovedDataset || "Approved public dataset"}</div></PhField>
          </div>
          <button onClick={onPreview} disabled={!country || !isAdmin}
            style={{ ...phBtnPrimary(C), opacity: country && isAdmin ? 1 : 0.5, cursor: country && isAdmin ? "pointer" : "not-allowed" }}>
            {T.settingsPagePreviewBeforeImporting || "Preview before importing"}
          </button>
        </div>

        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12, padding: "18px 20px" }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 12 }}>{T.settingsPageUploadCsv || "Upload CSV"}</div>
          <div style={{ border: `1.5px dashed ${C.border}`, borderRadius: 10, padding: "30px 18px", textAlign: "center", background: C.bg }}>
            <div style={{ fontSize: 12, color: C.textSub, marginBottom: 4 }}>{T.settingsPageDragDropCsv || "Drag and drop CSV file here"}</div>
            <div style={{ fontSize: 11, color: C.textLight, marginBottom: 12 }}>{T.settingsPageOrClickToBrowse || "or click to browse · Max 5MB"}</div>
            <input ref={fileRef} type="file" accept=".csv" style={{ display: "none" }} />
            <button onClick={() => fileRef.current && fileRef.current.click()} style={phBtnSecondary(C)}>{T.settingsPageBrowseFile || "Browse file"}</button>
          </div>
          <div style={{ fontSize: 11, color: C.textSub, marginTop: 10 }}>
            {T.settingsPageRequiredColumns || "Required columns:"}{" "}
            {["name", "date", "country", "type"].map((c, i) => (
              <span key={c}>
                <span style={{ fontFamily: "monospace", background: C.bg, padding: "1px 6px", borderRadius: 4 }}>{c}</span>
                {i < 3 ? ", " : ""}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div style={{ fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 8 }}>
        {T.settingsPageConflictsRequiringResolution || "Conflicts requiring resolution"}{country ? ` — ${country} ${year}` : ""}
      </div>

      {conflicts.length === 0 ? (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: "26px 20px", textAlign: "center", color: C.textSub, fontSize: 13 }}>
          {T.settingsPageNoConflictsClean || "✅ No conflicts. Holidays are clean to activate."}
        </div>
      ) : (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, overflow: "hidden" }}>
          <div style={{ background: warnBg, color: warnText, padding: "10px 18px", fontSize: 12, fontWeight: 700, borderBottom: `1px solid ${warnBdr}` }}>
            {conflicts.length} {conflicts.length === 1 ? (T.settingsPageConflictWord || "conflict") : (T.settingsPageConflictsWord || "conflicts")} {T.settingsPageConflictsDetectedSuffix || "detected · Existing records linked to approved attendance — archive or supersede required · Cannot hard-delete"}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1.4fr 1.4fr 1fr 1.2fr", padding: "10px 18px", background: C.bg, borderBottom: `1px solid ${C.border}`, fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 0.5, textTransform: "uppercase" }}>
            <div>{T.settingsPageHolidayCol || "Holiday"}</div><div>{T.settingsPageDateLabel || "Date"}</div><div>{T.settingsPageExistingRecord || "Existing record"}</div><div>{T.settingsPageImportedRecord || "Imported record"}</div><div>{T.settingsPageLinkedRecords || "Linked records"}</div><div>{T.settingsPageActionCol || "Action"}</div>
          </div>
          {conflicts.map((c) => (
            <div key={c.id} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1.4fr 1.4fr 1fr 1.2fr", padding: "12px 18px", borderBottom: `1px solid ${C.borderLight}`, fontSize: 13, alignItems: "center" }}>
              <div style={{ fontWeight: 700, color: C.text }}>{c.existing.name}</div>
              <div style={{ color: C.textSub, fontSize: 12 }}>{fmtDate(c.date)}</div>
              <div style={{ color: C.textSub, fontSize: 12 }}>{c.existing.name} · {c.existing.source}</div>
              <div style={{ color: C.textSub, fontSize: 12 }}>{c.imported.name} · {c.imported.source}</div>
              <div style={{ color: C.textSub, fontSize: 12 }}>—</div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button onClick={() => onResolveConflict(c.id, "keep-existing")} style={phLink(C.brand)}>{T.settingsPageKeepExisting || "Keep existing"}</button>
                <button onClick={() => onResolveConflict(c.id, "keep-imported")} style={phLink(C.brand)}>{T.settingsPageReplace || "Replace"}</button>
                <button onClick={() => onResolveConflict(c.id, "archive")}        style={phLink("#B91C1C")}>{T.settingsPageArchive || "Archive"}</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ApplicabilityRulesView({ C, isDark, profiles, country, applicability, setApplicability, isAdmin, onLog }) {
  const { T } = useLanguage();
  const okBg   = isDark ? "#0D2A1A" : "#ECFDF5";
  const okBdr  = isDark ? "#1F5A3D" : "#A7F3D0";
  const okText = isDark ? "#5FCC93" : "#065F46";

  const rows = useMemo(() => {
    const set = new Map();
    (profiles || []).forEach((p) => set.set(p.country_code, { code: p.country_code, name: p.country_name || p.country_code }));
    if (country && !set.has(country)) set.set(country, { code: country, name: country });
    return [...set.values()];
  }, [profiles, country]);

  const toggle = (key) => {
    if (!isAdmin) return;
    setApplicability((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      onLog && onLog("holiday.applicability_updated", `updated applicability — ${key} = ${next[key] ? "on" : "off"}`, next);
      return next;
    });
  };

  return (
    <div>
      <div style={{ background: okBg, border: `1px solid ${okBdr}`, color: okText, borderRadius: 10, padding: "12px 16px", marginBottom: 18, fontSize: 12 }}>
        {T.settingsPageApplicabilityIntro || "Applicability rules determine which workers a holiday applies to. Multi-country workspaces must scope each holiday correctly — a US Federal holiday must not auto-apply to workers in India or UK."}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr", gap: 22 }}>
        <div>
          <div style={{ fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 10 }}>
            {T.settingsPageDefaultApplicabilityByCountry || "Default applicability by country"}
          </div>
          <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, overflow: "hidden" }}>
            <div style={{ display: "grid", gridTemplateColumns: "140px 1fr 80px 100px", padding: "10px 16px", background: C.bg, borderBottom: `1px solid ${C.border}`, fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 0.5, textTransform: "uppercase" }}>
              <div>{T.settingsPageCountryLabel || "Country"}</div><div>{T.settingsPageScopeCol || "Scope"}</div><div>{T.workers || "Workers"}</div><div>{T.settingsPageOverrideCol || "Override"}</div>
            </div>
            {rows.length === 0 && (
              <div style={{ padding: "26px 16px", textAlign: "center", color: C.textSub, fontSize: 13 }}>
                {T.settingsPageAddCountryProfileHint || "Add a country profile to configure applicability."}
              </div>
            )}
            {rows.map((r) => (
              <div key={r.code} style={{ display: "grid", gridTemplateColumns: "140px 1fr 80px 100px", padding: "14px 16px", borderBottom: `1px solid ${C.borderLight}`, fontSize: 13, alignItems: "center" }}>
                <div><PhBadge bg={isDark ? "#0E2B3D" : "#F0F9FF"} fg={isDark ? "#7DD3FC" : "#075985"}>{HOLIDAY_FLAG_MAP[r.code] || ""} {r.name}</PhBadge></div>
                <div style={{ color: C.textSub, fontSize: 12 }}>All {r.code}-based workers</div>
                <div style={{ color: C.text, fontWeight: 700 }}>—</div>
                <div><button style={phLink(C.brand)}>Configure</button></div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 10, fontWeight: 800, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 10 }}>
            {T.settingsPageDownstreamImpactSettings || "Downstream impact settings"}
          </div>
          <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: "8px 0" }}>
            <PhImpactToggle C={C} label={T.settingsPageApplyToLeave || "Apply to leave entitlement calculations"} sub={T.settingsPageApplyToLeaveSub || "Holidays reduce or adjust leave balance per policy"} on={applicability.leave}      onToggle={() => toggle("leave")} />
            <PhImpactToggle C={C} label={T.settingsPageApplyToAttendance || "Apply to attendance interpretation"}      sub={T.settingsPageApplyToAttendanceSub || "Absence on holiday date is not flagged"}               on={applicability.attendance} onToggle={() => toggle("attendance")} />
            <PhImpactToggle C={C} label={T.settingsPageApplyToOvertime || "Apply to overtime rules"}                  sub={T.settingsPageApplyToOvertimeSub || "Work on public holiday triggers overtime multiplier"}  on={applicability.overtime}   onToggle={() => toggle("overtime")} />
            <PhImpactToggle C={C} label={T.settingsPageShowInWorkerCalendar || "Show in worker calendar"}                  sub={T.settingsPageShowInWorkerCalendarSub || "Worker sees applicable holidays in their view"}       on={applicability.calendar}   onToggle={() => toggle("calendar")} />
            <PhImpactToggle C={C} label={T.settingsPageIncludeInPayroll || "Include in payroll exports"}               sub={T.settingsPageIncludeInPayrollSub || "Holiday pay flags appear in payroll data"}            on={applicability.payroll}    onToggle={() => toggle("payroll")} />
          </div>
        </div>
      </div>
    </div>
  );
}

function PhImpactToggle({ C, label, sub, on, onToggle }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 16px", gap: 12 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{label}</div>
        <div style={{ fontSize: 11, color: C.textSub, marginTop: 2 }}>{sub}</div>
      </div>
      <button onClick={onToggle} style={{ width: 40, height: 22, borderRadius: 11, border: "none", cursor: "pointer", background: on ? C.brand : C.textLight, position: "relative", flexShrink: 0 }}>
        <span style={{ position: "absolute", top: 2, left: on ? 20 : 2, width: 18, height: 18, background: "#fff", borderRadius: "50%", transition: "left .2s", boxShadow: "0 1px 3px rgba(0,0,0,0.25)" }} />
      </button>
    </div>
  );
}

function AuditTrailView({ C, events, fmtTs }) {
  const { T } = useLanguage();
  const [q, setQ]         = useState("");
  const [typeF, setTypeF] = useState("all");
  const windowDays        = 30;
  // Capture the cutoff once per mount via a lazy state initializer —
  // pure `useMemo` cannot call Date.now() directly without a purity warning.
  const [cutoff] = useState(() => Date.now() - windowDays * 86400000);

  const filtered = useMemo(() => {
    return events.filter((e) => {
      if (q && !((e.message || "") + " " + (e.actor || "")).toLowerCase().includes(q.toLowerCase())) return false;
      if (typeF !== "all" && e.type !== typeF) return false;
      if (new Date(e.ts).getTime() < cutoff) return false;
      return true;
    });
  }, [events, q, typeF, cutoff]);

  const typeColor = (t) => {
    if (t.includes("imported")) return "#F59E0B";
    if (t.includes("created"))  return "#10B981";
    if (t.includes("updated"))  return "#3B82F6";
    if (t.includes("resolved")) return "#10B981";
    if (t.includes("archived")) return "#EF4444";
    if (t.includes("exported")) return "#8B5CF6";
    return C.textLight;
  };

  const typeOptions = ["all", ...Array.from(new Set(events.map((e) => e.type)))];

  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 180px 180px 120px", gap: 10, marginBottom: 14 }}>
        <input type="text" value={q} onChange={(e) => setQ(e.target.value)} placeholder={T.settingsPageSearchAuditEvents || "Search audit events…"} style={phInput(C)} />
        <select value={typeF} onChange={(e) => setTypeF(e.target.value)} style={phInput(C)}>
          {typeOptions.map((t) => <option key={t} value={t}>{t === "all" ? (T.settingsPageAllEventTypes || "All event types") : t}</option>)}
        </select>
        <div style={{ ...phInput(C), padding: "10px 12px", color: C.textSub, fontSize: 13 }}>Last {windowDays} days</div>
        <button style={phBtnSecondary(C)}>Export</button>
      </div>

      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, padding: "8px 0" }}>
        {filtered.length === 0 && (
          <div style={{ padding: "26px 18px", textAlign: "center", color: C.textSub, fontSize: 13 }}>
            {T.settingsPageNoAuditEvents || "No audit events yet. Importing a country calendar, adding a manual holiday, or resolving a conflict will appear here."}
          </div>
        )}
        {filtered.map((e) => (
          <div key={e.id} style={{ display: "flex", gap: 12, padding: "14px 18px", borderBottom: `1px solid ${C.borderLight}` }}>
            <span style={{ width: 8, height: 8, borderRadius: 4, background: typeColor(e.type), marginTop: 6, flexShrink: 0 }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, color: C.text }}>
                <span style={{ fontWeight: 700 }}>{e.actor}</span> {e.message}
              </div>
              <div style={{ fontSize: 11, color: C.textSub, marginTop: 4 }}>
                {e.type} · {T.settingsPageAuditEventLabel || "Audit event:"} {e.id}
              </div>
            </div>
            <div style={{ fontSize: 11, color: C.textLight, whiteSpace: "nowrap" }}>{fmtTs(e.ts)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function PhModal({ C, title, onClose, children }) {
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.42)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999 }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12, padding: "20px 24px", width: 580, maxWidth: "94vw", maxHeight: "90vh", overflowY: "auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <div style={{ fontSize: 15, fontWeight: 800, color: C.text }}>{title}</div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", fontSize: 18, color: C.textSub, cursor: "pointer" }}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function PhField({ C, label, children }) {
  return (
    <div>
      <div style={{ fontSize: 10, fontWeight: 800, color: C.textLight, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{label}</div>
      {children}
    </div>
  );
}

function PhBadge({ bg, fg, children }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 8px", borderRadius: 6, background: bg, color: fg, fontSize: 11, fontWeight: 700, lineHeight: 1.4 }}>
      {children}
    </span>
  );
}

function PhTypeBadge({ type }) {
  const col = HOLIDAY_TYPE_COLORS[type] || HOLIDAY_TYPE_COLORS.Public;
  return <PhBadge bg={col.bg} fg={col.fg}>{type}</PhBadge>;
}

// Shared style helpers — prefixed to avoid colliding with other module exports.
const phBtnPrimary   = (C) => ({ padding: "9px 16px", background: C.brand, color: "#fff", border: "none", borderRadius: 8, fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" });
const phBtnSecondary = (C) => ({ padding: "8px 14px", background: C.surface, color: C.text, border: `1px solid ${C.border}`, borderRadius: 8, fontWeight: 600, fontSize: 12, cursor: "pointer", fontFamily: "inherit" });
const phInput        = (C) => ({ padding: "9px 12px", border: `1.5px solid ${C.border}`, borderRadius: 8, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none", width: "100%", boxSizing: "border-box" });
const phLink         = (color) => ({ background: "transparent", border: "none", color: color, fontWeight: 700, fontSize: 12, cursor: "pointer", fontFamily: "inherit", padding: 0 });

// ── MAIN SETTINGS PAGE ────────────────────────────────────────────────────────
export default function SettingsPage() {
  const { T } = useLanguage();
  const { C } = useTheme();
  const params = useParams();
  const navigate = useNavigate();
  const location = useLocation();

  // Resolve the URL slug → internal tab key using SETTINGS_SLUG_MAP.
  // Synonyms (employees, staff, workers, users, people, teams, departments)
  // and legacy slugs (manage-teams, manage-employees) all land on the
  // correct internal tab per spec §5 acceptance criterion 8.
  const slugFromUrl = (params.tab || "").toLowerCase().trim();
  const resolvedKey = SETTINGS_SLUG_MAP[slugFromUrl] || "workspace-profile";

  const [subTab, setSubTab] = useState(resolvedKey);
  const [saving, setSaving] = useState(false);
  const [toast, setToast]   = useState(null);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const isAdmin = ["admin","hr"].includes(localStorage.getItem("role"));

  // Keep the URL aligned with the active tab without polluting the back
  // button. Three behaviors converge here:
  //   1. First load with a tab in the URL  → state matches URL (handled in
  //      useState init above); if it was a synonym/legacy slug, rewrite to
  //      the canonical one.
  //   2. First load without a tab          → rewrite URL to the resolved tab.
  //   3. User clicks a sub-nav button      → setSubTab() runs this effect.
  useEffect(() => {
    const desiredSlug = SETTINGS_KEY_TO_SLUG[subTab] || subTab;
    const desiredPath = `/settings/${desiredSlug}`;
    const currentPath = location.pathname.replace(/\/+$/, "");
    if (currentPath !== desiredPath) {
      // replace = no extra history entry, no surprise back-button behavior.
      navigate(desiredPath + location.search + location.hash, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab]);

  // Also react to URL changes (e.g. browser back/forward, deep-link paste)
  // by reflecting the new slug into subTab. The mapping handles synonyms.
  useEffect(() => {
    const newKey = SETTINGS_SLUG_MAP[(params.tab || "").toLowerCase().trim()] || "workspace-profile";
    if (newKey !== subTab) setSubTab(newKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.tab]);

  const showToast = (msg, type = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3500);
  };

  // ── TRACK SETTINGS STATE ───────────────────────────────────────────────────
  // Only the settings that the desktop tracker / risk engine actually consume
  // are kept. Each one is wired to a real code path — see settings/router.py
  // for the DEFAULTS list and where each key is read.
  const [ssEnabled, setSsEnabled]               = useState(true);
  // Placeholders only — applyServerSettings() overwrites both the moment
  // GET /settings returns. They now match the backend SYSTEM DEFAULTS
  // (settings/router.py: 1800s and 15 min); the old "30 secs" / "3 minutes"
  // were a different product's defaults, so an admin who opened the page
  // and saved without touching anything silently rewrote their org's
  // screenshot cadence from 30 minutes to 30 seconds.
  const [ssInterval, setSsInterval]             = useState("Every 30 mins");
  const [blurEnabled, setBlurEnabled]           = useState(false);
  const [idleThreshold, setIdleThreshold]       = useState("15 minutes");
  const [workHours, setWorkHours]               = useState("8:45");
  const [riskAlertEnabled, setRiskAlertEnabled] = useState(true);

  // Helpers used by both load and live-update flows
  const intervalToSecs = (v) => {
    if (!v) return 1800;
    const s   = String(v).toLowerCase();
    const num = parseInt(s.replace(/\D/g, "")) || 0;
    if (s.includes("sec"))  return num || 1800;
    if (s.includes("hour") || s.includes("hr")) return (num || 1) * 3600;
    return (num || 1) * 60;
  };
  const secToInterval = (secs) => {
    const n = +secs;
    if (!n || n <= 0) return "Every 30 mins";
    if (n < 60)   return `Every ${n} secs`;
    if (n % 3600 === 0) {
      const h = n / 3600;
      return h === 1 ? "Every 1 hour" : `Every ${h} hours`;
    }
    const m = Math.round(n / 60);
    return m === 1 ? "Every 1 min" : `Every ${m} mins`;
  };
  const idleMinsToLabel = (mins) => {
    const n = parseInt(mins);
    if (!n || n <= 0) return "15 minutes";
    return n === 1 ? "1 minute" : `${n} minutes`;
  };
  const idleLabelToMins = (label) => {
    const n = parseInt(String(label).replace(/\D/g, "")) || 15;
    return String(n);
  };

  // ── Apply a settings map (from initial fetch or live broadcast) ────────────
  const applyServerSettings = (s) => {
    if (!s) return;
    const bool = (val, def = true) => val === undefined ? def : val !== "false";
    if (s.screenshot_enabled    !== undefined) setSsEnabled(bool(s.screenshot_enabled));
    if (s.screenshot_interval)                 setSsInterval(secToInterval(s.screenshot_interval));
    if (s.screenshot_blur       !== undefined) setBlurEnabled(bool(s.screenshot_blur, false));
    if (s.idle_threshold_minutes)              setIdleThreshold(idleMinsToLabel(s.idle_threshold_minutes));
    if (s.work_hours_threshold)                setWorkHours(s.work_hours_threshold);
    if (s.risk_alert_enabled    !== undefined) setRiskAlertEnabled(bool(s.risk_alert_enabled));
  };

  // ── Load settings from backend on mount ────────────────────────────────────
  useEffect(() => {
    apiFetch("/settings").then(d => {
      applyServerSettings(d.settings || {});
      setSettingsLoaded(true);
    }).catch(() => { setSettingsLoaded(true); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Listen for live settings_updated broadcasts so the form reflects
  // changes made from another browser/admin instantly ────────────────────────
  useEffect(() => {
    const onSettingsUpdated = (e) => applyServerSettings(e.detail?.settings);
    window.addEventListener("settings_updated", onSettingsUpdated);
    return () => window.removeEventListener("settings_updated", onSettingsUpdated);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Save the full track-settings payload to the backend ───────────────────
  const handleSave = async () => {
    if (!isAdmin || saving) return;
    setSaving(true);
    showToast(T.savingTrackSettings || "Saving track settings...");
    try {
      const ssSeconds = intervalToSecs(ssInterval);
      const res = await apiFetch("/settings/update", {
        method:"PUT",
        body: JSON.stringify({ settings: {
          screenshot_enabled:     String(ssEnabled),
          screenshot_interval:    String(ssSeconds),
          screenshot_blur:        String(blurEnabled),
          idle_threshold_minutes: idleLabelToMins(idleThreshold),
          work_hours_threshold:   workHours,
          risk_alert_enabled:     String(riskAlertEnabled),
        }}),
      });
      // Confirm what the server actually persisted by re-reading from the
      // response payload — guards against the form drifting from the DB.
      if (res && res.settings) applyServerSettings(res.settings);
      showToast(T.trackSettingsSaved || "Track settings saved — every desktop tracker will pick up the change within ~10 seconds.");
    } catch { showToast(T.failedToSaveSettings || "Failed to save settings", "error"); }
    setSaving(false);
  };

  // ── STYLES ─────────────────────────────────────────────────────────────────
  // Settings sub-nav row. Uses the same active-treatment as the AdminSidebar
  // (brand background + accent stripe) and inherits the green hover via the
  // .zt-sp-subnav class injected below.
  const lnk = (active) => ({
    display:"flex", alignItems:"center", gap:10, padding:"10px 14px", borderRadius:8, cursor:"pointer",
    background: active ? C.brandBg : "transparent",
    color:      active ? C.brand   : C.textSub,
    fontWeight: active ? 700 : 500, fontSize:13, border:"none", fontFamily:"inherit",
    borderLeft: active ? `3px solid ${C.brand}` : "3px solid transparent",
    width:"100%", textAlign:"left",
    transition:"background .15s ease, color .15s ease, transform .18s cubic-bezier(.34,1.56,.64,1)",
  });

  const labelMap = {
    "workspace-profile": T.companyWorkspaceProfile || "Company & Workspace Profile",
    "organization-settings": T.organizationSettings || "Organization Settings",
    "manage-teams":      T.departmentsTeams        || "Departments & Teams",
    "manage-employees":  T.workersUsers            || "Workers & Users",
    "roles-permissions": T.rolesPermissions        || "Roles & Permissions",
    "policies":          T.policies                || "Policies",
    "public-holidays":   T.publicHolidays          || "Public Holidays",
    "billing-subscription": T.billingSubscription  || "Billing & Subscription",
    "security":          T.security                || "Security",
    "connected-systems": T.connectedSystems        || "Connected Systems",
    "audit-compliance":  T.auditCompliance         || "Audit & Compliance",
  };
  const currentTabLabel = labelMap[subTab] || SUB_NAV.find(n => n.k === subTab)?.label || (T.settings || "Settings");

  return (
    <div style={{ display:"flex", height:"100vh", overflow:"hidden", background: C.bg, fontFamily:"'DM Sans', system-ui, sans-serif" }}>
      <style>{`
        @keyframes ztSpFadeIn { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }
        .zt-sp-anim { animation: ztSpFadeIn .35s cubic-bezier(.22,1,.36,1) both; }
        .zt-sp-subnav[data-active="false"]:hover { background: ${C.brandAccentBg} !important; color: ${C.brandAccent} !important; transform: translateX(2px); }
        .zt-sp-btn-primary { transition: transform .18s ease, box-shadow .18s ease; }
        .zt-sp-btn-primary:not(:disabled):hover { transform: translateY(-1px); box-shadow: 0 6px 14px ${C.brandAccent}55; }
      `}</style>
      <AdminSidebar activeKey="settings" />

      {/* Right column: TopNavbar + sub-nav + content */}
      <main style={{ flex:1, display:"flex", flexDirection:"column", overflow:"hidden", minWidth: 0 }}>
        <TopNavbar
          title={currentTabLabel}
          subtitle={T.workspaceSettings || "Workspace Settings"}
          onBack={() => window.history.back()}
          right={subTab === "track-settings" && isAdmin ? (
            <button onClick={handleSave} disabled={saving} className="zt-sp-btn-primary"
              style={{ padding:"9px 18px", borderRadius:C.rSm, border:"none", background:C.brand,
                color:"#fff", fontWeight:700, fontSize:13, cursor: saving ? "wait" : "pointer",
                fontFamily:"inherit", boxShadow: `0 2px 6px ${C.brand}33`,
                display: "inline-flex", alignItems: "center", gap: 6,
                opacity: saving ? 0.8 : 1 }}>
              {saving ? (T.loading || "Saving...") : (T.saveTrackSettings || "Save Track Settings")}
            </button>
          ) : null}
        />

        {/* Toast */}
        {toast && (
          <div className="zt-sp-anim" style={{ position:"fixed", top:74, right:20, zIndex:9999,
            background: toast.type === "error" ? C.errorBg  : C.successBg,
            border:    `1px solid ${toast.type === "error" ? C.errorBorder : C.successBorder}`,
            color:      toast.type === "error" ? C.error    : C.success,
            borderRadius:10, padding:"13px 20px", fontWeight:600, fontSize:14,
            boxShadow:"0 4px 20px rgba(0,0,0,0.12)"}}>
            {toast.type === "error" ? "❌" : "✅"} {toast.msg}
          </div>
        )}

        <div style={{ flex:1, display:"flex", overflow:"hidden" }}>
          {/* ══ SETTINGS SUB-NAV ══ */}
          <aside style={{ width:230, flexShrink:0, background:C.surface, borderRight:`1px solid ${C.border}`, padding:"20px 12px", overflowY:"auto" }}>
            <p style={{ fontSize:11, fontWeight:800, color:C.textLight, marginBottom:6, paddingLeft:10,
                        textTransform:"uppercase", letterSpacing:0.6 }}>
              {T.workspaceSettings || "Workspace Settings"}
            </p>
            {/* Workspace Settings microcopy (spec §4.1 recommended copy). */}
            <p style={{ fontSize:11, color:C.textSub, padding:"0 10px", margin:"0 0 14px",
                        lineHeight:1.45 }}
               title={T.workspaceSettingsMicrocopy || "Configure your company workspace, departments, workers, permissions, policies, billing, security, and compliance settings."}>
              {T.workspaceSettingsMicrocopy || "Configure your company workspace, departments, workers, permissions, policies, billing, security, and compliance settings."}
            </p>
            {SUB_NAV.map(n => {
              const isActive = subTab === n.k;
              return (
                <button key={n.k} onClick={() => setSubTab(n.k)}
                  className="zt-sp-subnav" data-active={isActive ? "true" : "false"}
                  style={lnk(isActive)}>
                  <span style={{ fontSize:15 }}>{n.icon}</span>
                  {labelMap[n.k] || n.label}
                </button>
              );
            })}
          </aside>

          {/* ══ MAIN CONTENT ══ */}
          <div style={{ flex:1, overflowY:"auto", background:C.bg }}>
            {/* ─── TAB CONTENT ─── */}
            {subTab === "workspace-profile" && <WorkspaceProfileTab isAdmin={isAdmin} showToast={showToast} setSubTab={setSubTab} />}
            {subTab === "organization-settings" && <OrganizationSettingsTab isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "manage-teams"     && <ManageTeamsTab     isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "manage-employees" && <ManageEmployeesTab isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "roles-permissions" && <RolesPermissionsTab isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "policies"          && <PoliciesTab          isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "leave-balance"     && <LeaveBalanceManagementTab isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "public-holidays"  && <PublicHolidaysTab        isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "time-adjustments" && <TimeAdjustmentsTab       isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "billing-subscription" && <BillingSubscriptionTab showToast={showToast} />}
            {subTab === "security"          && <SecurityTab          isAdmin={isAdmin} showToast={showToast} />}
            {/* integrations.api is Governed+; the projection LOCKs it for Verified. */}
            {subTab === "connected-systems" && (
              <Gate capability="integrations.api">
                <ConnectedSystemsTab isAdmin={isAdmin} showToast={showToast} />
              </Gate>
            )}
            {/* Audit / compliance / legal-hold / retention is the Sovereign+
                governance surface. Gated as a unit on reports.compliance (a
                representative Sovereign+ capability — all tabs here behave
                identically: LOCK for Verified/Governed, SHOW for Sovereign+).
                Per-tab gating (audit→reports.compliance, hold→evidence.legal_hold,
                custody→governance.chain_of_custody) is a follow-up refinement. */}
            {subTab === "audit-compliance"  && (
              <Gate capability="reports.compliance">
                <AuditComplianceTab isAdmin={isAdmin} showToast={showToast} />
              </Gate>
            )}
            {subTab === "rebrand"          && <RebrandTab         isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "risk-user"        && <RiskUserSettingsTab isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "leave-settings"   && <LeaveSettingsTab   isAdmin={isAdmin} showToast={showToast} />}
            {subTab === "notifications"    && <PlatformNotificationsTab isAdmin={isAdmin} showToast={showToast} />}

            {/* ─── TRACK SETTINGS ─── */}
            {subTab === "track-settings" && (
              <div style={{ padding:"32px 40px", maxWidth:680 }}>
                {!settingsLoaded && (
                  <div style={{ textAlign:"center", padding:32, color:C.textSub }}>{T.loadingSettings || "Loading settings..."}</div>
                )}

                {settingsLoaded && (
                  <>
                    <p style={{ fontSize:14, color:C.textSub, marginBottom:24, lineHeight:1.7 }}>
                      {T.trackSettingsIntro || "Configure how the desktop tracker captures screenshots and detects idleness. Changes save instantly and are pushed to every running tracker within ~10 seconds."}
                    </p>

                    {/* evidence.screenshots.capture — the capture CONFIGURATION.
                        The viewer (evidence.screenshots.view) is All-plan and is
                        NOT gated. Here: ALLOW → normal control; CONFIGURATION_REQUIRED
                        → the same control plus a setup note (never an upsell);
                        DENY → follow the projection (hide/lock). No Layer-6 change. */}
                    <Gate capability="evidence.screenshots.capture" render={(cap) => (
                      cap.isHidden ? null :
                      cap.isLock ? (
                        <FeatureLocked capability="evidence.screenshots.capture"
                          title={T.captureUnavailableTitle || "Screenshot capture is unavailable"}
                          message={T.captureUnavailableMsg || "Screenshot capture is not available for this workspace right now."} />
                      ) : (
                    <SectionBox title={T.screenshotSettings || "Screenshot Settings"}>
                      {cap.needsConfig && (
                        <div style={{ marginBottom: 12, padding: "10px 12px", borderRadius: 8, border: "1px solid rgba(245,158,11,.35)", background: "rgba(245,158,11,.08)", color: "#92400E", fontSize: 12.5, display: "flex", alignItems: "center", gap: 8 }}>
                          <span aria-hidden="true">⚙️</span>
                          {T.captureSetupHint || "Screenshots aren't being captured yet — enable capture below to start."}
                        </div>
                      )}
                      <Chk
                        checked={ssEnabled}
                        onChange={setSsEnabled}
                        disabled={!isAdmin}
                        label={T.enableScreenCapture || "Enable screen capture"}
                        sub={T.enableScreenCaptureSub || "Turn off to stop the desktop tracker from taking screenshots."}
                      />
                      <div style={{ marginTop:14 }}>
                        <p style={{ fontSize:13, color:C.text, marginBottom:6, fontWeight:600 }}>
                          {T.screenCaptureInterval || "Screen capture interval"}
                        </p>
                        <p style={{ fontSize:12, color:C.textSub, marginBottom:8 }}>
                          {T.screenCaptureIntervalSub || "How often the tracker takes a screenshot while the employee is working."}
                        </p>
                        <Sel
                          value={ssInterval}
                          onChange={setSsInterval}
                          options={SCREENSHOT_INTERVALS}
                          labelKeys={SCREENSHOT_INTERVAL_KEYS}
                          disabled={!isAdmin || !ssEnabled}
                          style={{ width:"100%" }}
                        />
                      </div>
                    </SectionBox>
                      ))} />

                    <SectionBox title={T.activityMonitoring || "Activity Monitoring"}>
                      <Chk
                        checked={blurEnabled}
                        onChange={setBlurEnabled}
                        disabled={!isAdmin}
                        label={T.blurScreenshots || "Blur sensitive areas in screenshots"}
                        sub={T.blurScreenshotsSub || "Apply a privacy blur to captured screenshots before upload."}
                      />
                      <div style={{ marginTop:14 }}>
                        <p style={{ fontSize:13, color:C.text, marginBottom:6, fontWeight:600 }}>
                          {T.idleThreshold || "Idle threshold (minutes)"}
                        </p>
                        <p style={{ fontSize:12, color:C.textSub, marginBottom:8 }}>
                          {T.idleThresholdSub || "Minutes of no keyboard or mouse activity before the user is marked idle."}
                        </p>
                        <Sel
                          value={idleThreshold}
                          onChange={setIdleThreshold}
                          options={IDLE_THRESHOLDS}
                          labelKeys={IDLE_THRESHOLD_KEYS}
                          disabled={!isAdmin}
                          style={{ width:"100%" }}
                        />
                      </div>
                      <div style={{ marginTop:14 }}>
                        <p style={{ fontSize:13, color:C.text, marginBottom:6, fontWeight:600 }}>
                          {T.workHoursThreshold || "Daily work hours threshold"}
                        </p>
                        <p style={{ fontSize:12, color:C.textSub, marginBottom:8 }}>
                          {T.workHoursThresholdSub || "Minimum daily hours before the Risk Engine flags a low-hours alert."}
                        </p>
                        <Sel
                          value={workHours}
                          onChange={setWorkHours}
                          options={WORK_HOUR_OPTIONS}
                          disabled={!isAdmin}
                          style={{ width:"100%" }}
                        />
                      </div>
                      <div style={{ marginTop:14 }}>
                        <Chk
                          checked={riskAlertEnabled}
                          onChange={setRiskAlertEnabled}
                          disabled={!isAdmin}
                          label={T.enableRiskAlert || "Email HR when an employee falls below the threshold"}
                          sub={T.enableRiskAlertSub || "Sends an automatic risk alert when the Risk Engine flags a user."}
                        />
                      </div>
                    </SectionBox>

                    {isAdmin && (
                      <button onClick={handleSave} disabled={saving}
                        style={{ padding:"11px 28px", borderRadius:8, border:"none", background:C.brand,
                          color:"#fff", fontWeight:700, fontSize:14, cursor:"pointer", fontFamily:"inherit", marginTop:8 }}>
                        {saving ? (T.saving || "Saving...") : (T.saveTrackSettings || "Save Track Settings")}
                      </button>
                    )}

                    <div style={{ fontSize:12, color:C.textSub, lineHeight:1.8, padding:"16px 2px 0" }}>
                      <p style={{ display:"flex", gap:8, marginBottom:8 }}>
                        <span>ℹ️</span>
                        {T.trackSettingsHelp || "Saved changes apply to admin and employee dashboards immediately. Running desktop trackers re-check this setting every 10 seconds."}
                      </p>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
