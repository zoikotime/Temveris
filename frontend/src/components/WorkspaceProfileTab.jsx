/**
 * WorkspaceProfileTab — first surface inside Workspace Settings.
 *
 * Implements the Figma "Workspace Profile" layout: identity, operating
 * context, owner, linked configurations.
 *
 * Backend coverage (everything below is real, persisted, and audited):
 *   • GET /org/settings           — name, slug, internal description, logo,
 *                                   primary country, default timezone,
 *                                   default currency, owner, owner_since,
 *                                   data residency
 *   • PUT /org/settings           — partial update; emits
 *                                   AuditLog.WorkspaceProfileUpdated row
 *                                   listing only changed fields
 *   • POST /org/logo              — logo upload
 *   • POST /org/owner/transfer    — sensitive; requires confirm: true,
 *                                   promotes target user to admin if
 *                                   needed, emits OwnerTransferred audit
 *   • GET /org/owner/events       — last 10 ownership-relevant audit
 *                                   events (transfers, logo changes,
 *                                   profile updates)
 *   • GET /org/linked-status      — per-module status computed from real
 *                                   tables (user count by role, holiday
 *                                   table count, branding state, etc.)
 *
 * Genuinely infrastructure-level (NOT user-editable):
 *   • Data residency — set by where the Supabase project is hosted;
 *     surfaced from the DATA_RESIDENCY_* env vars on the backend so
 *     ops can override per-environment.
 */
import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE } from "../config";
import { authFetch, authFetchJson } from "../api/authFetch";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useWorkspace } from "../context/WorkspaceContext";
import { isSelectableZone, isUsableZone } from "../lib/orgTime";
import TimezoneCombobox from "./TimezoneCombobox";

// Auth headers are NOT built here any more. This file used to read the token
// straight out of localStorage, which broke two ways:
//   • "Remember this device" unchecked puts the token in sessionStorage, so
//     every request on this tab went out as `Bearer null` → 401.
//   • An expired access token got no silent refresh (that lives in the axios
//     interceptor), so a 401 became a permanent error screen.
// api/authFetch.js handles both — it reads either storage tier and refreshes +
// replays on 401, exactly like the axios client.
const JSON_HEADERS = { "Content-Type": "application/json" };

// ── Tiny presentational helpers ──────────────────────────────────────────────

const SectionCard = ({ icon, title, subtitle, action, children, C }) => (
  <div style={{
    background: C.surface, border: `1px solid ${C.border}`,
    borderRadius: 14, overflow: "hidden", marginBottom: 18,
  }}>
    <div style={{
      display: "flex", alignItems: "flex-start", justifyContent: "space-between",
      padding: "18px 22px", borderBottom: `1px solid ${C.border}`, gap: 16,
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, flex: 1, minWidth: 0 }}>
        <div style={{
          width: 36, height: 36, borderRadius: 9,
          background: C.brandBg, color: C.brand,
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 16, flexShrink: 0,
        }}>{icon}</div>
        <div style={{ minWidth: 0 }}>
          <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>{title}</p>
          {subtitle && (
            <p style={{ margin: "2px 0 0", fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>
              {subtitle}
            </p>
          )}
        </div>
      </div>
      {action && <div style={{ flexShrink: 0 }}>{action}</div>}
    </div>
    <div>{children}</div>
  </div>
);

const RowDivider = ({ C }) => <div style={{ height: 1, background: C.border }} />;

const FieldRow = ({ label, children, hint, C }) => (
  <>
    <div style={{
      display: "grid", gridTemplateColumns: "200px 1fr auto",
      gap: 16, alignItems: "center", padding: "14px 22px",
    }}>
      <span style={{ fontSize: 13, fontWeight: 600, color: C.textSub }}>{label}</span>
      <div style={{ fontSize: 14, color: C.text, minWidth: 0 }}>{children}</div>
      {hint && (
        <span style={{ fontSize: 11, color: C.textLight, fontWeight: 500, whiteSpace: "nowrap" }}>
          {hint}
        </span>
      )}
    </div>
    <RowDivider C={C} />
  </>
);

const STATUS_TONE_COLOR = {
  active:          "#10B981",
  configured:      "#10B981",
  trial:           "#F59E0B",
  partial:         "#F59E0B",
  review:          "#F59E0B",
  incomplete:      "#F59E0B",
  notset:          "#CBD5E1",
  not_implemented: "#CBD5E1",
};
const STATUS_LABEL = {
  active:          "Active",
  configured:      "Configured",
  trial:           "Trial",
  partial:         "Partial",
  review:          "Review needed",
  incomplete:      "Incomplete",
  notset:          "Not set",
  not_implemented: "Not implemented",
};

const StatusDot = ({ tone = "neutral" }) => (
  <span style={{
    width: 8, height: 8, borderRadius: "50%",
    background: STATUS_TONE_COLOR[tone] || "#94A3B8",
    display: "inline-block", flexShrink: 0,
  }} />
);

const Pill = ({ label, tone = "neutral", C }) => {
  const palette = {
    neutral:   { bg: C.bg,      bdr: C.border,  fg: C.textSub },
    success:   { bg: "#ECFDF5", bdr: "#A7F3D0", fg: "#059669" },
    info:      { bg: "#EEF2FF", bdr: "#C7D2FE", fg: "#4338CA" },
    immutable: { bg: "#F3E8FF", bdr: "#E9D5FF", fg: "#6B21A8" },
    gated:     { bg: "#FFFBEB", bdr: "#FDE68A", fg: "#B45309" },
  }[tone] || { bg: C.bg, bdr: C.border, fg: C.textSub };
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "3px 9px", borderRadius: 999,
      background: palette.bg, border: `1px solid ${palette.bdr}`,
      color: palette.fg, fontSize: 11, fontWeight: 600,
      whiteSpace: "nowrap",
    }}>{label}</span>
  );
};

const initials = (name) =>
  (name || "").split(" ").filter(Boolean).map(w => w[0]).join("")
    .toUpperCase().slice(0, 2) || "U";

const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-US", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
  } catch { return iso; }
};

const COUNTRY_NAMES = {
  US: { flag: "🇺🇸", name: "United States" },
  GB: { flag: "🇬🇧", name: "United Kingdom" },
  IN: { flag: "🇮🇳", name: "India" },
  DE: { flag: "🇩🇪", name: "Germany" },
  CA: { flag: "🇨🇦", name: "Canada" },
  AU: { flag: "🇦🇺", name: "Australia" },
  FR: { flag: "🇫🇷", name: "France" },
  NL: { flag: "🇳🇱", name: "Netherlands" },
  SG: { flag: "🇸🇬", name: "Singapore" },
  AE: { flag: "🇦🇪", name: "United Arab Emirates" },
  JP: { flag: "🇯🇵", name: "Japan" },
};
const CURRENCY_NAMES = {
  USD: "United States Dollar",
  EUR: "Euro",
  GBP: "Pound Sterling",
  INR: "Indian Rupee",
  CAD: "Canadian Dollar",
  AUD: "Australian Dollar",
  SGD: "Singapore Dollar",
  AED: "UAE Dirham",
  JPY: "Japanese Yen",
};
const lookupCountry = (cc) => COUNTRY_NAMES[cc?.toUpperCase()] || { flag: "🌐", name: cc || "Not set" };

// ── Main component ───────────────────────────────────────────────────────────

export default function WorkspaceProfileTab({ isAdmin, showToast, setSubTab }) {
  const { T } = useLanguage();
  const { C } = useTheme();
  const navigate = useNavigate();

  // Workspace settings come from the global context — no local fetch needed.
  const { workspace: settings, loading, loadFailed: settingsLoadFailed, refresh: refreshWorkspace } = useWorkspace();

  const [linkedStatus, setLinkedStatus] = useState(null);
  const [ownerEvents, setOwnerEvents]   = useState([]);

  // Identity edit
  const [editingIdentity, setEditingIdentity] = useState(false);
  const [draftIdentity, setDraftIdentity] = useState({ company_name: "", slug: "", internal_description: "" });
  const [savingIdentity, setSavingIdentity] = useState(false);
  const fileRef = useRef(null);

  // Operating Context edit
  const [editingContext, setEditingContext] = useState(false);
  const [draftContext, setDraftContext] = useState({ primary_country: "", default_timezone: "", default_currency: "" });
  const [savingContext, setSavingContext] = useState(false);

  // Ownership transfer
  const [transferEmail, setTransferEmail] = useState("");
  const [transferConfirm, setTransferConfirm] = useState(false);
  const [transferring, setTransferring] = useState(false);

  // Company profile edit
  const [editingProfile, setEditingProfile] = useState(false);
  const [draftProfile, setDraftProfile] = useState({
    company_description: "", company_phone: "", company_website: "", company_address: "",
  });
  const [savingProfile, setSavingProfile] = useState(false);

  // Contact info edit
  const [editingContact, setEditingContact] = useState(false);
  const [draftContact, setDraftContact] = useState({ hr_email: "", support_email: "" });
  const [savingContact, setSavingContact] = useState(false);

  // Branding edit
  const [editingBranding, setEditingBranding] = useState(false);
  const [draftBranding, setDraftBranding] = useState({ primary_brand_color: "", secondary_brand_color: "" });
  const [savingBranding, setSavingBranding] = useState(false);

  // ── Load the two auxiliary panels ──
  //
  // This page used to block on a single `Promise.all` with no per-request
  // timeout, so one slow endpoint (cold container, DB pool still warming up)
  // kept the whole page on "Loading workspace profile…" indefinitely. Each
  // request now stands on its own:
  //   1. authFetchJson carries a timeout, a retry on a transient failure, and
  //      the 401 silent-refresh-and-replay — so a blip or an expired access
  //      token no longer becomes a dead panel.
  //   2. It resolves to null rather than throwing, so a slow or failing
  //      /org/owner/events can't hold back /org/linked-status.
  //   3. The page renders as soon as /org/settings lands (owned by
  //      WorkspaceContext); these panels fill in afterwards.
  const loadAuxiliary = () => {
    authFetchJson(`${API_BASE}/org/linked-status`)
      .then(ls => { if (ls) setLinkedStatus(ls); });
    authFetchJson(`${API_BASE}/org/owner/events`)
      .then(oe => { if (oe?.events) setOwnerEvents(oe.events); });
  };

  useEffect(() => { loadAuxiliary(); }, []);

  // ── Persist identity (name + slug + internal description) ──
  const openEditIdentity = () => {
    setDraftIdentity({
      company_name:         settings?.company_name || "",
      slug:                 settings?.slug || "",
      internal_description: settings?.internal_description || "",
    });
    setEditingIdentity(true);
  };
  const saveIdentity = async () => {
    if (!draftIdentity.company_name.trim()) {
      showToast(T.companyNameRequired || "Company name is required", "error");
      return;
    }
    setSavingIdentity(true);
    try {
      const res = await authFetch(`${API_BASE}/org/settings`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          company_name:         draftIdentity.company_name.trim(),
          slug:                 draftIdentity.slug.trim().toLowerCase(),
          internal_description: draftIdentity.internal_description,
        }),
      });
      const d = await res.json();
      if (res.ok) {
        
        setEditingIdentity(false);
        showToast(T.workspaceProfileSaved || "Workspace profile saved");
        refreshWorkspace();
      } else {
        showToast(d.detail || (T.failedToSave || "Failed to save"), "error");
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setSavingIdentity(false);
  };

  // ── Persist operating context ──
  const openEditContext = () => {
    setDraftContext({
      primary_country:  settings?.primary_country || "",
      default_timezone: settings?.default_timezone || "",
      default_currency: settings?.default_currency || "",
    });
    setEditingContext(true);
  };
  // Empty is allowed (it clears the field); anything else must be a real zone.
  // Blocking here saves a round-trip and puts the error next to the control
  // rather than in a toast — but it is a convenience, not the guarantee. The
  // backend rejects an invalid zone whatever the client sends.
  const tzInvalid = !!draftContext.default_timezone.trim()
    && !isSelectableZone(draftContext.default_timezone);

  const saveContext = async () => {
    if (tzInvalid) {
      showToast(T.timezoneInvalid || "Not a valid IANA timezone. Pick one from the list.", "error");
      return;
    }
    setSavingContext(true);
    try {
      const res = await authFetch(`${API_BASE}/org/settings`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          primary_country:  draftContext.primary_country.toUpperCase() || null,
          default_timezone: draftContext.default_timezone || null,
          default_currency: draftContext.default_currency.toUpperCase() || null,
        }),
      });
      const d = await res.json();
      if (res.ok) {
        
        setEditingContext(false);
        showToast(T.operatingContextSaved || "Operating context saved");
        refreshWorkspace();
      } else {
        showToast(d.detail || (T.failedToSave || "Failed to save"), "error");
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setSavingContext(false);
  };

  // ── Logo upload ──
  const handleLogoUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("logo", file);
    try {
      const res = await authFetch(`${API_BASE}/org/logo`, {
        // retries: 0 — an upload is not something to silently re-send on a 5xx.
        // The 401 refresh-and-replay still applies (the server rejected the
        // request before touching anything, so replaying is safe).
        method: "POST", body: fd, retries: 0, timeout: 30000,
      });
      const d = await res.json();
      if (res.ok && d.logo_url) {
        
        showToast(T.logoUpdated || "Workspace logo updated");
        refreshWorkspace();
      } else {
        showToast(d.detail || (T.failedToUploadLogo || "Failed to upload logo"), "error");
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
  };

  // ── Ownership transfer ──
  const handleTransfer = async () => {
    if (!transferEmail.trim()) {
      showToast(T.enterNewOwnerEmail || "Enter the new owner's work email", "error");
      return;
    }
    if (!transferConfirm) {
      showToast(
        T.confirmOwnershipTransfer ||
        "Tick the confirmation box — ownership transfer cannot be undone without another transfer.",
        "error",
      );
      return;
    }
    setTransferring(true);
    try {
      const res = await authFetch(`${API_BASE}/org/owner/transfer`, {
        // retries: 0 — ownership transfer is not idempotent, never auto-resend.
        method: "POST", retries: 0,
        headers: JSON_HEADERS,
        body: JSON.stringify({
          new_owner_email: transferEmail.trim(),
          confirm:         true,
        }),
      });
      const d = await res.json();
      if (res.ok) {
        
        setTransferEmail("");
        setTransferConfirm(false);
        showToast(T.ownershipTransferred || "Ownership transferred");
        refreshWorkspace(); loadAuxiliary();
      } else {
        showToast(d.detail || (T.failedToTransfer || "Failed to transfer ownership"), "error");
      }
    } catch { showToast(T.networkError || "Network error", "error"); }
    setTransferring(false);
  };

  // ── Company profile handlers ──
  const openEditProfile = () => {
    setDraftProfile({
      company_description: settings?.company_description || "",
      company_phone:       settings?.company_phone       || "",
      company_website:     settings?.company_website     || "",
      company_address:     settings?.company_address     || "",
    });
    setEditingProfile(true);
  };
  const saveProfile = async () => {
    if (draftProfile.company_website && !/^https?:\/\/.+/.test(draftProfile.company_website.trim())) {
      showToast("Website must start with http:// or https://", "error"); return;
    }
    setSavingProfile(true);
    try {
      const res = await authFetch(`${API_BASE}/org/settings`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          company_description: draftProfile.company_description || null,
          company_phone:       draftProfile.company_phone.trim()   || null,
          company_website:     draftProfile.company_website.trim() || null,
          company_address:     draftProfile.company_address        || null,
        }),
      });
      const d = await res.json();
      if (res.ok) {  setEditingProfile(false); showToast("Company profile saved"); refreshWorkspace(); }
      else          showToast(d.detail || "Failed to save", "error");
    } catch { showToast("Network error", "error"); }
    setSavingProfile(false);
  };

  // ── Contact info handlers ──
  const openEditContact = () => {
    setDraftContact({
      hr_email:      settings?.hr_email      || "",
      support_email: settings?.support_email || "",
    });
    setEditingContact(true);
  };
  const saveContact = async () => {
    const emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (draftContact.hr_email && !emailRe.test(draftContact.hr_email.trim())) {
      showToast("HR email must be a valid email address", "error"); return;
    }
    if (draftContact.support_email && !emailRe.test(draftContact.support_email.trim())) {
      showToast("Support email must be a valid email address", "error"); return;
    }
    setSavingContact(true);
    try {
      const res = await authFetch(`${API_BASE}/org/settings`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          hr_email:      draftContact.hr_email.trim().toLowerCase()      || null,
          support_email: draftContact.support_email.trim().toLowerCase() || null,
        }),
      });
      const d = await res.json();
      if (res.ok) {  setEditingContact(false); showToast("Contact information saved"); refreshWorkspace(); }
      else          showToast(d.detail || "Failed to save", "error");
    } catch { showToast("Network error", "error"); }
    setSavingContact(false);
  };

  // ── Branding handlers ──
  const openEditBranding = () => {
    setDraftBranding({
      primary_brand_color:   settings?.primary_brand_color   || "",
      secondary_brand_color: settings?.secondary_brand_color || "",
    });
    setEditingBranding(true);
  };
  const saveBranding = async () => {
    const hexRe = /^#[0-9A-Fa-f]{6}$/;
    if (draftBranding.primary_brand_color && !hexRe.test(draftBranding.primary_brand_color)) {
      showToast("Primary color must be a hex color e.g. #3D6BFF", "error"); return;
    }
    if (draftBranding.secondary_brand_color && !hexRe.test(draftBranding.secondary_brand_color)) {
      showToast("Secondary color must be a hex color e.g. #00BFA9", "error"); return;
    }
    setSavingBranding(true);
    try {
      const res = await authFetch(`${API_BASE}/org/settings`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({
          primary_brand_color:   draftBranding.primary_brand_color   || null,
          secondary_brand_color: draftBranding.secondary_brand_color || null,
        }),
      });
      const d = await res.json();
      if (res.ok) {  setEditingBranding(false); showToast("Branding settings saved"); refreshWorkspace(); }
      else          showToast(d.detail || "Failed to save", "error");
    } catch { showToast("Network error", "error"); }
    setSavingBranding(false);
  };

  // ── Render guards ──

  // Show skeleton in two cases:
  //   1. Actively loading (loading=true, settings=null)
  //   2. Initial render before the first fetch has even started
  //      (loading=false, settings=null, loadFailed=false)
  // Without the second case, the first render hits the error screen below
  // because effects haven't fired yet — causing a flash of "Could not load"
  // before the fetch even begins.
  if (!settings && !settingsLoadFailed) {
    return (
      <div style={{ padding: "24px 32px 80px", maxWidth: 980 }}>
        {[180, 160, 220].map((h, i) => (
          <div key={i} style={{
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 14, marginBottom: 18, padding: "20px 22px", overflow: "hidden",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
              <div style={{ width: 36, height: 36, borderRadius: 9, background: C.border }} />
              <div>
                <div style={{ height: 14, width: 140, background: C.border, borderRadius: 6, marginBottom: 6 }} />
                <div style={{ height: 11, width: 220, background: C.border, borderRadius: 6, opacity: .6 }} />
              </div>
            </div>
            {Array.from({ length: i === 2 ? 2 : 3 }).map((_, j) => (
              <div key={j} style={{
                height: 44, borderTop: `1px solid ${C.border}`,
                display: "flex", alignItems: "center", gap: 16, padding: "0 2px",
              }}>
                <div style={{ height: 12, width: 120, background: C.border, borderRadius: 5, flexShrink: 0 }} />
                <div style={{ height: 12, width: `${60 + j * 15}%`, background: C.border, borderRadius: 5, opacity: .5 }} />
              </div>
            ))}
          </div>
        ))}
      </div>
    );
  }

  // Failed to load — only shown when the fetch explicitly failed (loadFailed=true).
  // Previously this used `!loading && !settings` which fired on the first render
  // before effects ran, causing a false error flash on every first navigation.
  if (settingsLoadFailed && !settings) {
    return (
      <div style={{ padding: "80px 32px", maxWidth: 980, textAlign: "center" }}>
        <div style={{ fontSize: 32, marginBottom: 12 }}>⚠️</div>
        <p style={{ margin: "0 0 6px", fontSize: 16, fontWeight: 700, color: C.text }}>
          {T.workspaceProfileLoadError || "Could not load workspace profile"}
        </p>
        <p style={{ margin: "0 0 22px", fontSize: 13, color: C.textSub, lineHeight: 1.6 }}>
          {T.workspaceProfileLoadErrorSub ||
            "This is usually a temporary network issue. Your settings are safe — try again in a moment."}
        </p>
        <button
          onClick={() => { refreshWorkspace(); loadAuxiliary(); }}
          style={{
            padding: "10px 24px", borderRadius: 8, border: "none",
            background: C.brand, color: "#fff", fontWeight: 700,
            fontSize: 13, cursor: "pointer", fontFamily: "inherit",
          }}>
          {T.retryLoad || "Retry"}
        </button>
      </div>
    );
  }

  // ── Computed display values (real data only) ──
  const companyName     = settings?.company_name || "—";
  const logoUrl         = settings?.logo_url || null;
  const slug            = settings?.slug || "";
  const description     = settings?.internal_description || "";
  const country         = lookupCountry(settings?.primary_country);
  const timezone        = settings?.default_timezone || "—";
  const currencyCode    = settings?.default_currency || "";
  const currencyName    = CURRENCY_NAMES[currencyCode] || "";
  const dataRes         = settings?.data_residency || {};
  const owner           = settings?.owner || null;

  // ── Linked Configurations rows from real /org/linked-status data ──
  const linkedRows = linkedStatus ? [
    { key: "roles_permissions", icon: "👥", row: linkedStatus.roles_permissions, onClick: () => setSubTab && setSubTab("roles-permissions") },
    { key: "billing",           icon: "💳", row: linkedStatus.billing,           onClick: () => setSubTab && setSubTab("billing-subscription") },
    { key: "public_holidays",   icon: "📅", row: linkedStatus.public_holidays,   onClick: () => setSubTab && setSubTab("public-holidays") },
    { key: "security",          icon: "🔒", row: linkedStatus.security,          onClick: () => setSubTab && setSubTab("security") },
    { key: "audit_compliance",  icon: "📋", row: linkedStatus.audit_compliance,  onClick: () => setSubTab && setSubTab("audit-compliance") },
    { key: "policies",          icon: "⚙️", row: linkedStatus.policies,          onClick: () => setSubTab && setSubTab("policies") },
    { key: "integrations",      icon: "🔌", row: linkedStatus.integrations,      onClick: () => setSubTab && setSubTab("connected-systems") },
    { key: "custom_branding",   icon: "🎨", row: linkedStatus.custom_branding,   onClick: () => setSubTab && setSubTab("workspace-profile") },
  ].filter(r => r.row) : [];

  return (
    <div style={{ padding: "24px 32px 80px", maxWidth: 980 }}>
      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, marginBottom: 18 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 800, color: C.text }}>
            {T.companyWorkspaceProfile || "Workspace Profile"}
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: C.textSub, lineHeight: 1.6 }}>
            {T.workspaceProfileLead ||
              "Identity and control hub for this workspace — links to canonical configuration modules below."}
          </p>
        </div>
        <button onClick={() => navigate("/admin?tab=auditlog")}
          style={{
            display: "inline-flex", alignItems: "center", gap: 8,
            padding: "9px 14px", borderRadius: 8,
            border: `1px solid ${C.border}`, background: C.surface,
            color: C.text, fontWeight: 600, fontSize: 12.5,
            cursor: "pointer", fontFamily: "inherit", flexShrink: 0,
          }}>
          📋 {T.viewAuditLog || "View Audit Log"}
        </button>
      </div>

      {/* ── Workspace Identity ── */}
      <SectionCard
        C={C} icon="🏢"
        title={T.workspaceIdentity || "Workspace Identity"}
        subtitle={T.workspaceIdentitySub || "How this workspace appears in the product"}
        action={isAdmin && (
          <button
            onClick={openEditIdentity}
            disabled={loading || !settings}
            style={{
              padding: "7px 16px", borderRadius: 7, border: `1px solid ${C.border}`,
              background: C.surface, color: C.text, fontWeight: 600, fontSize: 12.5,
              cursor: (loading || !settings) ? "not-allowed" : "pointer",
              fontFamily: "inherit",
              opacity: (loading || !settings) ? 0.45 : 1,
            }}>
            {T.edit || "Edit"}
          </button>
        )}
      >
        <FieldRow C={C} label={T.workspaceName || "Workspace name"}>
          <span style={{ fontWeight: 600 }}>{companyName}</span>
        </FieldRow>
        <FieldRow C={C} label={T.workspaceUrlSlug || "Workspace URL / slug"}>
          <span style={{ fontFamily: "Consolas, 'Courier New', monospace", fontSize: 13 }}>
            {slug || (T.notSet || "Not set")}
          </span>
        </FieldRow>
        <FieldRow C={C} label={T.workspaceLogo || "Workspace logo"}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 12 }}>
            <span style={{
              width: 36, height: 36, borderRadius: 8,
              background: logoUrl ? "transparent" : "#10B981",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              color: "#fff", fontWeight: 800, fontSize: 14, overflow: "hidden",
              border: `1px solid ${C.border}`,
            }}>
              {logoUrl
                ? <img src={logoUrl} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                : initials(companyName)}
            </span>
            <span style={{ fontSize: 13, color: C.textSub }}>
              {logoUrl ? (T.logoUploaded || "Logo uploaded") : (T.notUploaded || "Not uploaded")}
            </span>
            {isAdmin && (
              <>
                <input ref={fileRef} type="file" accept="image/*" onChange={handleLogoUpload}
                       style={{ display: "none" }} />
                <button onClick={() => fileRef.current?.click()}
                  style={{ padding: "4px 10px", borderRadius: 6, border: `1px solid ${C.border}`,
                           background: C.surface, color: C.brand, fontWeight: 600, fontSize: 11.5,
                           cursor: "pointer", fontFamily: "inherit" }}>
                  {logoUrl ? (T.replaceLogo || "Replace") : (T.uploadLogo || "Upload")}
                </button>
              </>
            )}
          </span>
        </FieldRow>
        <div style={{
          display: "grid", gridTemplateColumns: "200px 1fr",
          gap: 16, alignItems: "start", padding: "14px 22px",
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: C.textSub }}>
            {T.internalDescription || "Internal description"}
          </span>
          <span style={{
            fontSize: 13, color: description ? C.text : C.textLight,
            fontStyle: description ? "normal" : "italic", lineHeight: 1.5,
          }}>
            {description ||
              (T.internalDescriptionPlaceholder ||
                "Add an internal note to help admins identify this workspace at a glance.")}
          </span>
        </div>
      </SectionCard>

      {/* ── Operating Context ── */}
      <SectionCard
        C={C} icon="🌐"
        title={T.operatingContext || "Operating Context"}
        subtitle={T.operatingContextSub || "Workspace-level defaults for display and setup recommendations"}
        action={isAdmin && (
          <button
            onClick={openEditContext}
            disabled={loading || !settings}
            style={{
              padding: "7px 16px", borderRadius: 7, border: `1px solid ${C.border}`,
              background: C.surface, color: C.text, fontWeight: 600, fontSize: 12.5,
              cursor: (loading || !settings) ? "not-allowed" : "pointer",
              fontFamily: "inherit",
              opacity: (loading || !settings) ? 0.45 : 1,
            }}>
            {T.edit || "Edit"}
          </button>
        )}
      >
        <FieldRow C={C} label={T.primaryCountry || "Primary country"}
                  hint={<span style={{ color: C.textLight }}>{T.setsDefaultsOnly || "Sets defaults only"}</span>}>
          <span>{country.flag} {country.name}</span>
        </FieldRow>
        {/* NOT "display" — this is the organization's business clock. The row
            used to be captioned "Admin & report display", which is how an
            invalid value sat here unnoticed: nobody reviewing it believed it
            affected attendance. It decides Late, Absent, the absent email, the
            attendance date, report ranges and payroll periods. */}
        <FieldRow C={C} label={T.organizationTimezone || "Organization Timezone"}
                  hint={<span style={{ color: C.textLight }}>
                    {T.organizationTimezoneUse || "Attendance, absence, shifts, reports, payroll"}
                  </span>}>
          <span>
            {timezone}
            {settings?.default_timezone && !isUsableZone(settings.default_timezone) && (
              <span style={{ marginLeft: 8, fontSize: 11, fontWeight: 600, color: C.danger || "#DC2626" }}>
                {T.timezoneInvalidStored || "not a valid IANA timezone — this workspace is running on the system fallback"}
              </span>
            )}
          </span>
        </FieldRow>
        <FieldRow C={C} label={T.defaultDisplayCurrency || "Default display currency"}
                  hint={<span style={{ color: C.textLight }}>{T.displayOnly || "Display only"}</span>}>
          {currencyCode ? `${currencyCode} — ${currencyName || currencyCode}` : (T.notSet || "Not set")}
        </FieldRow>
        <div style={{
          display: "grid", gridTemplateColumns: "200px 1fr auto",
          gap: 16, alignItems: "center", padding: "14px 22px",
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: C.textSub }}>
            {T.dataResidency || "Data residency"}
          </span>
          <div style={{
            background: "#F5F3FF", border: `1px solid #DDD6FE`,
            borderRadius: 10, padding: "10px 14px",
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
          }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", minWidth: 0 }}>
              <span style={{ fontSize: 14, color: "#6B21A8" }}>🛡️</span>
              <div style={{ minWidth: 0 }}>
                <p style={{ margin: 0, fontWeight: 700, color: "#5B21B6", fontSize: 13 }}>
                  {dataRes.label || "—"}
                </p>
                <p style={{ margin: 0, fontSize: 11.5, color: "#6B7280" }}>
                  {dataRes.detail || ""}
                </p>
              </div>
            </div>
            <Pill C={C} label={T.immutable || "Immutable"} tone="immutable" />
          </div>
          <span style={{ fontSize: 11, color: C.textLight, whiteSpace: "nowrap" }}>
            {T.infrastructureManaged || "Infrastructure-managed"}
          </span>
        </div>
      </SectionCard>

      {/* ── Company Profile ── */}
      <SectionCard
        C={C} icon="🏛️"
        title="Company Profile"
        subtitle="Public-facing company information shown in reports and employee communications"
        action={isAdmin && (
          <button
            onClick={openEditProfile}
            disabled={loading || !settings}
            style={{
              padding: "7px 16px", borderRadius: 7, border: `1px solid ${C.border}`,
              background: C.surface, color: C.text, fontWeight: 600, fontSize: 12.5,
              cursor: (loading || !settings) ? "not-allowed" : "pointer",
              fontFamily: "inherit", opacity: (loading || !settings) ? 0.45 : 1,
            }}>
            {T.edit || "Edit"}
          </button>
        )}
      >
        <FieldRow C={C} label="Company description">
          <span style={{ color: settings?.company_description ? C.text : C.textLight,
                         fontStyle: settings?.company_description ? "normal" : "italic", lineHeight: 1.5 }}>
            {settings?.company_description || "Not set"}
          </span>
        </FieldRow>
        <FieldRow C={C} label="Phone number">
          {settings?.company_phone || <span style={{ color: C.textLight, fontStyle: "italic" }}>Not set</span>}
        </FieldRow>
        <FieldRow C={C} label="Website">
          {settings?.company_website
            ? <a href={settings.company_website} target="_blank" rel="noreferrer"
                 style={{ color: C.brand, textDecoration: "none", fontWeight: 500 }}>
                {settings.company_website}
              </a>
            : <span style={{ color: C.textLight, fontStyle: "italic" }}>Not set</span>}
        </FieldRow>
        <FieldRow C={C} label="Address">
          <span style={{ color: settings?.company_address ? C.text : C.textLight,
                         fontStyle: settings?.company_address ? "normal" : "italic", lineHeight: 1.5,
                         whiteSpace: "pre-wrap" }}>
            {settings?.company_address || "Not set"}
          </span>
        </FieldRow>
      </SectionCard>

      {/* ── Contact Information ── */}
      <SectionCard
        C={C} icon="📬"
        title="Contact Information"
        subtitle="HR and support email addresses used for employee notifications and platform alerts"
        action={isAdmin && (
          <button
            onClick={openEditContact}
            disabled={loading || !settings}
            style={{
              padding: "7px 16px", borderRadius: 7, border: `1px solid ${C.border}`,
              background: C.surface, color: C.text, fontWeight: 600, fontSize: 12.5,
              cursor: (loading || !settings) ? "not-allowed" : "pointer",
              fontFamily: "inherit", opacity: (loading || !settings) ? 0.45 : 1,
            }}>
            {T.edit || "Edit"}
          </button>
        )}
      >
        <FieldRow C={C} label="HR email" hint={<span style={{ color: C.textLight }}>Leave approvals · absence alerts</span>}>
          {settings?.hr_email
            ? <span style={{ fontFamily: "Consolas, 'Courier New', monospace", fontSize: 13 }}>{settings.hr_email}</span>
            : <span style={{ color: C.textLight, fontStyle: "italic" }}>Not set — leave notifications will not be delivered</span>}
        </FieldRow>
        <FieldRow C={C} label="Support email" hint={<span style={{ color: C.textLight }}>Platform support contact</span>}>
          {settings?.support_email
            ? <span style={{ fontFamily: "Consolas, 'Courier New', monospace", fontSize: 13 }}>{settings.support_email}</span>
            : <span style={{ color: C.textLight, fontStyle: "italic" }}>Not set</span>}
        </FieldRow>
      </SectionCard>

      {/* ── Branding ── */}
      <SectionCard
        C={C} icon="🎨"
        title="Branding"
        subtitle="Brand colors applied to your organization's workspace appearance"
        action={isAdmin && (
          <button
            onClick={openEditBranding}
            disabled={loading || !settings}
            style={{
              padding: "7px 16px", borderRadius: 7, border: `1px solid ${C.border}`,
              background: C.surface, color: C.text, fontWeight: 600, fontSize: 12.5,
              cursor: (loading || !settings) ? "not-allowed" : "pointer",
              fontFamily: "inherit", opacity: (loading || !settings) ? 0.45 : 1,
            }}>
            {T.edit || "Edit"}
          </button>
        )}
      >
        <FieldRow C={C} label="Primary brand color">
          {settings?.primary_brand_color
            ? <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                <span style={{ width: 22, height: 22, borderRadius: 6, background: settings.primary_brand_color,
                               border: `1px solid ${C.border}`, flexShrink: 0 }} />
                <span style={{ fontFamily: "Consolas, 'Courier New', monospace", fontSize: 13 }}>
                  {settings.primary_brand_color}
                </span>
              </span>
            : <span style={{ color: C.textLight, fontStyle: "italic" }}>Not set</span>}
        </FieldRow>
        <FieldRow C={C} label="Secondary brand color">
          {settings?.secondary_brand_color
            ? <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                <span style={{ width: 22, height: 22, borderRadius: 6, background: settings.secondary_brand_color,
                               border: `1px solid ${C.border}`, flexShrink: 0 }} />
                <span style={{ fontFamily: "Consolas, 'Courier New', monospace", fontSize: 13 }}>
                  {settings.secondary_brand_color}
                </span>
              </span>
            : <span style={{ color: C.textLight, fontStyle: "italic" }}>Not set</span>}
        </FieldRow>
      </SectionCard>

      {/* ── Workspace Owner ── */}
      <SectionCard
        C={C} icon="👤"
        title={T.workspaceOwner || "Workspace Owner"}
        subtitle={T.workspaceOwnerSub || "Accountable administrator for this workspace"}
      >
        <div style={{ padding: "20px 22px" }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between",
                        gap: 16, marginBottom: 18 }}>
            <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
              <div style={{
                width: 56, height: 56, borderRadius: "50%",
                background: "linear-gradient(135deg,#7C3AED 0%,#5B21B6 100%)",
                color: "#fff", display: "flex", alignItems: "center", justifyContent: "center",
                fontWeight: 800, fontSize: 18, flexShrink: 0,
              }}>{initials(owner?.name)}</div>
              <div style={{ minWidth: 0 }}>
                <p style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>
                  {owner?.name || (T.noOwnerAssigned || "No owner assigned")}
                </p>
                <p style={{ margin: "2px 0 0", fontSize: 12.5, color: C.textSub }}>
                  {owner?.email || "—"}
                </p>
                <div style={{ marginTop: 6 }}>
                  <Pill C={C} label={T.workspaceOwner || "Workspace Owner"} tone="success" />
                </div>
              </div>
            </div>
            <div style={{ textAlign: "right", flexShrink: 0 }}>
              <p style={{ margin: 0, fontSize: 11, color: C.textLight,
                          textTransform: "uppercase", letterSpacing: 0.5, fontWeight: 700 }}>
                {T.ownerSince || "Owner since"}
              </p>
              <p style={{ margin: "2px 0 0", fontSize: 12, color: C.text,
                          fontFamily: "Consolas, 'Courier New', monospace" }}>
                {fmtDate(owner?.since)}
              </p>
            </div>
          </div>

          {isAdmin && (
            <>
              <div style={{
                background: C.bg, border: `1px solid ${C.border}`,
                borderRadius: 10, padding: "12px 14px", marginBottom: 12,
              }}>
                <p style={{ margin: "0 0 8px", fontSize: 13, fontWeight: 700, color: C.text }}>
                  {T.transferWorkspaceOwnership || "Transfer Workspace Ownership"}
                </p>
                <div style={{ display: "flex", gap: 10, marginBottom: 8 }}>
                  <input
                    type="email"
                    value={transferEmail}
                    onChange={e => setTransferEmail(e.target.value)}
                    placeholder={T.newOwnerEmailPlaceholder || "New owner work email address..."}
                    style={{
                      flex: 1, padding: "9px 12px", borderRadius: 7,
                      border: `1px solid ${C.border}`, fontFamily: "inherit",
                      fontSize: 13, outline: "none",
                      background: C.surface, color: C.text,
                    }} />
                  <button
                    onClick={handleTransfer} disabled={transferring}
                    style={{
                      padding: "9px 18px", borderRadius: 7, border: "none",
                      background: C.brand, color: "#fff", fontWeight: 700,
                      fontSize: 12.5, cursor: transferring ? "wait" : "pointer",
                      fontFamily: "inherit", opacity: transferring ? 0.6 : 1,
                    }}>
                    {transferring ? (T.transferring || "Transferring…") : (T.transfer || "Transfer")} →
                  </button>
                </div>
                <label style={{
                  display: "inline-flex", alignItems: "center", gap: 8,
                  fontSize: 12, color: C.textSub, cursor: "pointer",
                }}>
                  <input type="checkbox" checked={transferConfirm}
                         onChange={e => setTransferConfirm(e.target.checked)} />
                  {T.confirmOwnershipTransferLabel ||
                    "I understand this is a sensitive, audited action."}
                </label>
              </div>
              <p style={{ margin: 0, fontSize: 11.5, color: "#B45309", lineHeight: 1.6 }}>
                ⚠️ {T.ownershipTransferWarning ||
                  "Ownership transfer requires confirmation, generates a sensitive audit event, and immediately grants the new owner all workspace privileges. This action cannot be undone without a new transfer."}
              </p>
            </>
          )}

          {/* Recent owner events */}
          <div style={{ marginTop: 18 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <p style={{
                margin: 0, fontSize: 11, color: C.textLight, fontWeight: 700,
                textTransform: "uppercase", letterSpacing: 0.6,
              }}>
                {T.recentOwnerEvents || "Recent Owner Events"}
              </p>
              <button onClick={() => navigate("/admin?tab=auditlog")}
                style={{ background: "none", border: "none", padding: 0,
                         color: C.brand, fontWeight: 600, fontSize: 12,
                         cursor: "pointer", fontFamily: "inherit" }}>
                {T.viewFullLog || "View full log"} →
              </button>
            </div>
            {ownerEvents.length === 0 ? (
              <div style={{
                padding: "16px 14px", border: `1px dashed ${C.border}`,
                borderRadius: 8, color: C.textLight, fontSize: 12.5, textAlign: "center",
              }}>
                {T.noRecentOwnerEvents ||
                  "No owner events to show yet. Profile updates and ownership transfers will appear here."}
              </div>
            ) : (
              <div>
                {ownerEvents.map(ev => (
                  <div key={ev.id} style={{
                    display: "flex", justifyContent: "space-between", gap: 12,
                    padding: "10px 12px", borderTop: `1px solid ${C.border}`,
                    fontSize: 12,
                  }}>
                    <div style={{ display: "flex", gap: 10, minWidth: 0 }}>
                      <span style={{ width: 6, height: 6, marginTop: 6, borderRadius: "50%",
                                     background: "#10B981", flexShrink: 0 }} />
                      <div style={{ minWidth: 0 }}>
                        <p style={{ margin: 0, fontWeight: 700, color: C.text }}>
                          {ev.action}
                          {ev.actor_name && (
                            <span style={{ fontWeight: 500, color: C.textSub }}>
                              {" — "}{ev.actor_name}
                            </span>
                          )}
                        </p>
                        {ev.payload && (
                          <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 11,
                                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {ev.entity}
                            {ev.ip_address ? ` · IP ${ev.ip_address}` : ""}
                          </p>
                        )}
                      </div>
                    </div>
                    <span style={{
                      fontFamily: "Consolas, 'Courier New', monospace",
                      color: C.textLight, fontSize: 11, whiteSpace: "nowrap",
                    }}>{fmtDate(ev.at)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </SectionCard>

      {/* ── Linked Configurations ── */}
      <SectionCard
        C={C} icon="🔗"
        title={T.linkedConfigurations || "Linked Configurations"}
        subtitle={T.linkedConfigurationsSub || "Status overview — configure each module in its canonical location"}
      >
        <div>
          {linkedRows.map((r, idx) => (
            <button
              key={r.key}
              onClick={r.onClick}
              style={{
                display: "grid", gridTemplateColumns: "auto 1fr auto",
                gap: 14, alignItems: "center", width: "100%", textAlign: "left",
                padding: "14px 22px", background: "transparent", border: "none",
                borderTop: idx === 0 ? "none" : `1px solid ${C.border}`,
                cursor: "pointer", fontFamily: "inherit",
                transition: "background .14s ease",
              }}
              onMouseEnter={e => e.currentTarget.style.background = C.bg}
              onMouseLeave={e => e.currentTarget.style.background = "transparent"}
            >
              <span style={{ fontSize: 18 }}>{r.icon}</span>
              <div style={{ minWidth: 0 }}>
                <p style={{ margin: 0, fontSize: 13.5, fontWeight: 700, color: C.text }}>
                  {r.row.title}
                </p>
                <p style={{ margin: "2px 0 0", fontSize: 12, color: C.textSub }}>
                  {r.row.detail}
                </p>
              </div>
              <span style={{
                display: "inline-flex", alignItems: "center", gap: 6,
                fontSize: 12, fontWeight: 600, color: C.textSub,
              }}>
                <StatusDot tone={r.row.status} />
                {STATUS_LABEL[r.row.status] || r.row.status}
                <span style={{ marginLeft: 6, color: C.textLight }}>→</span>
              </span>
            </button>
          ))}
          {linkedRows.length === 0 && (
            <p style={{ margin: 0, padding: 22, textAlign: "center", color: C.textLight, fontSize: 12.5 }}>
              {T.noLinkedConfigurations || "Status data is unavailable right now."}
            </p>
          )}
        </div>
      </SectionCard>

      {/* ── Edit Identity Modal ── */}
      {editingIdentity && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(15,23,42,0.5)",
          display: "flex", alignItems: "center", justifyContent: "center",
          zIndex: 9000, padding: 20,
        }} onClick={() => setEditingIdentity(false)}>
          <div onClick={e => e.stopPropagation()} style={{
            width: 520, maxWidth: "100%", background: C.surface,
            borderRadius: 14, padding: 26, maxHeight: "90vh", overflowY: "auto",
            boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
          }}>
            <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
              {T.editWorkspaceIdentity || "Edit Workspace Identity"}
            </p>
            <p style={{ margin: "4px 0 18px", fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>
              {T.editWorkspaceIdentitySub ||
                "Update how this workspace appears across reports, the worker dashboard, and the workspace switcher."}
            </p>

            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
              {T.workspaceName || "Workspace name"} *
            </label>
            <input
              value={draftIdentity.company_name}
              onChange={e => setDraftIdentity(p => ({ ...p, company_name: e.target.value }))}
              placeholder={T.companyNamePlaceholder || "Your company name"}
              style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                       border: `1px solid ${C.border}`, fontSize: 14, fontFamily: "inherit",
                       background: C.surface, color: C.text, outline: "none",
                       boxSizing: "border-box", marginBottom: 14 }} />

            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
              {T.workspaceUrlSlug || "Workspace URL / slug"}
            </label>
            <input
              value={draftIdentity.slug}
              onChange={e => setDraftIdentity(p => ({ ...p, slug: e.target.value }))}
              placeholder="acme-corp"
              style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                       border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "Consolas, 'Courier New', monospace",
                       background: C.surface, color: C.text, outline: "none",
                       boxSizing: "border-box", marginBottom: 4 }} />
            <p style={{ margin: "0 0 14px", fontSize: 11, color: C.textLight, lineHeight: 1.5 }}>
              {T.slugHint || "Lowercase letters, numbers, and single hyphens. Must be unique across Temveris."}
            </p>

            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
              {T.internalDescription || "Internal description"}
            </label>
            <textarea
              value={draftIdentity.internal_description}
              onChange={e => setDraftIdentity(p => ({ ...p, internal_description: e.target.value }))}
              placeholder={T.internalDescriptionHint ||
                "Optional — internal note to help admins identify this workspace."}
              rows={3}
              style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                       border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                       background: C.surface, color: C.text, outline: "none",
                       boxSizing: "border-box", resize: "vertical", marginBottom: 18 }} />

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button onClick={() => setEditingIdentity(false)}
                style={{ padding: "9px 18px", borderRadius: 7,
                         border: `1px solid ${C.border}`, background: C.surface,
                         color: C.text, fontWeight: 600, fontSize: 13,
                         cursor: "pointer", fontFamily: "inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={saveIdentity} disabled={savingIdentity || !draftIdentity.company_name.trim()}
                style={{ padding: "9px 22px", borderRadius: 7, border: "none",
                         background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13,
                         cursor: "pointer", fontFamily: "inherit",
                         opacity: (!draftIdentity.company_name.trim() || savingIdentity) ? 0.55 : 1 }}>
                {savingIdentity ? (T.saving || "Saving...") : (T.save || "Save")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Edit Operating Context Modal ── */}
      {editingContext && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(15,23,42,0.5)",
          display: "flex", alignItems: "center", justifyContent: "center",
          zIndex: 9000, padding: 20,
        }} onClick={() => setEditingContext(false)}>
          <div onClick={e => e.stopPropagation()} style={{
            width: 520, maxWidth: "100%", background: C.surface,
            borderRadius: 14, padding: 26, maxHeight: "90vh", overflowY: "auto",
            boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
          }}>
            <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
              {T.editOperatingContext || "Edit Operating Context"}
            </p>
            <p style={{ margin: "4px 0 18px", fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>
              {T.editOperatingContextSub ||
                "Workspace-level defaults used by reports and setup recommendations."}
            </p>

            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
              {T.primaryCountry || "Primary country"}
            </label>
            <select
              value={draftContext.primary_country}
              onChange={e => setDraftContext(p => ({ ...p, primary_country: e.target.value }))}
              style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                       border: `1px solid ${C.border}`, fontSize: 14, fontFamily: "inherit",
                       background: C.surface, color: C.text, outline: "none",
                       boxSizing: "border-box", marginBottom: 14 }}>
              <option value="">— {T.notSet || "Not set"} —</option>
              {Object.entries(COUNTRY_NAMES).map(([cc, info]) => (
                <option key={cc} value={cc}>{info.flag} {info.name} ({cc})</option>
              ))}
            </select>

            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
              {T.organizationTimezone || "Organization Timezone"}
            </label>
            {/* Searchable combobox — click shows every zone straight away,
                typing filters them, and only list entries can be chosen. See
                TimezoneCombobox for why free text, <datalist> and <select> each
                fell short. An invalid stored value (production held
                "California, Sacramento") stays visible in the field and is
                called out below, so it is never silently swapped for whatever
                option happened to be first. */}
            <TimezoneCombobox
              value={draftContext.default_timezone}
              onChange={tz => setDraftContext(p => ({ ...p, default_timezone: tz }))}
              C={C}
              T={T}
              invalid={tzInvalid} />
            {tzInvalid && (
              <p style={{ margin: "0 0 6px", fontSize: 11, color: C.danger || "#DC2626", lineHeight: 1.5 }}>
                {T.timezoneInvalid || "Not a valid IANA timezone. Pick one from the list."}
              </p>
            )}
            <p style={{ margin: "0 0 14px", fontSize: 11, color: C.textLight, lineHeight: 1.5 }}>
              {T.organizationTimezoneHint
                || "Business timezone used for attendance, absence, shifts, reports, payroll periods, and day boundaries. Applies to this organization and every employee in it."}
            </p>

            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
              {T.defaultDisplayCurrency || "Default display currency"}
            </label>
            <select
              value={draftContext.default_currency}
              onChange={e => setDraftContext(p => ({ ...p, default_currency: e.target.value }))}
              style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                       border: `1px solid ${C.border}`, fontSize: 14, fontFamily: "inherit",
                       background: C.surface, color: C.text, outline: "none",
                       boxSizing: "border-box", marginBottom: 18 }}>
              <option value="">— {T.notSet || "Not set"} —</option>
              {Object.entries(CURRENCY_NAMES).map(([code, name]) => (
                <option key={code} value={code}>{code} — {name}</option>
              ))}
            </select>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button onClick={() => setEditingContext(false)}
                style={{ padding: "9px 18px", borderRadius: 7,
                         border: `1px solid ${C.border}`, background: C.surface,
                         color: C.text, fontWeight: 600, fontSize: 13,
                         cursor: "pointer", fontFamily: "inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={saveContext} disabled={savingContext || tzInvalid}
                style={{ padding: "9px 22px", borderRadius: 7, border: "none",
                         background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13,
                         cursor: (savingContext || tzInvalid) ? "not-allowed" : "pointer",
                         fontFamily: "inherit",
                         opacity: (savingContext || tzInvalid) ? 0.55 : 1 }}>
                {savingContext ? (T.saving || "Saving...") : (T.save || "Save")}
              </button>
            </div>
          </div>
        </div>
      )}
      {/* ── Edit Company Profile Modal ── */}
      {editingProfile && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.5)",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      zIndex: 9000, padding: 20 }} onClick={() => setEditingProfile(false)}>
          <div onClick={e => e.stopPropagation()} style={{
            width: 540, maxWidth: "100%", background: C.surface, borderRadius: 14,
            padding: 26, maxHeight: "90vh", overflowY: "auto",
            boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
          }}>
            <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>Edit Company Profile</p>
            <p style={{ margin: "4px 0 20px", fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>
              Public-facing details shown in reports and employee communications.
            </p>

            {[
              { label: "Company description", key: "company_description", type: "textarea",
                placeholder: "Brief description of what your company does." },
              { label: "Phone number", key: "company_phone", type: "input",
                placeholder: "+1 555 000 0000" },
              { label: "Website", key: "company_website", type: "input",
                placeholder: "https://yourcompany.com" },
            ].map(({ label, key, type, placeholder }) => (
              <div key={key} style={{ marginBottom: 14 }}>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                  {label}
                </label>
                {type === "textarea"
                  ? <textarea value={draftProfile[key]}
                      onChange={e => setDraftProfile(p => ({ ...p, [key]: e.target.value }))}
                      placeholder={placeholder} rows={3}
                      style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                               border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                               background: C.surface, color: C.text, outline: "none",
                               boxSizing: "border-box", resize: "vertical" }} />
                  : <input value={draftProfile[key]}
                      onChange={e => setDraftProfile(p => ({ ...p, [key]: e.target.value }))}
                      placeholder={placeholder}
                      style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                               border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                               background: C.surface, color: C.text, outline: "none",
                               boxSizing: "border-box" }} />}
              </div>
            ))}

            <div style={{ marginBottom: 20 }}>
              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                Address
              </label>
              <textarea value={draftProfile.company_address}
                onChange={e => setDraftProfile(p => ({ ...p, company_address: e.target.value }))}
                placeholder={"123 Main Street\nCity, State, ZIP\nCountry"} rows={3}
                style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                         border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                         background: C.surface, color: C.text, outline: "none",
                         boxSizing: "border-box", resize: "vertical" }} />
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button onClick={() => setEditingProfile(false)}
                style={{ padding: "9px 18px", borderRadius: 7, border: `1px solid ${C.border}`,
                         background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
                         cursor: "pointer", fontFamily: "inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={saveProfile} disabled={savingProfile}
                style={{ padding: "9px 22px", borderRadius: 7, border: "none",
                         background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13,
                         cursor: savingProfile ? "wait" : "pointer", fontFamily: "inherit",
                         opacity: savingProfile ? 0.55 : 1 }}>
                {savingProfile ? (T.saving || "Saving...") : (T.save || "Save")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Edit Contact Information Modal ── */}
      {editingContact && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.5)",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      zIndex: 9000, padding: 20 }} onClick={() => setEditingContact(false)}>
          <div onClick={e => e.stopPropagation()} style={{
            width: 500, maxWidth: "100%", background: C.surface, borderRadius: 14,
            padding: 26, maxHeight: "90vh", overflowY: "auto",
            boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
          }}>
            <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>Edit Contact Information</p>
            <p style={{ margin: "4px 0 20px", fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>
              These addresses receive leave approval notifications and platform alerts for your organization.
            </p>

            {[
              { label: "HR Email", key: "hr_email", hint: "Receives leave approvals and absence alerts" },
              { label: "Support Email", key: "support_email", hint: "Platform support and IT contact for this workspace" },
            ].map(({ label, key, hint }) => (
              <div key={key} style={{ marginBottom: 16 }}>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                  {label}
                </label>
                <input type="email" value={draftContact[key]}
                  onChange={e => setDraftContact(p => ({ ...p, [key]: e.target.value }))}
                  placeholder="name@yourcompany.com"
                  style={{ width: "100%", padding: "10px 12px", borderRadius: 7,
                           border: `1px solid ${C.border}`, fontSize: 13, fontFamily: "inherit",
                           background: C.surface, color: C.text, outline: "none",
                           boxSizing: "border-box", marginBottom: 4 }} />
                <p style={{ margin: 0, fontSize: 11, color: C.textLight }}>{hint}</p>
              </div>
            ))}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 8 }}>
              <button onClick={() => setEditingContact(false)}
                style={{ padding: "9px 18px", borderRadius: 7, border: `1px solid ${C.border}`,
                         background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
                         cursor: "pointer", fontFamily: "inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={saveContact} disabled={savingContact}
                style={{ padding: "9px 22px", borderRadius: 7, border: "none",
                         background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13,
                         cursor: savingContact ? "wait" : "pointer", fontFamily: "inherit",
                         opacity: savingContact ? 0.55 : 1 }}>
                {savingContact ? (T.saving || "Saving...") : (T.save || "Save")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Edit Branding Modal ── */}
      {editingBranding && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.5)",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      zIndex: 9000, padding: 20 }} onClick={() => setEditingBranding(false)}>
          <div onClick={e => e.stopPropagation()} style={{
            width: 480, maxWidth: "100%", background: C.surface, borderRadius: 14,
            padding: 26, maxHeight: "90vh", overflowY: "auto",
            boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
          }}>
            <p style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>Edit Branding</p>
            <p style={{ margin: "4px 0 20px", fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>
              Set your organization's brand colors. Use 6-digit hex format e.g. #3D6BFF
            </p>

            {[
              { label: "Primary brand color", key: "primary_brand_color", placeholder: "#3D6BFF" },
              { label: "Secondary brand color", key: "secondary_brand_color", placeholder: "#00BFA9" },
            ].map(({ label, key, placeholder }) => (
              <div key={key} style={{ marginBottom: 16 }}>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: C.text, marginBottom: 5 }}>
                  {label}
                </label>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <input type="color"
                    value={draftBranding[key] || "#cccccc"}
                    onChange={e => setDraftBranding(p => ({ ...p, [key]: e.target.value }))}
                    style={{ width: 38, height: 38, borderRadius: 7, border: `1px solid ${C.border}`,
                             padding: 2, cursor: "pointer", background: "none", flexShrink: 0 }} />
                  <input value={draftBranding[key]}
                    onChange={e => setDraftBranding(p => ({ ...p, [key]: e.target.value }))}
                    placeholder={placeholder}
                    style={{ flex: 1, padding: "10px 12px", borderRadius: 7,
                             border: `1px solid ${C.border}`, fontSize: 13,
                             fontFamily: "Consolas, 'Courier New', monospace",
                             background: C.surface, color: C.text, outline: "none" }} />
                </div>
                <p style={{ margin: "4px 0 0", fontSize: 11, color: C.textLight }}>
                  Must be a valid 6-digit hex color e.g. #3D6BFF
                </p>
              </div>
            ))}

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 8 }}>
              <button onClick={() => setEditingBranding(false)}
                style={{ padding: "9px 18px", borderRadius: 7, border: `1px solid ${C.border}`,
                         background: C.surface, color: C.text, fontWeight: 600, fontSize: 13,
                         cursor: "pointer", fontFamily: "inherit" }}>
                {T.cancel || "Cancel"}
              </button>
              <button onClick={saveBranding} disabled={savingBranding}
                style={{ padding: "9px 22px", borderRadius: 7, border: "none",
                         background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13,
                         cursor: savingBranding ? "wait" : "pointer", fontFamily: "inherit",
                         opacity: savingBranding ? 0.55 : 1 }}>
                {savingBranding ? (T.saving || "Saving...") : (T.save || "Save")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
