/**
 * Temveris — Admin Billing Module
 * ================================
 * Implements the May 1 launch spec: Overview cards · Plan comparison ·
 * Upgrade / Downgrade flows · Enterprise quote · Seat management ·
 * Invoices · Payment method · Billing contacts · Tax profile ·
 * Billing audit trail. All commercial values come from the API
 * (/api/admin/billing/*) — no prices are hardcoded in this view.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE } from "../config";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useEntitlements } from "../context/EntitlementsContext";

// ── Icon set local to billing — kept tiny so we don't bloat AdminDashboard ───
const ICONS = {
  card:        "M2 7h20v10a2 2 0 01-2 2H4a2 2 0 01-2-2V7z M2 11h20",
  receipt:     "M5 3h14a1 1 0 011 1v17l-3-2-3 2-3-2-3 2-3-2-3 2V4a1 1 0 011-1z M9 9h6 M9 13h6 M9 17h4",
  shield:      "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  users:       "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M16 3.13a4 4 0 010 7.75 M9 11a4 4 0 100-8 4 4 0 000 8z",
  download:    "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  refresh:     "M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10 M20.49 15a9 9 0 01-14.85 3.36L1 14",
  check:       "M5 12l5 5 9-9",
  x:           "M18 6L6 18 M6 6l12 12",
  arrow_up:    "M12 19V5 M5 12l7-7 7 7",
  arrow_down:  "M12 5v14 M19 12l-7 7-7-7",
  alert:       "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  info:        "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 16v-4 M12 8h.01",
  edit:        "M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7 M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z",
  trash:       "M3 6h18 M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2",
  plus:        "M12 5v14 M5 12h14",
  star:        "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z",
  calendar:    "M3 4h18v18H3z M16 2v4 M8 2v4 M3 10h18",
  briefcase:   "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  globe:       "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M2 12h20 M12 2a15 15 0 010 20 15 15 0 010-20z",
  history:     "M3 12a9 9 0 1 0 3-6.7L3 8 M3 3v5h5",
  lock:        "M5 11h14v10H5z M8 11V7a4 4 0 018 0v4",
};

const BIcon = ({ n, s = 16, c = "#475467", strokeWidth = 2 }) => (
  <svg
    width={s}
    height={s}
    viewBox="0 0 24 24"
    fill="none"
    stroke={c}
    strokeWidth={strokeWidth}
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ display: "inline-block", flexShrink: 0 }}
  >
    {(ICONS[n] || "").split(" M").map((seg, i) => (
      <path key={i} d={i === 0 ? seg : "M" + seg} />
    ))}
  </svg>
);

const fmtMoney = (n, ccy = "USD") => {
  if (n === undefined || n === null || Number.isNaN(Number(n))) return "—";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: ccy, maximumFractionDigits: 2 }).format(Number(n));
  } catch {
    return `${ccy} ${Number(n).toFixed(2)}`;
  }
};

const fmtDate = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "2-digit" }); }
  catch { return "—"; }
};

const titleCase = (s) =>
  String(s || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (m) => m.toUpperCase());

const STATUS_TINTS = (C) => ({
  active:            { color: C.green,  bg: C.greenBg,  border: C.greenBdr,  label: "Active" },
  // The backend sometimes emits "trial", sometimes "trial_active" — keep
  // both keys so the Billing Status pill always resolves and reads
  // "Trial active" (matches the Figma).
  trial:             { color: C.brand,  bg: C.brandBg,  border: C.brandBdr,  label: "Trial active" },
  trial_active:      { color: C.brand,  bg: C.brandBg,  border: C.brandBdr,  label: "Trial active" },
  // The 14-day trial ran out and no plan was chosen. Nothing was charged and no data
  // was removed, so this reads as a plain fact rather than a failure.
  trial_expired:     { color: C.amber,  bg: C.amberBg,  border: C.amberBdr,  label: "Trial ended" },
  past_due:          { color: C.red,    bg: C.redBg,    border: C.redBdr,    label: "Past Due" },
  // Scheduled to cancel at period end — service is still live.
  cancel_pending:    { color: C.amber,  bg: C.amberBg,  border: C.amberBdr,  label: "Cancel Pending" },
  // Terminal: the Stripe subscription is gone. Must never render as "Active".
  canceled:          { color: C.textSub, bg: C.borderLight, border: C.border, label: "Cancelled" },
  // First payment never succeeded, so the subscription was never usable.
  incomplete:        { color: C.amber,  bg: C.amberBg,  border: C.amberBdr,  label: "Payment Incomplete" },
  incomplete_expired:{ color: C.red,    bg: C.redBg,    border: C.redBdr,    label: "Payment Failed" },
  enterprise_contract: { color: "#7C3AED", bg: "#F5F3FF", border: "#DDD6FE", label: "Enterprise" },
  paid:    { color: C.green, bg: C.greenBg, border: C.greenBdr, label: "Paid" },
  issued:  { color: C.brand, bg: C.brandBg, border: C.brandBdr, label: "Issued" },
  draft:   { color: C.textSub, bg: C.borderLight, border: C.border, label: "Draft" },
  pending: { color: C.amber, bg: C.amberBg, border: C.amberBdr, label: "Pending" },
  failed:  { color: C.red, bg: C.redBg, border: C.redBdr, label: "Failed" },
  voided:  { color: C.textSub, bg: C.borderLight, border: C.border, label: "Voided" },
  // Alias — backend uses "void", Figma renders "Void" pill.
  void:    { color: C.textSub, bg: C.borderLight, border: C.border, label: "Void" },
  refunded: { color: "#7C3AED", bg: "#F5F3FF", border: "#DDD6FE", label: "Refunded" },
});


// ─── Toast (local, lightweight) ──────────────────────────────────────────────
function useToast() {
  const [toast, setToast] = useState(null);
  const show = useCallback((msg, kind = "success") => {
    setToast({ msg, kind });
    setTimeout(() => setToast(null), 3500);
  }, []);
  return { toast, show };
}


// ─── Reusable presentational atoms ───────────────────────────────────────────
function Badge({ children, color, bg, border, size = "sm" }) {
  const padY = size === "sm" ? "3px" : "5px";
  const padX = size === "sm" ? "9px" : "12px";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      fontSize: size === "sm" ? 11 : 12, fontWeight: 700,
      letterSpacing: 0.2, padding: `${padY} ${padX}`,
      borderRadius: 999, color, background: bg,
      border: `1px solid ${border}`, whiteSpace: "nowrap",
    }}>{children}</span>
  );
}


function SectionCard({ C, title, subtitle, action, children, padding }) {
  return (
    <section style={{
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: C.r, boxShadow: "0 1px 2px rgba(16,24,40,.04)",
      overflow: "hidden",
    }}>
      {(title || action) && (
        <header style={{
          display: "flex", alignItems: "flex-start", justifyContent: "space-between",
          padding: "18px 22px 14px", gap: 16, borderBottom: `1px solid ${C.borderLight}`,
        }}>
          <div>
            {title && <h3 style={{ fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>{title}</h3>}
            {subtitle && <p style={{ fontSize: 12.5, color: C.textSub, marginTop: 4 }}>{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      <div style={{ padding: padding ?? "20px 22px" }}>{children}</div>
    </section>
  );
}


// ═══════════════════════════════════════════════════════════════════════════
//   PRIMARY COMPONENT
// ═══════════════════════════════════════════════════════════════════════════
export default function BillingModule() {
  const { C, isDark } = useTheme();
  const { T } = useLanguage();
  // A successful plan change must refresh the entitlement projection so gated
  // features unlock in place. The backend is already correct after the change;
  // this only re-fetches the actor projection — it never alters entitlement
  // state locally.
  const { refresh: refreshEntitlements } = useEntitlements();
  const { toast, show: showToast } = useToast();
  const navigate = useNavigate();
  const STATUS = STATUS_TINTS(C);

  const tr = (key, fallback) => (T && T[key]) || fallback;

  const tokenRef = (() => localStorage.getItem("zt_token") || localStorage.getItem("token") || "");
  const authHeaders = () => ({ Authorization: `Bearer ${tokenRef()}`, "Content-Type": "application/json" });

  // Top-level routing within the module
  const [view, setView]     = useState("home");          // home | plans
  const [section, setSection] = useState("overview");    // overview | invoices | payment | contacts | tax | audit

  // Network state
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState(null);
  const [plans, setPlans]     = useState([]);
  const [invoices, setInvoices] = useState([]);
  const [contacts, setContacts] = useState([]);
  const [taxProfile, setTaxProfile] = useState(null);
  const [paymentMethods, setPaymentMethods] = useState([]);
  const [audit, setAudit] = useState([]);
  // Entitlements — feature flags + limits for the current org. Pulled
  // from the commercial-layer /api/v1/entitlements/current endpoint so
  // the Entitlements tab shows the live capability profile (seat caps,
  // API quota, retention, feature toggles like SSO/SCIM/API access).
  const [entitlements, setEntitlements] = useState(null);
  // Audit-tab filters (search + event-type chip + date range).
  const [auditQuery, setAuditQuery] = useState({ search: "", action: "", days: 30 });

  // Plan comparison view state
  const [billingCycle, setBillingCycle] = useState("monthly");

  // Modal state
  const [confirmUpgrade, setConfirmUpgrade] = useState(null);   // plan
  const [downgradeFor,   setDowngradeFor]   = useState(null);   // plan
  const [enterpriseOpen, setEnterpriseOpen] = useState(false);
  const [contactModal,   setContactModal]   = useState(null);   // {} or row

  // Filters
  const [invoiceFilter, setInvoiceFilter] = useState({ status: "", search: "" });

  // ─── Data loaders ───────────────────────────────────────────────────────
  const loadSummary = useCallback(async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/summary`, { headers: authHeaders() });
    if (r.ok) setSummary(await r.json());
  }, []);

  const loadPlans = useCallback(async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/plans`, { headers: authHeaders() });
    if (r.ok) {
      const d = await r.json();
      setPlans(d.plans || []);
    }
  }, []);

  const loadInvoices = useCallback(async () => {
    const params = new URLSearchParams();
    if (invoiceFilter.status) params.append("status", invoiceFilter.status);
    if (invoiceFilter.search) params.append("search", invoiceFilter.search);
    const r = await fetch(`${API_BASE}/api/admin/billing/invoices?${params}`, { headers: authHeaders() });
    if (r.ok) {
      const d = await r.json();
      setInvoices(d.invoices || []);
    }
  }, [invoiceFilter]);

  const loadContacts = useCallback(async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/contacts`, { headers: authHeaders() });
    if (r.ok) setContacts((await r.json()).contacts || []);
  }, []);

  const loadTax = useCallback(async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/tax-profile`, { headers: authHeaders() });
    if (r.ok) setTaxProfile((await r.json()).profile || null);
  }, []);

  const loadPayments = useCallback(async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/payment-method`, { headers: authHeaders() });
    if (r.ok) setPaymentMethods((await r.json()).payment_methods || []);
  }, []);

  const loadAudit = useCallback(async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/audit`, { headers: authHeaders() });
    if (r.ok) setAudit((await r.json()).events || []);
  }, []);

  // Entitlements come from the commercial layer, not the legacy billing
  // admin namespace. Non-fatal: an org without a commercial subscription
  // record (legacy single-tenant) just gets a null payload and the tab
  // renders a friendly empty state instead of erroring.
  const loadEntitlements = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/v1/entitlements/current`, { headers: authHeaders() });
      if (r.ok) setEntitlements(await r.json());
      else      setEntitlements(null);
    } catch { setEntitlements(null); }
  }, []);

  // Only what the two visible tabs need. This dropped four requests per page load
  // (contacts, tax-profile, audit, entitlements) that fed tabs no longer shown.
  //
  // Dropping loadTax also removes the "Complete tax details for accurate invoicing"
  // warning at source: prices are final and tax-inclusive, so there are no tax
  // details to complete and prompting for them was misleading.
  // The loader functions and their endpoints are left in place, unused.
  const refreshAll = useCallback(async () => {
    setLoading(true);
    await Promise.all([
      loadSummary(), loadPlans(), loadInvoices(), loadPayments(),
    ]);
    setLoading(false);
  }, [loadSummary, loadPlans, loadInvoices, loadPayments]);

  // Load everything once per mount. `refreshAll`'s identity changes whenever
  // `invoiceFilter` does (loadInvoices depends on it), so keying the effect on
  // [refreshAll] used to refetch all eight endpoints on every keystroke in the
  // invoice search box — and flip `loading`, unmounting the input mid-type.
  // The ref guard survives StrictMode's deliberate double-invoke in dev.
  // Hidden sections (billing profile, entitlements, audit) are not in SECTIONS, so
  // their data is no longer fetched on mount. Fetch it only if one is opened — that
  // keeps them re-enableable without slowing the two tabs customers actually see.
  useEffect(() => {
    if (section === "profile") { loadContacts(); loadTax(); }
    if (section === "entitlements") loadEntitlements();
    if (section === "audit") loadAudit();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);

  const didInitialLoad = useRef(false);
  useEffect(() => {
    if (didInitialLoad.current) return;
    didInitialLoad.current = true;
    refreshAll();
  }, [refreshAll]);

  // Invoice filtering now reloads only the invoice list, debounced. Compared
  // against the last fetched filter so StrictMode's re-run is a no-op.
  const lastInvoiceFilter = useRef(null);
  useEffect(() => {
    const key = JSON.stringify(invoiceFilter);
    if (lastInvoiceFilter.current === null) {
      lastInvoiceFilter.current = key;   // initial value — refreshAll covers it
      return;
    }
    if (lastInvoiceFilter.current === key) return;
    lastInvoiceFilter.current = key;
    const t = setTimeout(() => { loadInvoices(); }, 300);
    return () => clearTimeout(t);
  }, [invoiceFilter, loadInvoices]);

  // After Stripe Checkout redirects back, confirm with backend then show toast
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const sessionId = params.get("session_id");
    const setupIntent = params.get("setup_intent");
    if (!sessionId && !setupIntent) return;

    params.delete("session_id");
    params.delete("setup_intent");
    const clean = [window.location.pathname, params.toString()].filter(Boolean).join("?");
    window.history.replaceState({}, "", clean);

    if (sessionId) {
      // fetch() resolves for 4xx/5xx too, so success must be gated on r.ok.
      // Claiming "Payment successful" when the backend rejected the session (or
      // when the network failed) tells the customer they were charged and
      // upgraded when neither may be true. refreshAll() runs either way so the
      // page always settles on the real server-side state.
      fetch(`${API_BASE}/api/admin/billing/upgrade/confirm?session_id=${sessionId}`, {
        headers: authHeaders(),
      })
        .then(async (r) => {
          if (r.ok) {
            showToast(tr("billingCheckoutSuccess", "Payment successful! Your plan has been updated."), "success");
            // Payment confirmed by the server → refresh entitlements so the new
            // plan's features appear at once. Inside the r.ok branch only: a
            // failed or still-pending confirmation must not move the UI forward.
            refreshEntitlements();
          } else {
            const err = await r.json().catch(() => ({}));
            showToast(
              err.detail ||
                tr("billingCheckoutFailed", "We could not confirm your payment. Your plan has not been changed."),
              "error",
            );
          }
          refreshAll();
        })
        .catch(() => {
          showToast(
            tr("billingCheckoutUnconfirmed", "We could not reach the server to confirm your payment. Please refresh, and contact support if your plan looks wrong."),
            "error",
          );
          refreshAll();
        });
    } else {
      // Returning from Stripe's hosted Setup Session. This previously showed
      // "Payment method saved successfully." with no backend call at all — a claim
      // nothing had verified and nothing had stored. Now it confirms with the server,
      // which checks the session belongs to this tenant and mirrors the real card.
      fetch(`${API_BASE}/api/admin/billing/payment-method/confirm?setup_intent=${encodeURIComponent(setupIntent)}`, {
        headers: authHeaders(),
      })
        .then(async (r) => {
          if (r.ok) {
            showToast(tr("billingSetupSuccess", "Payment method saved successfully."), "success");
          } else {
            const err = await r.json().catch(() => ({}));
            showToast(
              err.detail || tr("billingSetupFailed", "We could not confirm your card. It has not been saved."),
              "error",
            );
          }
          refreshAll();
        })
        .catch(() => {
          showToast(
            tr("billingSetupUnconfirmed", "We could not reach the server to confirm your card. Please refresh."),
            "error",
          );
          refreshAll();
        });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ─── Derived ────────────────────────────────────────────────────────────
  // Temveris's owner runs the product it sells: full plan entitlements, never
  // charged. Purchase and portal actions are hidden rather than shown and then
  // rejected — the backend returns 403 on all of them, so offering the buttons
  // would only produce dead ends. The backend guard is what makes it safe; this
  // is presentation only.
  const isInternalTenant = summary?.is_internal_tenant === true;
  const currentPlan = summary?.subscription?.plan;
  const subStatus = summary?.subscription?.subscription_status || "active";
  // Fall back to a neutral pill, never to "Active" — an unrecognised status is
  // unknown, and painting it green claims a healthy subscription we can't vouch
  // for (a cancelled or unpaid one would have read "Active").
  const subStatusTint =
    STATUS[subStatus] || { color: C.textSub, bg: C.borderLight, border: C.border, label: titleCase(subStatus) };

  // Derived helpers for the Figma top section ───────────────────────────────
  const isTrial       = subStatus === "trial" || subStatus === "trial_active";
  const trialExpired  = subStatus === "trial_expired";

  const daysUntil = (iso) => {
    if (!iso) return null;
    // Date.now() during render is intentional here — these counters are
    // day-resolution and only need to be stale-correct between renders.
    // eslint-disable-next-line react-hooks/purity
    const ms = new Date(iso).getTime() - Date.now();
    return Math.max(0, Math.ceil(ms / 86400000));
  };
  const renewalDate = summary?.subscription?.renewal_date || null;

  // `trial_end_date` is the authoritative trial end — it is what the backend stores and
  // what the reminder emails quote. This previously read `trial_ends_at`, a key this API
  // has never emitted (it belongs to the separate commercial schema), so it was always
  // undefined and silently fell back to renewal_date. On a trial row renewal_date used
  // to be start + 30 days, so the banner told every customer they had ~30 days left of
  // a 14-day trial. Both halves of that are fixed; the fallback is kept only for rows
  // created before this change.
  const trialEndsAt = summary?.subscription?.trial_end_date || renewalDate;
  const daysToRenewal = daysUntil(renewalDate);
  // Prefer the server's own count so the page and the emails can never disagree by a
  // rounding rule; fall back to computing it locally.
  const daysToTrialEnd =
    summary?.subscription?.trial_days_remaining ?? daysUntil(trialEndsAt);
  // Matches TRIAL_WARNING_DAYS in app/billing/router.py and the day-11 reminder email,
  // so the banner turns amber on the same day the customer is emailed about it.
  const trialEndingSoon = isTrial && daysToTrialEnd !== null && daysToTrialEnd <= 3;
  // A card can only attach to an existing Stripe customer, and that customer is created
  // by the first plan purchase. On a trial with no plan chosen there is nothing to
  // attach to, so offering "Add Payment Method" here just produced a 409 telling the
  // customer to choose a plan — a dead end reached from the banner that told them to do
  // it. When there is no billing account the banner sends them to the plans instead.
  const hasBillingAccount = summary?.subscription?.has_billing_account === true;

  // Audit filter — applied client-side so the user can scrub through
  // event types without re-fetching the whole list each keystroke.
  const visibleAudit = useMemo(() => {
    const q = (auditQuery.search || "").trim().toLowerCase();
    // Date.now() inside a useMemo is acceptable here — the filter recomputes
    // only when audit / auditQuery changes, and the cutoff is day-resolution.
    // eslint-disable-next-line react-hooks/purity
    const nowMs = Date.now();
    const cutoffMs = auditQuery.days ? nowMs - auditQuery.days * 86400000 : 0;
    return (audit || []).filter(a => {
      if (auditQuery.action && a.action_key !== auditQuery.action) return false;
      if (cutoffMs && a.created_at && new Date(a.created_at).getTime() < cutoffMs) return false;
      if (!q) return true;
      const hay = `${a.actor_name || ""} ${a.actor_email || ""} ${a.summary || ""} ${a.action_key || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [audit, auditQuery]);

  // Distinct action types (for the "All event types" dropdown).
  const auditActionTypes = useMemo(
    () => Array.from(new Set((audit || []).map(a => a.action_key).filter(Boolean))),
    [audit]
  );

  // Action key → coloured dot (left rail of the timeline). Palette matches
  // the Figma colour semantics so admins can scan the feed at a glance:
  //   • RED    — failures (payment failed, declined, error)
  //   • AMBER  — mutating actions that need attention (seat changes, retries)
  //   • PURPLE — read / export actions (downloads, signed-URL generation)
  //   • GREEN  — successful money / lifecycle events (paid, renewed, added pm)
  //   • BRAND  — neutral lifecycle / profile changes (created, updated)
  const auditDotColor = (a) => {
    const k = (a?.action_key || "").toLowerCase();
    if (k.includes("fail") || k.includes("error") || k.includes("declined"))                  return C.red;
    if (k.includes("seats") || k.includes("seat_") || k.includes("retry") || k.includes("dunning") || k.includes("warn")) return C.amber;
    if (k.includes("download") || k.includes("export") || k.includes("viewed") || k.includes("signed_url")) return "#7C3AED";
    if (k.includes("payment_method_added") || k.includes("payment_added") || k.includes("paid")
        || k.includes("renew") || k.includes("processed") || k.includes("succeeded"))         return C.green;
    return C.brand;
  };

  // Bullet description used in the timeline footer. The backend returns
  // an unstructured `details_json` blob; we fold every key the Figma
  // spec calls out (provider, correlation_id, webhook, token, old/new
  // value, signed-url expiry, MFA, retry, payment failure reason, IP)
  // into one human-readable bullet line in a deterministic order.
  const auditDetailsLine = (a) => {
    if (!a) return "";
    const d = a.details_json || a.details || {};
    const bits = [];
    if (d.source)               bits.push(`Source: ${d.source}`);
    if (d.provider)             bits.push(`Provider: ${d.provider}`);
    if (d.webhook)              bits.push(`Webhook: ${d.webhook}`);
    if (d.correlation_id)       bits.push(`Correlation ID: ${d.correlation_id}`);
    if (d.token)                bits.push(`Token: ${d.token}`);
    if (d.invoice_number)       bits.push(d.invoice_number);
    if (d.old_value !== undefined && d.new_value !== undefined) {
      bits.push(`Old value: ${d.old_value}`);
      bits.push(`New value: ${d.new_value}`);
    }
    if (d.old_seats !== undefined && d.new_seats !== undefined) {
      bits.push(`Old seats: ${d.old_seats}`);
      bits.push(`New seats: ${d.new_seats}`);
    }
    if (d.signed_url_expires_minutes)
      bits.push(`Expires in ${d.signed_url_expires_minutes} minutes`);
    if (d.provider_error)       bits.push(`Provider error: ${d.provider_error}`);
    if (d.retry_attempt && d.retry_max) bits.push(`Retry attempt ${d.retry_attempt} of ${d.retry_max}`);
    if (d.grace_period_initiated) bits.push("Grace period initiated");
    if (d.mfa_confirmed)        bits.push("MFA confirmed");
    if (d.mfa_step_up_completed) bits.push("MFA step-up completed");
    if (d.entitlements_refreshed) bits.push("Entitlements refreshed");
    if (d.entitlements_recomputed) bits.push("Entitlements recomputed");
    if (d.seat_ledger_updated)  bits.push("Seat ledger updated");
    if (d.audit_event_recorded) bits.push("Audit event recorded");
    if (a.ip_address)           bits.push(`IP: ${a.ip_address}`);
    return bits.join(" · ");
  };

  // ═══════════════════════════════════════════════════════════════════════
  //   ACTIONS
  // ═══════════════════════════════════════════════════════════════════════
  const doUpgrade = async (plan) => {
    // Single choke point: "Change plan" / "Add seats" / plan cards all land here,
    // so gating the handler covers every entry point rather than relying on each
    // button being individually hidden.
    if (isInternalTenant) {
      showToast(tr("billingInternalTenant",
        "This is an internal Temveris organization and is not billed. Plan changes do not apply."),
        "error");
      return;
    }
    const r = await fetch(`${API_BASE}/api/admin/billing/upgrade`, {
      method: "POST", headers: authHeaders(),
      body: JSON.stringify({ plan_code: plan.code, billing_cycle: billingCycle }),
    });
    if (r.ok) {
      const d = await r.json();
      if (d.url) {
        window.location.href = d.url;
        return;
      }
      showToast(d.message || tr("billingPlanUpdated", "Plan updated"), "success");
      setConfirmUpgrade(null);
      setView("home");
      await refreshAll();
      // Server confirmed the in-place plan change → pull the fresh entitlement
      // projection so newly-granted features (e.g. Payroll Calculator) unlock
      // immediately, without a logout or manual refresh. Only reached on success.
      refreshEntitlements();
    } else {
      const err = await r.json().catch(() => ({}));
      showToast(err.detail || tr("billingUpgradeFailed", "Couldn't update plan"), "error");
    }
  };

  const doSubmitDowngrade = async (form) => {
    if (!form.reason || form.reason.length < 20) {
      showToast(tr("billingDowngradeReasonShort", "Please give at least 20 characters of context."), "error");
      return;
    }
    if (!form.acknowledged) {
      showToast(tr("billingDowngradeAck", "Please acknowledge the impact before submitting."), "error");
      return;
    }
    const r = await fetch(`${API_BASE}/api/admin/billing/downgrade-request`, {
      method: "POST", headers: authHeaders(),
      body: JSON.stringify({
        requested_plan_code: downgradeFor.code,
        reason: form.reason,
        preferred_effective_date: form.effective || null,
        acknowledged_risk: !!form.acknowledged,
      }),
    });
    if (r.ok) {
      const d = await r.json();
      showToast(d.message || tr("billingDowngradeReceived", "Request received."), "success");
      setDowngradeFor(null);
      await refreshAll();
    } else {
      const err = await r.json().catch(() => ({}));
      showToast(err.detail || tr("billingDowngradeFailed", "Couldn't submit request"), "error");
    }
  };

  const doSubmitEnterprise = async (form) => {
    const r = await fetch(`${API_BASE}/api/admin/billing/enterprise-quote`, {
      method: "POST", headers: authHeaders(),
      body: JSON.stringify(form),
    });
    if (r.ok) {
      const d = await r.json();
      showToast(d.message || tr("billingEnterpriseSubmitted", "Quote request submitted."), "success");
      setEnterpriseOpen(false);
      await refreshAll();
    } else {
      const err = await r.json().catch(() => ({}));
      showToast(err.detail || tr("billingEnterpriseFailed", "Couldn't submit quote request"), "error");
    }
  };

  const openCustomerPortal = async () => {
    // Same reasoning as doUpgrade — five buttons reach this handler. The portal is
    // where plan changes and cancellation happen, so an internal tenant must not
    // open it even though it may still hold a stripe_customer_id.
    if (isInternalTenant) {
      showToast(tr("billingInternalTenant",
        "This is an internal Temveris organization and is not billed. There is no billing portal for it."),
        "error");
      return;
    }
    const r = await fetch(`${API_BASE}/api/admin/billing/customer-portal`, {
      method: "POST", headers: authHeaders(),
    });
    if (r.ok) {
      const d = await r.json();
      if (d.url) window.location.href = d.url;
    } else {
      const err = await r.json().catch(() => ({}));
      showToast(err.detail || tr("billingPortalFailed", "Couldn't open billing portal"), "error");
    }
  };

  const downloadInvoice = async (inv) => {
    // The endpoint now returns Stripe's hosted PDF URL rather than a generated
    // document. Opened in a new tab instead of fetched: Stripe serves no CORS headers
    // for invoice PDFs, and following a redirect would forward our Authorization
    // header to them.
    const r = await fetch(`${API_BASE}/api/admin/billing/invoices/${inv.id}/pdf`, { headers: authHeaders() });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(err.detail || tr("billingDownloadFailed", "Couldn't download invoice"), "error");
      return;
    }
    const d = await r.json().catch(() => ({}));
    if (d.url) window.open(d.url, "_blank", "noopener,noreferrer");
    else showToast(tr("billingDownloadFailed", "Couldn't download invoice"), "error");
  };

  // payInvoice() was removed with the /invoices/{id}/pay endpoint (now 410 Gone).
  // It marked invoices paid without taking any payment. Real collection will go
  // through a Stripe-hosted payment flow; until then unpaid invoices are settled
  // via the Stripe billing portal.

  const downloadStatement = async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/statement`, { headers: authHeaders() });
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "billing-statement.csv"; a.click(); URL.revokeObjectURL(url);
    refreshAll();
  };

  const exportAuditCsv = async () => {
    const r = await fetch(`${API_BASE}/api/admin/billing/audit/export`, { headers: authHeaders() });
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "billing-audit.csv"; a.click(); URL.revokeObjectURL(url);
  };

  const submitContact = async (form) => {
    const isUpdate = !!contactModal?.id;
    const url = isUpdate
      ? `${API_BASE}/api/admin/billing/contacts/${contactModal.id}`
      : `${API_BASE}/api/admin/billing/contacts`;
    const r = await fetch(url, {
      method: isUpdate ? "PATCH" : "POST",
      headers: authHeaders(),
      body: JSON.stringify(form),
    });
    if (r.ok) { showToast(tr("billingContactSaved", "Contact saved"), "success"); setContactModal(null); refreshAll(); }
    else      { showToast(tr("billingContactFailed", "Couldn't save contact"), "error"); }
  };

  const deleteContact = async (id) => {
    if (!window.confirm(tr("billingContactConfirmDelete", "Remove this billing contact?"))) return;
    const r = await fetch(`${API_BASE}/api/admin/billing/contacts/${id}`, {
      method: "DELETE", headers: authHeaders(),
    });
    if (r.ok) { showToast(tr("billingContactRemoved", "Contact removed"), "success"); refreshAll(); }
    else {
      const err = await r.json().catch(() => ({}));
      showToast(err.detail || tr("billingContactDeleteFailed", "Couldn't remove contact"), "error");
    }
  };

  const submitTax = async (form) => {
    const r = await fetch(`${API_BASE}/api/admin/billing/tax-profile`, {
      method: "PATCH", headers: authHeaders(), body: JSON.stringify(form),
    });
    if (r.ok) {
      showToast(tr("billingTaxSaved", "Tax details saved"), "success");
      // Reload only the tax-profile + audit log instead of running the full
      // refresh. refreshAll() flips `loading` to true, which unmounts the
      // TaxProfileForm and wipes the local form state — making the page
      // feel like the save didn't stick.
      loadTax();
      loadAudit();
    } else {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (_e) {}
      showToast(detail || tr("billingTaxFailed", "Couldn't save tax details"), "error");
    }
  };

  // Cards are entered on Stripe's hosted page — the number and CVC never reach
  // Temveris. This replaced a form where the admin typed a brand, four digits and an
  // expiry, producing a record Stripe could not charge while the UI claimed it was
  // tokenized. POST /payment-method is now 410 Gone.
  const startCardSetup = async () => {
    if (isInternalTenant) {
      showToast(tr("billingInternalTenant",
        "This is an internal Temveris organization and is not billed."), "error");
      return;
    }
    const r = await fetch(`${API_BASE}/api/admin/billing/payment-method/session`, {
      method: "POST", headers: authHeaders(),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.session_url) {
      window.location.href = d.session_url;      // Stripe collects the card
      return;
    }
    showToast(d.detail || tr("billingPaymentSetupFailed",
      "Couldn't start secure card setup. No card has been saved."), "error");
  };

  const removePaymentMethod = async (id) => {
    if (!window.confirm(tr("billingPaymentConfirmDelete", "Remove this payment method?"))) return;
    const r = await fetch(`${API_BASE}/api/admin/billing/payment-method/${id}`, {
      method: "DELETE", headers: authHeaders(),
    });
    if (r.ok) { showToast(tr("billingPaymentRemoved", "Payment method removed"), "success"); refreshAll(); }
    else      { showToast(tr("billingPaymentDeleteFailed", "Couldn't remove payment method"), "error"); }
  };

  // ═══════════════════════════════════════════════════════════════════════
  //   RENDER PIECES
  // ═══════════════════════════════════════════════════════════════════════
  const ALERT_DEFS = (a) => {
    const map = {
      seat_approaching_limit: { tier: "amber", title: tr("billingAlertSeatApproaching", "Approaching plan limit"), cta: tr("billingActionUpgradePlan", "Upgrade Plan") },
      seat_limit_reached:     { tier: "amber", title: tr("billingAlertSeatLimitReached", "User limit reached"),     cta: tr("billingActionUpgradePlan", "Upgrade Plan") },
      seat_over_capacity:     { tier: "red",   title: tr("billingAlertSeatOver", "Active users exceed plan limit"), cta: tr("billingActionUpgradeRequired", "Upgrade Required") },
      invites_exceed_seats:   { tier: "amber", title: tr("billingAlertInvitesExceed", "Pending invites would exceed your plan limit"), cta: tr("billingActionReviewInvites", "Review Invites") },
      payment_failed:         { tier: "red",   title: tr("billingAlertPaymentFailed", "Latest payment could not be processed"), cta: tr("billingActionUpdatePayment", "Update Payment") },
      trial_ending:           { tier: "amber", title: tr("billingAlertTrialEnding", "Your free trial ends soon"), cta: tr("billingActionChoosePlan", "Choose Plan") },
      // Raised once the 14-day trial has passed. Deliberately says what did NOT happen —
      // that is the question a customer has at this moment.
      trial_expired:          { tier: "red",   title: tr("billingAlertTrialExpired", "Your free trial has ended — you have not been charged"), cta: tr("billingActionChoosePlan", "Choose Plan") },
      tax_details_incomplete: { tier: "amber", title: tr("billingAlertTaxIncomplete", "Complete tax details for accurate invoicing"), cta: tr("billingActionUpdateTax", "Update Tax Details") },
    };
    return map[a.key] || { tier: a.tier || "amber", title: titleCase(a.key), cta: tr("billingActionResolve", "Resolve") };
  };

  // Scroll the billing module's content area back to the top so the user
  // visibly lands on the section the CTA jumped to. Without this the page
  // can stay scrolled mid-way and the click feels like a no-op.
  const scrollBillingToTop = () => {
    try {
      // The dashboard's main scroller is the closest "[overflowY: auto]"
      // ancestor; window.scrollTo handles the fallback case where the
      // module is the page-level scroller.
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch {}
  };

  const handleAlertCta = (key) => {
    if (key === "tax_details_incomplete") {
      setView("home");
      setSection("tax");
      scrollBillingToTop();
    } else if (key === "payment_failed") {
      setView("home");
      setSection("payment");
      scrollBillingToTop();
    } else if (key === "trial_ending" || key === "trial_expired" || key.startsWith("seat_")) {
      setView("plans");
      scrollBillingToTop();
    } else if (key === "invites_exceed_seats") {
      // SPA navigation — avoids a hard reload + state loss when the admin
      // clicks "Review Invites" from the billing module.
      navigate("/admin?tab=myteam");
    } else {
      // Unknown / future alert keys — surface a toast instead of silently
      // swallowing the click.
      showToast(tr("billingActionUnsupported", "This alert needs to be resolved from your admin console."), "info");
    }
  };

  // ─── Top tab switcher ───────────────────────────────────────────────────
  // Two tabs, not five. A customer needs to answer two questions — "what am I paying?"
  // and "where are my invoices?" — and nothing else on this page helps with either.
  //
  // Billing profile, Entitlements and Billing audit trail are hidden rather than
  // deleted: their render blocks and endpoints are untouched, so an internal tool can
  // surface them later without rebuilding anything. Entitlements in particular reads
  // the commercial capability profile, which reports the wrong plan for a paying
  // customer — showing it did active harm.
  // Alert keys the customer should never be shown.
  //   tax_details_incomplete — no tax is charged, so there is nothing to complete.
  const SUPPRESSED_ALERTS = ["tax_details_incomplete"];

  const SECTIONS = [
    { k: "overview", l: tr("billingSecPlanOverview", "Plan & usage"),        icon: "card" },
    { k: "invoices", l: tr("billingSecInvoicesPay",  "Invoices & payments"), icon: "receipt" },
  ];

  // Derived primary for default value
  const primaryContact = contacts.find(c => c.is_primary) || contacts[0] || null;

  // ═══════════════════════════════════════════════════════════════════════
  //   PLANS COMPARISON VIEW
  // ═══════════════════════════════════════════════════════════════════════
  if (view === "plans") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 18, animation: "billingFade .25s ease both" }}>
        <BillingStyles C={C} isDark={isDark} />

        <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 14 }}>
          <div>
            <button onClick={() => setView("home")} style={btnGhost(C)}>
              <BIcon n="arrow_up" s={13} c={C.brand} strokeWidth={2.4} />
              <span style={{ transform: "rotate(-90deg)", display: "inline-block" }}>›</span>
              {tr("billingBackToBilling", "Back to Billing")}
            </button>
            <h2 style={{ fontSize: 24, fontWeight: 800, color: C.text, marginTop: 10 }}>
              {tr("billingChoosePlan", "Choose your plan")}
            </h2>
            <p style={{ fontSize: 13.5, color: C.textSub, marginTop: 4, maxWidth: 720 }}>
              {tr("billingChoosePlanDesc",
                "All prices are per user. Switch between monthly and annual to view the dynamic discount, then upgrade instantly or request a downgrade for review.")}
            </p>
          </div>

          <div style={{ display: "inline-flex", padding: 4, background: C.bg, borderRadius: 999, border: `1px solid ${C.border}` }}>
            {[
              { k: "monthly", l: tr("billingMonthly", "Monthly") },
              { k: "annual",  l: tr("billingAnnual",  "Annual · Save up to 20%") },
            ].map(o => (
              <button key={o.k}
                onClick={() => setBillingCycle(o.k)}
                style={{
                  padding: "8px 18px", borderRadius: 999, border: "none",
                  background: billingCycle === o.k ? C.surface : "transparent",
                  color: billingCycle === o.k ? C.brand : C.textSub,
                  fontWeight: billingCycle === o.k ? 700 : 600, fontSize: 12.5,
                  cursor: "pointer", boxShadow: billingCycle === o.k ? "0 1px 4px rgba(16,24,40,.08)" : "none",
                  transition: "all .18s",
                }}>
                {o.l}
              </button>
            ))}
          </div>
        </header>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 16 }}>
          {plans.map(p => <PlanCard key={p.code}
                            C={C} plan={p} cycle={billingCycle}
                            currentPlan={currentPlan}
                            onUpgrade={() => setConfirmUpgrade(p)}
                            onDowngrade={() => setDowngradeFor(p)}
                            onEnterprise={() => setEnterpriseOpen(true)}
                            tr={tr} />)}
        </div>

        <p style={{ fontSize: 12, color: C.textLight, textAlign: "center", marginTop: 6 }}>
          {tr("billingTaxCopy", "Prices shown are final. No additional tax is applied.")}
        </p>

        {confirmUpgrade && (
          <UpgradeConfirmModal
            C={C} plan={confirmUpgrade} cycle={billingCycle} subscription={summary?.subscription}
            onClose={() => setConfirmUpgrade(null)}
            onConfirm={() => doUpgrade(confirmUpgrade)}
            tr={tr}
          />
        )}
        {downgradeFor && (
          <DowngradeReviewModal
            C={C} plan={downgradeFor} onClose={() => setDowngradeFor(null)}
            onSubmit={doSubmitDowngrade} tr={tr}
          />
        )}
        {enterpriseOpen && (
          <EnterpriseQuoteModal C={C} onClose={() => setEnterpriseOpen(false)} onSubmit={doSubmitEnterprise} tr={tr} />
        )}

        <Toast C={C} toast={toast} />
      </div>
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  //   HOME — overview / invoices / payment / contacts / tax / audit
  // ═══════════════════════════════════════════════════════════════════════
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18, animation: "billingFade .25s ease both" }}>
      <BillingStyles C={C} isDark={isDark} />

      {/* ─── HEADER (Figma) ───────────────────────────────────────────────── */}
      <header style={{
        display: "flex", justifyContent: "space-between", alignItems: "flex-start",
        flexWrap: "wrap", gap: 14, paddingBottom: 6,
      }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
          <div aria-hidden style={{
            width: 8, alignSelf: "stretch", borderRadius: 4,
            background: `linear-gradient(180deg, ${C.brand} 0%, ${C.green} 100%)`,
            minHeight: 60, opacity: 0.18,
          }} />
          <div>
            <h2 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0 }}>
              {tr("billingTitle", "Billing & Subscription")}
            </h2>
            <p style={{ fontSize: 13, color: C.textSub, maxWidth: 640, marginTop: 4 }}>
              {tr("billingSubheading", "Manage your workspace plan, trial status, seats, invoices, payment method, and billing contacts.")}
            </p>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button onClick={() => setSection("audit")} style={btnGhost(C)} title={tr("billingViewAuditLog", "View Audit Log")}>
            <BIcon n="history" s={13} c={C.brand} strokeWidth={2.4} />
            {tr("billingViewAuditLog", "View Audit Log")}
          </button>
          <button onClick={() => setEnterpriseOpen(true)} style={btnGhost(C)}>
            <BIcon n="briefcase" s={13} c={C.brand} strokeWidth={2.4} />
            {tr("billingContactSales", "Contact Sales")}
          </button>
          <button onClick={() => setView("plans")} style={btnPrimary(C)}>
            <BIcon n="arrow_up" s={13} c="#fff" strokeWidth={2.6} />
            {tr("billingUpgradePlan", "Upgrade Plan")}
          </button>
        </div>
      </header>

      {/* ─── TRIAL BANNER ─────────────────────────────────────────────────────
          Turns amber inside the last few days so "ends soon" is visible rather than
          buried in a green banner that looked identical on day 1 and day 13. The exact
          end date is always shown: a bare day count leaves the customer working out
          which date that is, and they get it wrong. */}
      {isTrial && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 14, flexWrap: "wrap",
          padding: "14px 18px", borderRadius: 10,
          background: trialEndingSoon ? C.amberBg : C.greenBg,
          border: `1px solid ${trialEndingSoon ? C.amberBdr : C.greenBdr}`,
        }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <p style={{ margin: 0, fontSize: 13.5, fontWeight: 700,
                        color: trialEndingSoon ? C.amber : C.green }}>
              {daysToTrialEnd !== null
                ? `${tr("billingFreeTrialPrefix", "Your workspace is on a free trial")} — ${daysToTrialEnd} ${daysToTrialEnd === 1 ? tr("billingDay", "day") : tr("billingDays", "days")} ${tr("billingRemaining", "remaining")}`
                : tr("billingFreeTrial", "Your workspace is on a free trial")}
            </p>
            <p style={{ margin: "4px 0 0", fontSize: 12.5,
                        color: trialEndingSoon ? C.amber : C.green, opacity: 0.85 }}>
              {(() => {
                // Two different instructions, because two different things are possible.
                // Telling a workspace with no billing account to "add a payment method"
                // sends them at an endpoint that can only refuse them.
                const action = hasBillingAccount
                  ? tr("billingTrialBannerSub",
                       "Add a payment method to continue service after the trial ends. No charges until trial expires.")
                  : tr("billingTrialBannerChoosePlan",
                       "Choose a plan to continue after your trial ends. Nothing is charged until you do.");
                return trialEndsAt
                  ? `${tr("billingTrialEnds", "Trial ends")} ${fmtDate(trialEndsAt)}. ${action}`
                  : action;
              })()}
            </p>
          </div>
          {hasBillingAccount ? (
            <button onClick={() => startCardSetup()} style={btnPrimary(C)}>
              <BIcon n="plus" s={13} c="#fff" strokeWidth={2.6} />
              {tr("billingAddPaymentMethod", "Add Payment Method")}
            </button>
          ) : (
            <button onClick={() => { setView("plans"); scrollBillingToTop(); }}
                    style={btnPrimary(C)}>
              <BIcon n="arrow_up" s={13} c="#fff" strokeWidth={2.6} />
              {tr("billingActionChoosePlan", "Choose Plan")}
            </button>
          )}
        </div>
      )}

      {/* ─── TRIAL ENDED BANNER ───────────────────────────────────────────────
          The trial ran out with no plan chosen. States the two things a customer
          actually wants to know — nothing was charged, nothing was deleted. */}
      {trialExpired && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 14, flexWrap: "wrap",
          padding: "14px 18px", borderRadius: 10,
          background: C.amberBg, border: `1px solid ${C.amberBdr}`,
        }}>
          <div style={{ flex: 1, minWidth: 240 }}>
            <p style={{ margin: 0, fontSize: 13.5, fontWeight: 700, color: C.amber }}>
              {trialEndsAt
                ? `${tr("billingTrialEndedOn", "Your free trial ended")} ${fmtDate(trialEndsAt)}`
                : tr("billingTrialEnded", "Your free trial has ended")}
            </p>
            <p style={{ margin: "4px 0 0", fontSize: 12.5, color: C.amber, opacity: 0.85 }}>
              {tr("billingTrialEndedSub", "You have not been charged and nothing has been deleted. Choose a plan to continue.")}
            </p>
          </div>
          <button onClick={() => setView("plans")} style={btnPrimary(C)}>
            <BIcon n="arrow_up" s={13} c="#fff" strokeWidth={2.6} />
            {tr("billingActionChoosePlan", "Choose Plan")}
          </button>
        </div>
      )}

      {/* ─── 4 STATUS CARDS (Figma) ───────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
        {/* CURRENT PLAN */}
        <div style={{ padding: "16px 18px", borderRadius: 12, background: C.surface, border: `1px solid ${C.border}` }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            {tr("billingCardCurrentPlan", "Current Plan")}
          </p>
          <p style={{ fontSize: 22, fontWeight: 800, color: C.text, marginTop: 6, lineHeight: 1.1 }}>
            {currentPlan?.name || "—"}
          </p>
          <p style={{ fontSize: 12, color: C.textSub, marginTop: 4 }}>
            {currentPlan
              ? `${titleCase(summary?.subscription?.billing_cycle || "monthly")} · ${
                  summary?.subscription?.billing_cycle === "annual" && currentPlan.annual_price_per_user
                    ? `${fmtMoney((currentPlan.annual_price_per_user / 12).toFixed(2), currentPlan.currency)}/employee/mo`
                    : currentPlan.monthly_price_per_user
                      ? `${fmtMoney(currentPlan.monthly_price_per_user.toFixed(2), currentPlan.currency)}/employee/mo`
                      : ""
                }`
              : tr("billingNoPlan", "No plan")}
          </p>
        </div>

        {/* ACTIVE EMPLOYEES — what the bill is actually based on.
            Was "Seat Usage 8 / 100", which read as "you bought 100 seats". Nobody buys
            seats in Temveris: the bill is the number of active employees. The plan
            limit is a ceiling, not a purchase, so it is not shown next to the count.
            Pending invites are not billed either, so they are not shown here. */}
        <div style={{ padding: "16px 18px", borderRadius: 12, background: C.surface, border: `1px solid ${C.border}` }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            {tr("billingCardActiveEmployees", "Active Employees")}
          </p>
          <p style={{ fontSize: 22, fontWeight: 800, color: C.text, marginTop: 6, lineHeight: 1.1, fontVariantNumeric: "tabular-nums" }}>
            {summary?.seat_metrics?.active ?? 0}
          </p>
          <p style={{ fontSize: 12, color: C.textSub, marginTop: 4 }}>
            {tr("billingCardActiveEmployeesSub", "you're billed for these")}
          </p>
        </div>

        {/* RENEWAL DATE */}
        <div style={{ padding: "16px 18px", borderRadius: 12, background: C.surface, border: `1px solid ${C.border}` }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            {tr("billingCardRenewal", "Renewal Date")}
          </p>
          <p style={{ fontSize: 22, fontWeight: 800, color: C.text, marginTop: 6, lineHeight: 1.1 }}>
            {renewalDate ? fmtDate(renewalDate) : "—"}
          </p>
          <p style={{ fontSize: 12, color: C.textSub, marginTop: 4 }}>
            {daysToRenewal !== null
              ? `${daysToRenewal} ${daysToRenewal === 1 ? tr("billingDay", "day") : tr("billingDays", "days")} ${tr("billingRemaining", "remaining")}`
              : "—"}
          </p>
        </div>

        {/* BILLING STATUS */}
        <div style={{ padding: "16px 18px", borderRadius: 12, background: C.surface, border: `1px solid ${C.border}` }}>
          <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", margin: 0 }}>
            {tr("billingCardStatus", "Billing Status")}
          </p>
          <div style={{ marginTop: 6 }}>
            <Badge color={subStatusTint.color} bg={subStatusTint.bg} border={subStatusTint.border} size="lg">
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: subStatusTint.color }} />
              {subStatusTint.label}
            </Badge>
          </div>
          <p style={{ fontSize: 12, color: C.textSub, marginTop: 8 }}>
            {audit && audit[0]?.created_at
              ? `${tr("billingLastEvent", "Last event")}: ${new Date(audit[0].created_at).toLocaleString([], { hour: "2-digit", minute: "2-digit" })}`
              : tr("billingNoEventsYet", "No events yet")}
          </p>
        </div>
      </div>

      {/* ─── ALERT BANNERS (server-driven, minus the ones a customer can't act on) ── */}
      {(summary?.alerts || []).filter(a => !SUPPRESSED_ALERTS.includes(a.key)).length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {(summary.alerts || []).filter(a => !SUPPRESSED_ALERTS.includes(a.key)).map((a, i) => {
            const def = ALERT_DEFS(a);
            const tint = def.tier === "red"
              ? { color: C.red, bg: C.redBg, border: C.redBdr }
              : { color: C.amber, bg: C.amberBg, border: C.amberBdr };
            return (
              <div key={i} style={{
                display: "flex", alignItems: "center", gap: 12,
                padding: "12px 16px", borderRadius: 10,
                background: tint.bg, border: `1px solid ${tint.border}`,
              }}>
                <BIcon n="alert" s={18} c={tint.color} strokeWidth={2.2} />
                <p style={{ flex: 1, fontSize: 13, color: tint.color, fontWeight: 600, margin: 0 }}>
                  {def.title}
                </p>
                <button onClick={() => handleAlertCta(a.key)} style={{
                  padding: "7px 14px", borderRadius: 8, border: `1px solid ${tint.color}`,
                  background: "transparent", color: tint.color, fontWeight: 700, fontSize: 12,
                  cursor: "pointer", fontFamily: "inherit",
                }}>{def.cta}</button>
              </div>
            );
          })}
        </div>
      )}

      {/* ─── TAB NAV (Figma — underline style) ────────────────────────────── */}
      <nav style={{
        display: "flex", gap: 0, paddingTop: 4,
        borderBottom: `1px solid ${C.border}`,
        overflowX: "auto",
      }}>
        {SECTIONS.map(s => {
          const active = section === s.k;
          return (
            <button key={s.k} onClick={() => setSection(s.k)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 8,
                padding: "10px 16px",
                background: "transparent",
                border: "none",
                borderBottom: active ? `2px solid ${C.brand}` : "2px solid transparent",
                marginBottom: -1,
                cursor: "pointer", fontFamily: "inherit",
                color: active ? C.text : C.textSub, fontWeight: active ? 700 : 500,
                fontSize: 13, transition: "all .18s",
                whiteSpace: "nowrap",
              }}>
              {s.l}
            </button>
          );
        })}
      </nav>

      {loading && (
        <SectionCard C={C}>
          <div style={{ textAlign: "center", padding: 32, color: C.textSub, fontSize: 13 }}>
            {tr("billingLoading", "Loading billing details…")}
          </div>
        </SectionCard>
      )}

      {/* ── PLAN OVERVIEW TAB (Figma) ─────────────────────────────────────── */}
      {!loading && section === "overview" && (() => {
        const sub = summary?.subscription || {};
        const cycle = sub.billing_cycle || "monthly";
        // The raw number, for showing the customer the actual sum.
        const perEmployeePrice = currentPlan
          ? (cycle === "annual"
              ? (currentPlan.annual_price_per_user || 0)
              : (currentPlan.monthly_price_per_user || 0))
          : 0;
        // "employee", not "seat" — nothing here is a purchasable seat.
        const pricePerSeat = currentPlan && perEmployeePrice
          ? `${fmtMoney(perEmployeePrice, currentPlan.currency)}${
              cycle === "annual"
                ? tr("billingPerEmployeeYr", "/employee/yr")
                : tr("billingPerEmployeeMo", "/employee/mo")}`
          : null;
        // Plan capacity — the seat ceiling the plan includes (BillingPlan.user_limit),
        // NOT a quantity anyone bought. Billing is per active user: Stripe's quantity
        // equals the billable headcount, which is `active` below.
        //
        // This previously read `sub.seat_count ?? seat_metrics.included` and was
        // labelled "seats purchased". BillingSubscription has no seat_count column at
        // all (only app/commercial's models do), so the first term never resolved and
        // the label described plan capacity as a purchase — telling a customer they
        // owned 100 seats when they were billed for 35.
        // Only the billed figure is needed here. Plan capacity (seat_metrics.included)
        // and pending invites are deliberately not shown: neither affects the bill, and
        // showing them is what made the page read like a seat-purchasing console.
        const active = summary?.seat_metrics?.active ?? 0;
        const nextInvoice = summary?.next_invoice || null;
        const defaultPm = paymentMethods.find(p => p.is_default) || paymentMethods[0] || summary?.payment_method || null;

        return (
          <>
            {/* Internal tenant: explain why purchase actions do nothing, instead of
                leaving the admin to discover it by clicking. */}
            {isInternalTenant && (
              <SectionCard C={C} padding="14px 18px">
                <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                  <BIcon n="lock" s={14} c={C.textSub} />
                  <div>
                    <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>
                      {tr("billingInternalTitle", "Internal organization — not billed")}
                    </p>
                    <p style={{ fontSize: 12, color: C.textSub, margin: "3px 0 0" }}>
                      {tr("billingInternalBody",
                        "This workspace belongs to Temveris and has full plan features at no charge. Plan purchases, payment methods and the billing portal do not apply.")}
                    </p>
                  </div>
                </div>
              </SectionCard>
            )}

            {/* ── Current plan card ── */}
            <SectionCard C={C} padding="22px 24px">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 18, flexWrap: "wrap" }}>
                <div style={{ flex: 1, minWidth: 240 }}>
                  <Badge color={C.green} bg={C.greenBg} border={C.greenBdr}>{tr("billingCurrentPlanTag", "Current plan")}</Badge>
                  <h3 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: "10px 0 4px" }}>
                    {currentPlan?.name ? `${currentPlan.name} ${tr("billingPlanSuffix", "Plan")}` : "—"}
                  </h3>
                  <p style={{ fontSize: 13, color: C.textSub }}>
                    {[
                      titleCase(cycle === "annual" ? tr("billingAnnualBilling", "Annual billing") : tr("billingMonthlyBilling", "Monthly billing")),
                      pricePerSeat,
                      sub.renewal_date && `${tr("billingNextInvoice", "Next invoice")} ${fmtDate(sub.renewal_date)}`,
                    ].filter(Boolean).join(" · ")}
                  </p>
                </div>
                <div style={{ textAlign: "right" }}>
                  <p style={{ fontSize: 26, fontWeight: 800, color: C.text, margin: 0, fontVariantNumeric: "tabular-nums" }}>
                    {nextInvoice
                      ? fmtMoney(nextInvoice.amount, nextInvoice.currency)
                      : (active && pricePerSeat && currentPlan
                          ? fmtMoney(
                              (cycle === "annual"
                                ? (currentPlan.annual_price_per_user || 0)
                                : (currentPlan.monthly_price_per_user || 0)
                              ) * active,
                              currentPlan.currency,
                            )
                          : "—")}
                  </p>
                  <p style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                    {cycle === "annual" ? tr("billingPerYear", "per year") : tr("billingPerMonth", "per month")}
                  </p>
                  {/* The sum, spelled out. "4 active employees × $25 = $100" answers
                      "why this number?" faster than any label can. */}
                  {active && perEmployeePrice ? (
                    <p style={{ fontSize: 12.5, color: C.text, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>
                      {active} {active === 1
                        ? tr("billingActiveEmployeeSingular", "active employee")
                        : tr("billingActiveEmployeePlural", "active employees")}
                      {" × "}{fmtMoney(perEmployeePrice, currentPlan?.currency)}
                      {" = "}
                      <strong>{fmtMoney(perEmployeePrice * active, currentPlan?.currency)}</strong>
                      {cycle === "annual" ? tr("billingPerYearShort", "/year") : tr("billingPerMonthShort", "/month")}
                    </p>
                  ) : null}
                  {/* Prices are final and tax-inclusive (business decision 2026-08-12),
                      so nothing is added later. */}
                  <p style={{ fontSize: 11, color: C.textLight, marginTop: 2 }}>
                    {nextInvoice?.tax_note || tr("billingTaxInclusive", "Prices are final and tax-inclusive. No additional tax is applied.")}
                  </p>
                  <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12, flexWrap: "wrap" }}>
                    {/* "Add seats" removed. It implied a purchase that does not exist —
                        seats are never bought here, they follow active employees — and
                        it opened the plans list, which is not adding a seat. */}
                    <button onClick={() => setView("plans")} style={btnPrimary(C)}>{tr("billingChangePlan", "Change plan")}</button>
                  </div>
                </div>
              </div>

              {/* Four-stat row */}
              <div style={{
                display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
                gap: 14, marginTop: 22, paddingTop: 20, borderTop: `1px solid ${C.borderLight}`,
              }}>
                {[
                  // Two stats, not four. "Pending invites" and "Seats available" were
                  // both seat-capacity concepts: invites are not billed, and there is no
                  // pool of seats to have any left of.
                  { l: tr("billingStatActiveEmployees", "Active employees"), v: String(active),
                    s: tr("billingStatActiveEmployeesSub", "billed this cycle") },
                  // The value must follow the same condition as the label. It used to prefer
                  // trialEndsAt unconditionally, so a customer who had converted from trial to
                  // paid saw the label "Next payment" above their old TRIAL end date, while the
                  // caption underneath said "365 days remaining" computed from the renewal date.
                  // Noxx Industries showed "Next payment Sep 12, 2026 / 365 days remaining" the
                  // moment they paid for an annual plan that renews Aug 21, 2027.
                  { l: isTrial ? tr("billingStatTrialEnds", "Trial ends") : tr("billingStatRenewsOn", "Next payment"),
                    v: (() => {
                      const when = isTrial ? (trialEndsAt || renewalDate) : renewalDate;
                      return when ? fmtDate(when) : "—";
                    })(),
                    s: (isTrial ? daysToTrialEnd : daysToRenewal) !== null
                        ? `${(isTrial ? daysToTrialEnd : daysToRenewal)} ${tr("billingDaysRemaining", "days remaining")}`
                        : "",
                    // Only paint the deadline red while the worker is in
                    // trial — once the subscription is active, the renewal
                    // date is informational, not urgent.
                    urgent: isTrial },
                ].map(c => (
                  <div key={c.l}>
                    <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.5, textTransform: "uppercase", margin: 0 }}>{c.l}</p>
                    <p style={{ fontSize: 18, fontWeight: 800, color: c.urgent ? C.red : C.text, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>{c.v}</p>
                    {c.s && <p style={{ fontSize: 11.5, color: C.textSub, marginTop: 2 }}>{c.s}</p>}
                  </div>
                ))}
              </div>

              {/* How billing works, in one sentence.
                  Replaces the seat-usage progress bar, which measured consumption of a
                  seat pool that does not exist and turned amber/red as headcount grew —
                  implying the customer was running out of something they had to top up. */}
              <div style={{
                marginTop: 18, padding: "12px 14px", borderRadius: 10,
                background: C.surfaceAlt || C.borderLight, border: `1px solid ${C.borderLight}`,
                display: "flex", gap: 10, alignItems: "flex-start",
              }}>
                <BIcon n="info" s={14} c={C.textSub} />
                <p style={{ fontSize: 12.5, color: C.textSub, margin: 0, lineHeight: 1.55 }}>
                  {tr("billingHowItWorks",
                    "You're billed based on your active employees. Add or deactivate employees from Team Directory and your billing updates automatically.")}
                </p>
              </div>
            </SectionCard>

            {/* ── Available Actions row ── */}
            <div>
              <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", margin: "4px 0 10px" }}>
                {tr("billingAvailableActions", "Available actions")}
              </p>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14 }}>
                {[
                  {
                    title: tr("billingActionUpgradeTitle", "Change plan"),
                    // Was "Move to Business or Enterprise…" — neither is a Temveris plan.
                    // The real tiers are Verified, Governed, Sovereign and Enterprise.
                    body:  tr("billingActionUpgradeBody", "Compare Verified, Governed and Sovereign, and switch whenever you need to."),
                    cta:   tr("billingUpgradePlan", "View plans"),
                    tint:  { color: C.green, bg: C.greenBg },
                    onClick: () => setView("plans"),
                    primary: true,
                  },
                  {
                    // Was "Manage Seats — add or reduce seats". There are no seats to
                    // manage: the bill follows active employees, so the honest action is
                    // to manage the team and let billing follow.
                    title: tr("billingActionManageTeamTitle", "Manage your team"),
                    body:  tr("billingActionManageTeamBody", "Add or deactivate employees in Team Directory. Your bill updates automatically — there are no seats to buy."),
                    cta:   tr("billingManageTeam", "Open Team Directory"),
                    tint:  { color: C.brand, bg: C.brandBg },
                    onClick: () => window.location.assign("/admin?tab=myteam"),
                  },
                  {
                    title: tr("billingActionAddPaymentTitle", "Payment method"),
                    // The trial reference was not always true; state what it does.
                    body:  tr("billingActionAddPaymentBody", "Card details are entered securely on Stripe. Temveris never sees your card number."),
                    cta:   tr("billingAddPaymentMethod", "Add payment method"),
                    tint:  { color: C.amber, bg: C.amberBg },
                    onClick: () => startCardSetup(),
                  },
                ].map((a, i) => (
                  <div key={i} style={{
                    padding: "18px 20px", borderRadius: 12,
                    background: C.surface, border: `1px solid ${C.border}`,
                    display: "flex", flexDirection: "column", gap: 10,
                  }}>
                    <div aria-hidden style={{
                      width: 32, height: 32, borderRadius: 8, background: a.tint.bg,
                      border: `1px solid ${a.tint.color}33`,
                    }} />
                    <p style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0 }}>{a.title}</p>
                    <p style={{ fontSize: 12.5, color: C.textSub, margin: 0, lineHeight: 1.55, minHeight: 56 }}>{a.body}</p>
                    <button onClick={a.onClick} style={a.primary ? btnPrimary(C) : btnGhost(C)}>{a.cta}</button>
                  </div>
                ))}
              </div>
            </div>

            {/* ── Payment method preview ── */}
            <div>
              <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", margin: "4px 0 10px" }}>
                {tr("billingPaymentMethodHeader", "Payment Method")}
              </p>
              <SectionCard C={C} padding="16px 20px">
                {defaultPm ? (
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 14, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                      <div style={{
                        width: 46, height: 32, borderRadius: 6,
                        background: C.brandBg, border: `1px solid ${C.brandBdr}`,
                        display: "flex", alignItems: "center", justifyContent: "center",
                      }}>
                        <BIcon n="card" s={18} c={C.brand} />
                      </div>
                      <div>
                        <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
                          {(defaultPm.brand || "Card").toUpperCase()} •••• {defaultPm.last_four || "—"}
                        </p>
                        <p style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                          {defaultPm.exp_month && defaultPm.exp_year
                            ? `${tr("billingExpires", "Expires")} ${String(defaultPm.exp_month).padStart(2,"0")}/${String(defaultPm.exp_year).slice(-2)}`
                            : (defaultPm.is_default ? tr("billingDefault", "Default") : "")}
                        </p>
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 8 }}>
                      <button onClick={() => openCustomerPortal()} style={btnGhost(C)}>
                        <BIcon n="plus" s={13} c={C.brand} strokeWidth={2.4} />
                        {tr("billingAddCard", "Add card")}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 14, flexWrap: "wrap" }}>
                    <p style={{ fontSize: 13, color: C.textSub, margin: 0 }}>
                      {tr("billingNoPaymentDescShort", "No payment method on file. Add one so your renewal goes through automatically.")}
                    </p>
                    <button onClick={() => startCardSetup()} style={btnPrimary(C)}>
                      <BIcon n="plus" s={13} c="#fff" strokeWidth={2.6} />
                      {tr("billingAddPaymentMethod", "Add Payment Method")}
                    </button>
                  </div>
                )}
              </SectionCard>
            </div>
          </>
        );
      })()}

      {/* ── INVOICES & PAYMENTS TAB (Figma) ───────────────────────────────── */}
      {!loading && section === "invoices" && (
        <SectionCard C={C} padding="0">
          {/* Filter row */}
          <div style={{
            display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
            padding: "16px 22px",
            borderBottom: `1px solid ${C.borderLight}`,
          }}>
            <input
              type="text"
              placeholder={tr("billingSearchInvoices", "Search invoices…")}
              value={invoiceFilter.search}
              onChange={(e) => setInvoiceFilter(f => ({ ...f, search: e.target.value }))}
              style={{ ...inputStyle(C), flex: 1, minWidth: 240 }}
            />
            <select
              value={invoiceFilter.status}
              onChange={(e) => setInvoiceFilter(f => ({ ...f, status: e.target.value }))}
              style={{ ...inputStyle(C), minWidth: 150 }}
            >
              <option value="">{tr("billingAllStatuses", "All statuses")}</option>
              {["draft","issued","paid","pending","failed","past_due","voided","refunded"].map(s =>
                <option key={s} value={s}>{titleCase(s)}</option>
              )}
            </select>
            <select
              value={invoiceFilter.range || "12m"}
              onChange={(e) => setInvoiceFilter(f => ({ ...f, range: e.target.value }))}
              style={{ ...inputStyle(C), minWidth: 150 }}
            >
              <option value="12m">{tr("billingLast12Months", "Last 12 months")}</option>
              <option value="6m">{tr("billingLast6Months",  "Last 6 months")}</option>
              <option value="ytd">{tr("billingYearToDate",  "Year to date")}</option>
              <option value="all">{tr("billingAllTime",     "All time")}</option>
            </select>
            <button onClick={downloadStatement} style={btnGhost(C)}>
              <BIcon n="download" s={13} c={C.brand} strokeWidth={2.4} />
              {tr("billingExportAll", "Export all")}
            </button>
          </div>

          {/* Table */}
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: C.surfaceAlt }}>
                  {[
                    tr("billingColInvoiceDate", "Invoice Date"),
                    tr("billingColInvoiceNum",  "Invoice #"),
                    tr("billingColDescription", "Description"),
                    tr("billingColAmount",      "Amount"),
                    tr("billingColTax",         "Tax"),
                    tr("billingColStatus",      "Status"),
                    tr("billingColActions",     "Actions"),
                  ].map(h =>
                    <th key={h} style={thStyle(C)}>{h}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {invoices.map(inv => {
                  const tint = STATUS[inv.status] || STATUS.draft;
                  // Build a one-line description: plan + seats + cycle, or
                  // fall back to the line_items field if the API supplies one.
                  const lineDescription =
                    inv.description
                    || (inv.line_items && inv.line_items[0]?.description)
                    || `${inv.plan_name || currentPlan?.name || ""} · ${inv.seat_count || ""} ${tr("billingSeats", "seats")} · ${titleCase(inv.billing_cycle || "")}`
                      .replace(/^[· ]+|[· ]+$/g, "");
                  return (
                    <tr key={inv.id} style={{ borderTop: `1px solid ${C.borderLight}` }}>
                      <td style={tdStyle(C)}>{fmtDate(inv.billing_period_end || inv.issued_at || inv.created_at)}</td>
                      <td style={{ ...tdStyle(C), fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", color: C.textSub }}>{inv.invoice_number}</td>
                      <td style={{ ...tdStyle(C), color: C.text }}>{lineDescription || "—"}</td>
                      <td style={{ ...tdStyle(C), fontWeight: 700, color: C.text, fontVariantNumeric: "tabular-nums" }}>
                        {fmtMoney(inv.total_amount ?? inv.amount, inv.currency)}
                      </td>
                      <td style={{ ...tdStyle(C), color: C.textSub, fontVariantNumeric: "tabular-nums" }}>
                        {inv.tax_amount !== undefined && inv.tax_amount !== null ? fmtMoney(inv.tax_amount, inv.currency) : "—"}
                      </td>
                      <td style={tdStyle(C)}>
                        <Badge color={tint.color} bg={tint.bg} border={tint.border}>{tint.label}</Badge>
                      </td>
                      <td style={tdStyle(C)}>
                        <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
                          {/* Only offer a PDF where Stripe actually issued one. Legacy
                              rows predate invoice mirroring and have no document —
                              offering a button that 409s, or fabricating a PDF for a
                              number Stripe never charged, are both worse. */}
                          {inv.has_pdf ? (
                            <button onClick={() => downloadInvoice(inv)}
                              style={{ border: "none", background: "transparent", color: C.brand, fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit", padding: 0 }}
                              title={tr("billingDownloadPdf", "Download PDF")}>
                              {tr("billingPdf", "PDF")}
                            </button>
                          ) : (
                            <span style={{ color: C.textLight, fontSize: 12 }}
                              title={tr("billingNoPdfHint", "This invoice predates Stripe invoicing and has no downloadable document.")}>
                              {tr("billingNoPdf", "No PDF")}
                            </span>
                          )}
                          {inv.status === "paid" && inv.has_pdf && (
                            <button onClick={() => downloadInvoice(inv)}
                              style={{ border: "none", background: "transparent", color: C.brand, fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit", padding: 0 }}
                              title={tr("billingDownloadReceipt", "Download receipt")}>
                              {tr("billingReceipt", "Receipt")}
                            </button>
                          )}
                          {/* "Pay now" removed — it called an endpoint that marked
                              the invoice paid without charging anything. */}
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {invoices.length === 0 && (
                  <tr><td colSpan={7} style={{ ...tdStyle(C), textAlign: "center", padding: 36, color: C.textSub }}>
                    {tr("billingNoInvoices", "No invoices match your filters yet.")}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>

          {/* Footer note */}
          <div style={{ padding: "12px 22px", borderTop: `1px solid ${C.borderLight}`, fontSize: 11.5, color: C.textSub }}>
            {tr("billingInvoiceFooter", "Invoice downloads use secure expiring links and are logged in the billing audit trail.")}
          </div>
        </SectionCard>
      )}

      {/* ── BILLING PROFILE TAB (Figma — merged tax + contacts) ───────────── */}
      {!loading && section === "profile" && (
        <>
          {/* Tax / legal entity form (re-uses the existing sub-component) */}
          <TaxProfileForm C={C} value={taxProfile || {}} onSave={submitTax} tr={tr} />

          {/* Billing contacts list */}
          <SectionCard C={C}
            title={tr("billingContactsTitle", "Billing contacts")}
            subtitle={tr("billingContactsSubtitle", "Choose who receives invoices, renewal notices, payment alerts, and tax documents.")}
            action={
              <button onClick={() => setContactModal({})} style={btnPrimary(C)}>
                <BIcon n="plus" s={13} c="#fff" strokeWidth={2.6} />
                {tr("billingAddContact", "Add contact")}
              </button>
            }>
            {contacts.length === 0 && (
              <div style={{ textAlign: "center", padding: 28, color: C.textSub, fontSize: 13 }}>
                {tr("billingNoContacts", "No billing contacts yet. Add one to receive invoices and renewal notices.")}
              </div>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {contacts.map(c => (
                <div key={c.id} style={{
                  display: "grid", gridTemplateColumns: "1fr auto", alignItems: "center", gap: 10,
                  padding: 14, border: `1px solid ${C.border}`, borderRadius: 10,
                  background: c.is_primary ? C.brandBg : C.surface,
                }}>
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <p style={{ fontSize: 14, fontWeight: 800, color: C.text }}>{c.name}</p>
                      {c.is_primary && <Badge color={C.brand} bg={C.surface} border={C.brandBdr}>{tr("billingPrimary", "Primary")}</Badge>}
                      <Badge color={C.textSub} bg={C.surfaceAlt} border={C.border}>{c.role}</Badge>
                    </div>
                    <p style={{ fontSize: 12.5, color: C.textSub, marginTop: 4 }}>{c.email}</p>
                    <div style={{ display: "flex", gap: 10, marginTop: 6, flexWrap: "wrap" }}>
                      {c.receives_invoices       && <Tag C={C}>{tr("billingNotifInvoices",  "Invoices")}</Tag>}
                      {c.receives_payment_alerts && <Tag C={C}>{tr("billingNotifPayments",  "Payment alerts")}</Tag>}
                      {c.receives_renewal_notices && <Tag C={C}>{tr("billingNotifRenewals", "Renewal notices")}</Tag>}
                      {c.receives_tax_documents  && <Tag C={C}>{tr("billingNotifTax",       "Tax documents")}</Tag>}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button onClick={() => setContactModal(c)} style={iconBtn(C)} title={tr("billingEdit","Edit")}>
                      <BIcon n="edit" s={13} c={C.brand} />
                    </button>
                    <button onClick={() => deleteContact(c.id)} style={{ ...iconBtn(C), borderColor: C.redBdr }} title={tr("billingRemove","Remove")}>
                      <BIcon n="trash" s={13} c={C.red} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </SectionCard>

          {/* Saved payment methods (full management — different from the
              preview card on the Plan Overview tab). Lets the admin remove
              old cards or add additional ones without leaving the profile. */}
          <SectionCard C={C}
            title={tr("billingPaymentTitle", "Payment methods on file")}
            subtitle={tr("billingPaymentSubtitle", "Cards on file are tokenized via Stripe. Temveris never stores raw card numbers.")}
            action={
              <button onClick={() => openCustomerPortal()} style={btnPrimary(C)}>
                <BIcon n="plus" s={13} c="#fff" strokeWidth={2.6} />
                {tr("billingAddCard", "Add card")}
              </button>
            }>
            {paymentMethods.length === 0 ? (
              <div style={{ textAlign: "center", padding: 28, color: C.textSub, fontSize: 13 }}>
                {tr("billingNoPaymentDesc", "No payment methods yet. Add a card so we can charge your renewal automatically.")}
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 14 }}>
                {paymentMethods.map(pm => (
                  <div key={pm.id} style={{
                    padding: 18, border: `1px solid ${C.border}`, borderRadius: 12,
                    background: pm.is_default ? C.brandBg : C.surface,
                    position: "relative", overflow: "hidden",
                  }}>
                    {pm.is_default && (
                      <span style={{ position: "absolute", top: 12, right: 12, fontSize: 10, fontWeight: 800, color: C.brand, background: C.surface, padding: "2px 10px", borderRadius: 999, border: `1px solid ${C.brandBdr}` }}>
                        {tr("billingDefault", "Default")}
                      </span>
                    )}
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div style={{ width: 40, height: 40, borderRadius: 10, background: C.surface, border: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <BIcon n="card" s={18} c={C.brand} />
                      </div>
                      <div>
                        <p style={{ fontSize: 14, fontWeight: 800, color: C.text, textTransform: "uppercase", letterSpacing: 1 }}>
                          {pm.brand} •••• {pm.last_four}
                        </p>
                        <p style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                          {tr("billingExpires", "Expires")} {String(pm.expiry_month || pm.exp_month || "").padStart(2, "0")} / {pm.expiry_year || pm.exp_year}
                        </p>
                      </div>
                    </div>
                    {pm.billing_name && (
                      <p style={{ fontSize: 12, color: C.textSub, marginTop: 10 }}>{pm.billing_name}</p>
                    )}
                    <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                      <button onClick={() => removePaymentMethod(pm.id)} style={{ ...btnGhost(C), color: C.red, borderColor: C.redBdr }}>
                        <BIcon n="trash" s={13} c={C.red} />
                        {tr("billingRemove", "Remove")}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <p style={{ fontSize: 12, color: C.textLight, marginTop: 14 }}>
              <BIcon n="lock" s={11} c={C.textLight} /> {tr("billingPaymentSecurity", "All card details are tokenized through Stripe. Temveris stores token references only.")}
            </p>
          </SectionCard>
        </>
      )}

      {/* ── ENTITLEMENTS TAB (Figma pill-chip layout) ─────────────────────── */}
      {!loading && section === "entitlements" && (() => {
        const ent = entitlements || {};
        const featuresObj = ent.features || ent.feature_flags || {};
        const limits     = ent.limits     || {};

        // ── Authoritative plan vs the commercial entitlements profile ──────────
        // Two systems describe the same tenant. Billing (/api/admin/billing/summary)
        // is authoritative: it is what Stripe charges against. The commercial layer
        // (/api/v1/entitlements/current) computes a capability profile, but it needs a
        // row in commercial_subscriptions — and that table is empty, so it falls back
        // to _empty_profile(plan="verified", state="manual_review"): EVERY feature
        // false, EVERY limit zero, manual_review_required true.
        //
        // Rendered as truth, that told a paying Governed customer they were on
        // Verified with nothing enabled and an account under review. So: the plan name
        // now comes from billing first, and a profile that contradicts billing (or is
        // the manual_review fallback) is treated as "not provisioned yet" rather than
        // as an authoritative list of zeroes.
        //
        // The real fix belongs in the commercial layer, which is out of scope here.
        const planLabel = (currentPlan?.name || ent.plan_name || ent.plan_code || "").toString();
        const entPlanCode = (ent.plan || ent.plan_code || "").toString().toLowerCase();
        const billingPlanCode = (currentPlan?.code || "").toString().toLowerCase();
        const entProfileStale =
          entitlements !== null && (
            ent?.restrictions?.manual_review_required === true ||
            ent.commercial_state === "manual_review" ||
            (!!entPlanCode && !!billingPlanCode && entPlanCode !== billingPlanCode)
          );

        // ── Canonical pill catalog ──
        // Each entry describes one "feature" that the Figma renders as a
        // pill chip. The pill's label may include a value drawn from the
        // limits map (e.g. "Data retention: 24 months", "API access
        // (5,000 req/day)"). Adding a new feature here only requires the
        // backend to flip its flag — no other UI change needed.
        const month = (days) => {
          if (days == null) return null;
          if (days === -1) return tr("billingUnlimited", "Unlimited");
          if (days >= 365) {
            const yrs = Math.round(days / 365);
            return `${yrs} ${yrs === 1 ? tr("billingYear", "year") : tr("billingYears", "years")}`;
          }
          if (days >= 30) {
            const mos = Math.round(days / 30);
            return `${mos} ${tr("billingMonths", "months")}`;
          }
          return `${days} ${tr("billingDays", "days")}`;
        };
        const reqLabel = (n) =>
          n == null ? null
          : n === -1 ? tr("billingUnlimited", "Unlimited")
          : `${Number(n).toLocaleString()} ${tr("billingReqDay", "req/day")}`;
        const screenshotLabel = (rate) =>
          rate == null ? null
          : rate === -1 ? tr("billingUnlimitedShort", "unlimited")
          : `${tr("billingUpTo", "up to")} ${rate}/hr`;

        // value/flag resolution: try several keys for cross-version safety.
        const flag = (...keys) => keys.some(k => {
          const v = featuresObj[k];
          return v === true || v === "true" || v === 1;
        });
        const lim = (...keys) => {
          for (const k of keys) {
            if (limits[k] !== undefined && limits[k] !== null) return limits[k];
          }
          return null;
        };

        // Build pills with their enabled state. `value` is appended in the
        // pill label when set (e.g. "Data retention: 24 months").
        const PILLS = [
          { key: "time_tracking",       label: "Time tracking",                                 enabled: flag("time_tracking") || true },
          { key: "attendance",          label: "Attendance & timesheets",                       enabled: flag("attendance", "timesheets") || true },
          { key: "screenshots",         label: `Screenshots${screenshotLabel(lim("screenshots_per_hour","screenshots_per_user")) ? ` (${screenshotLabel(lim("screenshots_per_hour","screenshots_per_user"))})` : ""}`,
                                        enabled: flag("screenshots") || lim("screenshots_per_hour","screenshots_per_user") != null },
          { key: "leave_management",    label: "Leave management",                              enabled: flag("leave_management") || true },
          { key: "team_directory",      label: "Team directory",                                enabled: flag("team_directory") || true },
          { key: "standard_reporting",  label: "Standard reporting",                            enabled: flag("standard_reporting","reporting") || true },
          { key: "behavioral_insights", label: "Behavioral pattern insights",                   enabled: flag("behavioral_insights","behavioral_pattern_insights") },
          { key: "public_holidays",     label: "Public holiday management",                     enabled: flag("public_holidays","public_holiday_management") || true },
          { key: "custom_branding",     label: "Custom branding",                               enabled: flag("custom_branding","branding") },
          { key: "notifications",       label: "Email & in-app notifications",                  enabled: flag("notifications","email_notifications") || true },
          { key: "data_retention",      label: `Data retention${month(lim("retention_days","data_retention_days")) ? `: ${month(lim("retention_days","data_retention_days"))}` : ""}`,
                                        enabled: lim("retention_days","data_retention_days") != null },
          { key: "support_email",       label: "Standard support (email)",                      enabled: flag("support_email") || (!flag("support_priority") && !flag("dedicated_account_manager")) },
          { key: "audit_trail",         label: "Billing audit trail",                           enabled: flag("audit_trail","billing_audit_trail") || true },
          { key: "api_access",          label: `API access${reqLabel(lim("api_requests_per_day")) ? ` (${reqLabel(lim("api_requests_per_day"))})` : ""}`,
                                        enabled: flag("api_access") || lim("api_requests_per_day") != null },
          // Higher-tier pills — usually disabled on Starter / Growth.
          { key: "sso",                 label: "SSO / SAML / OIDC",                             enabled: flag("sso","saml","oidc") },
          { key: "advanced_analytics",  label: "Advanced analytics dashboard",                  enabled: flag("advanced_analytics") },
          { key: "unlimited_api",       label: "Unlimited API access",                          enabled: flag("unlimited_api") || lim("api_requests_per_day") === -1 },
          { key: "screenshots_unlim",   label: "Screenshots (unlimited)",                       enabled: flag("screenshots_unlimited") || lim("screenshots_per_hour") === -1 },
          { key: "retention_long",      label: "Data retention: 5 years",                       enabled: (lim("retention_days") || 0) >= 5 * 365 || flag("data_retention_5y") },
          { key: "account_manager",     label: "Dedicated account manager",                     enabled: flag("dedicated_account_manager","account_manager") },
          { key: "support_priority",    label: "Priority support (phone + SLA)",                enabled: flag("support_priority","priority_support") },
          { key: "ip_allowlist",        label: "IP allowlisting",                               enabled: flag("ip_allowlist","ip_allowlisting") },
          { key: "scim",                label: "SCIM provisioning",                             enabled: flag("scim","scim_provisioning") },
          { key: "hipaa_dpa",           label: "HIPAA / DPA agreements",                        enabled: flag("hipaa","dpa","hipaa_dpa") },
          { key: "custom_contract",     label: "Custom contract terms",                         enabled: flag("custom_contract") || ent.plan_code === "enterprise" },
        ];

        const enabledPills = PILLS.filter(p => p.enabled);
        const lockedPills  = PILLS.filter(p => !p.enabled);
        // A stale/fallback profile must not be rendered as a definitive capability
        // list — all-false pills read as "you have nothing", which is worse than
        // showing the not-provisioned state. Genuine profiles still render normally.
        const hasData = !entProfileStale && (entitlements !== null || enabledPills.length > 0);

        const Chip = ({ children, locked }) => (
          <span style={{
            display: "inline-flex", alignItems: "center",
            padding: "8px 14px", borderRadius: 999,
            fontSize: 12.5, fontWeight: 600,
            background:  locked ? C.surfaceAlt : C.greenBg,
            border:     `1px solid ${locked ? C.border : C.greenBdr}`,
            color:       locked ? C.textSub  : C.green,
            opacity:     locked ? 0.85 : 1,
            whiteSpace: "nowrap",
            cursor: "default",
          }}>{children}</span>
        );

        return (
          <SectionCard C={C}>
            <p style={{ fontSize: 13.5, color: C.text, margin: 0, lineHeight: 1.6 }}>
              {tr("billingEntDescription",
                "Entitlements are computed from your current plan and any enterprise contract overrides. Module access, limits, and SLA are shown below.")}
            </p>

            {!hasData ? (
              <div style={{ textAlign: "center", padding: 28, color: C.textSub, fontSize: 13, marginTop: 20 }}>
                {/* Name the authoritative plan, so an unprovisioned capability profile
                    never reads as "you are on a lesser plan than you paid for". */}
                {currentPlan?.name && (
                  <p style={{ margin: "0 0 8px", color: C.text, fontWeight: 700, fontSize: 13.5 }}>
                    {tr("billingEntActivePlan", "Your active plan")}: {currentPlan.name}
                  </p>
                )}
                {tr("billingEntEmpty", "No entitlements profile yet — they appear here once your subscription is provisioned.")}
              </div>
            ) : (
              <>
                {/* Enabled features section */}
                <p style={{
                  fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6,
                  textTransform: "uppercase", margin: "20px 0 12px",
                }}>
                  {tr("billingEntEnabledHeader", "Enabled features")}
                  {planLabel ? ` — ${planLabel}${planLabel.toLowerCase().endsWith("plan") ? "" : " " + tr("billingPlanSuffix", "Plan")}` : ""}
                </p>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                  {enabledPills.map(p => <Chip key={p.key}>{p.label}</Chip>)}
                </div>

                {/* Locked section — only render if there are locked features
                    AND the current plan isn't Enterprise (where the list
                    would be empty anyway). */}
                {lockedPills.length > 0 && (
                  <>
                    <p style={{
                      fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.6,
                      textTransform: "uppercase", margin: "26px 0 12px",
                    }}>
                      {tr("billingEntLockedHeader", "Locked — available on Business or Enterprise")}
                    </p>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
                      {lockedPills.map(p => <Chip key={p.key} locked>{p.label}</Chip>)}
                    </div>

                    {/* CTA card */}
                    <div style={{
                      marginTop: 24, padding: "18px 22px",
                      borderRadius: 12, border: `1px solid ${C.border}`,
                      background: C.surfaceAlt,
                      display: "flex", alignItems: "center", justifyContent: "space-between",
                      gap: 14, flexWrap: "wrap",
                    }}>
                      <div>
                        <p style={{ fontSize: 14, fontWeight: 800, color: C.text, margin: 0 }}>
                          {tr("billingEntUpgradeTitle", "Need more features?")}
                        </p>
                        <p style={{ fontSize: 12.5, color: C.textSub, margin: "4px 0 0", lineHeight: 1.55 }}>
                          {tr("billingEntUpgradeBody", "Upgrade to Business or Enterprise to unlock SSO, advanced analytics, unlimited API, and dedicated support.")}
                        </p>
                      </div>
                      <button onClick={() => setView("plans")} style={btnPrimary(C)}>
                        {tr("billingComparePlans", "Compare plans")}
                      </button>
                    </div>
                  </>
                )}

                {/* Source line */}
                <p style={{ fontSize: 11, color: C.textLight, marginTop: 18 }}>
                  {ent.cache_refreshed_at
                    ? `${tr("billingEntRefreshed", "Last refreshed")}: ${new Date(ent.cache_refreshed_at).toLocaleString()}`
                    : tr("billingEntComputedNow", "Computed live from your subscription.")}
                </p>
              </>
            )}
          </SectionCard>
        );
      })()}

      {/* ── BILLING AUDIT TRAIL TAB (Figma timeline) ──────────────────────── */}
      {!loading && section === "audit" && (
        <SectionCard C={C} padding="0">
          {/* Filter row */}
          <div style={{
            display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
            padding: "16px 22px",
            borderBottom: `1px solid ${C.borderLight}`,
          }}>
            <input
              type="text"
              placeholder={tr("billingSearchAudit", "Search billing events, actors…")}
              value={auditQuery.search}
              onChange={(e) => setAuditQuery(q => ({ ...q, search: e.target.value }))}
              style={{ ...inputStyle(C), flex: 1, minWidth: 240 }}
            />
            <select
              value={auditQuery.action}
              onChange={(e) => setAuditQuery(q => ({ ...q, action: e.target.value }))}
              style={{ ...inputStyle(C), minWidth: 160 }}
            >
              <option value="">{tr("billingAllEventTypes", "All event types")}</option>
              {auditActionTypes.map(a => <option key={a} value={a}>{titleCase(a)}</option>)}
            </select>
            <select
              value={String(auditQuery.days)}
              onChange={(e) => setAuditQuery(q => ({ ...q, days: Number(e.target.value) }))}
              style={{ ...inputStyle(C), minWidth: 150 }}
            >
              <option value="7">{tr("billingLast7Days",  "Last 7 days")}</option>
              <option value="30">{tr("billingLast30Days","Last 30 days")}</option>
              <option value="90">{tr("billingLast90Days","Last 90 days")}</option>
              <option value="0">{tr("billingAllTime",    "All time")}</option>
            </select>
            <button onClick={exportAuditCsv} style={btnGhost(C)}>
              <BIcon n="download" s={13} c={C.brand} strokeWidth={2.4} />
              {tr("billingExportAuditLog", "Export audit log")}
            </button>
          </div>

          {/* Timeline */}
          {visibleAudit.length === 0 ? (
            <div style={{ textAlign: "center", padding: 40, color: C.textSub, fontSize: 13 }}>
              {tr("billingAuditEmpty", "No billing events match your filters.")}
            </div>
          ) : (
            <ul style={{ listStyle: "none", padding: "8px 22px 12px", margin: 0 }}>
              {visibleAudit.map((a) => {
                const dot = auditDotColor(a);
                const details = auditDetailsLine(a);
                return (
                  <li key={a.id} style={{
                    display: "grid",
                    gridTemplateColumns: "14px 1fr auto",
                    gap: 14, padding: "16px 0",
                    borderTop: `1px solid ${C.borderLight}`,
                  }}>
                    <div style={{
                      width: 10, height: 10, borderRadius: "50%",
                      background: dot, marginTop: 6,
                      boxShadow: `0 0 0 3px ${dot}22`,
                    }} />
                    <div style={{ minWidth: 0 }}>
                      <p style={{ margin: 0, fontSize: 13.5, color: C.text }}>
                        <strong style={{ fontWeight: 800 }}>{a.actor_name || a.actor_email || tr("billingSystem", "System")}</strong>
                        {a.actor_role && (
                          <span style={{ color: C.textSub, marginLeft: 6 }}>({titleCase(a.actor_role)})</span>
                        )}
                        <span style={{ color: C.text, marginLeft: 6 }}>{a.summary || titleCase(a.action_key)}</span>
                      </p>
                      <p style={{ margin: "4px 0 0", fontSize: 11.5, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {[titleCase(a.action_key), details].filter(Boolean).join(" · ")}
                      </p>
                    </div>
                    <p style={{ margin: 0, fontSize: 11.5, color: C.textSub, whiteSpace: "nowrap", paddingTop: 2 }}>
                      {a.created_at ? new Date(a.created_at).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"}
                    </p>
                  </li>
                );
              })}
            </ul>
          )}

          <div style={{ padding: "12px 22px", borderTop: `1px solid ${C.borderLight}`, fontSize: 11.5, color: C.textSub }}>
            {tr("billingAuditFooter", "Billing audit events are immutable. Exports include actor, role, IP, session, and correlation ID metadata.")}
          </div>
        </SectionCard>
      )}

      {/* MODALS */}
      {confirmUpgrade && (
        <UpgradeConfirmModal
          C={C} plan={confirmUpgrade} cycle={billingCycle} subscription={summary?.subscription}
          onClose={() => setConfirmUpgrade(null)}
          onConfirm={() => doUpgrade(confirmUpgrade)} tr={tr}
        />
      )}
      {downgradeFor && (
        <DowngradeReviewModal
          C={C} plan={downgradeFor} onClose={() => setDowngradeFor(null)}
          onSubmit={doSubmitDowngrade} tr={tr}
        />
      )}
      {enterpriseOpen && (
        <EnterpriseQuoteModal C={C} onClose={() => setEnterpriseOpen(false)}
          onSubmit={doSubmitEnterprise}
          defaults={{ company_name: "", work_email: primaryContact?.email || "" }}
          tr={tr} />
      )}
      {contactModal && (
        <ContactFormModal C={C} value={contactModal} onClose={() => setContactModal(null)} onSubmit={submitContact} tr={tr} />
      )}

      <Toast C={C} toast={toast} />
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────
function BillingStyles({ C, isDark }) {
  return (
    <style>{`
      @keyframes billingFade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
      @keyframes billingPop  { from { opacity: 0; transform: scale(.96); } to { opacity: 1; transform: scale(1); } }
      .zt-bill-input:focus, .zt-bill-textarea:focus, .zt-bill-select:focus {
        outline: none; border-color: ${C.brand}; box-shadow: 0 0 0 3px ${C.brandBg};
      }
      .zt-bill-plan:hover { transform: translateY(-3px); box-shadow: 0 14px 32px rgba(16,24,40,.10); }
      .zt-bill-modal-back { background: ${isDark ? "rgba(0,0,0,.55)" : "rgba(16,24,40,.45)"}; backdrop-filter: blur(3px); }
      .zt-bill-btn { transition: transform .14s, box-shadow .18s, background .18s, border-color .18s; }
      .zt-bill-btn:hover { transform: translateY(-1px); }
      .zt-bill-btn:active { transform: translateY(0); }
    `}</style>
  );
}


function PlanCard({ C, plan, cycle, currentPlan, onUpgrade, onDowngrade, onEnterprise, tr }) {
  const isCurrent = currentPlan?.code === plan.code;
  const isEnterprise = plan.is_enterprise;

  let priceLine, periodLine;
  if (isEnterprise) {
    priceLine = tr("billingCustom", "Custom");
    periodLine = tr("billingQuoteLed", "Quote-led pricing");
  } else if (cycle === "annual") {
    priceLine = fmtMoney((plan.annual_price_per_user / 12).toFixed(2), plan.currency);
    periodLine = `${tr("billingPerUserMonth", "/ user / month")} · ${tr("billingBilledAnnually", "billed annually")}`;
  } else {
    priceLine = fmtMoney(plan.monthly_price_per_user.toFixed(2), plan.currency);
    periodLine = tr("billingPerUserMonth", "/ user / month");
  }

  const upgrade = currentPlan && plan.tier_order > currentPlan.tier_order;
  const downgrade = currentPlan && plan.tier_order < currentPlan.tier_order;

  let cta;
  if (isCurrent) cta = { label: tr("billingCurrentPlan","Current Plan"), disabled: true };
  else if (isEnterprise) cta = { label: tr("billingRequestQuote","Request Quote"), action: onEnterprise, primary: true };
  else if (upgrade) cta = { label: `${tr("billingUpgradeTo","Upgrade to")} ${plan.name}`, action: onUpgrade, primary: true };
  else if (downgrade) cta = { label: tr("billingRequestDowngrade","Contact us to downgrade"), action: onDowngrade };
  else cta = { label: `${tr("billingSwitchTo","Switch to")} ${plan.name}`, action: onUpgrade, primary: true };

  const highlighted = plan.is_most_popular;

  return (
    <article className="zt-bill-plan" style={{
      position: "relative", display: "flex", flexDirection: "column", gap: 14,
      padding: 22, borderRadius: 14,
      background: C.surface,
      border: `${highlighted ? 2 : 1}px solid ${highlighted ? C.brand : isCurrent ? C.green : C.border}`,
      boxShadow: highlighted ? "0 6px 20px rgba(17,73,106,.12)" : "0 1px 3px rgba(16,24,40,.05)",
      transition: "transform .25s cubic-bezier(.34,1.56,.64,1), box-shadow .25s",
      animation: "billingPop .25s ease both",
    }}>
      {highlighted && (
        <span style={{
          position: "absolute", top: -11, left: "50%", transform: "translateX(-50%)",
          fontSize: 11, fontWeight: 800, letterSpacing: 0.6,
          color: "#fff", background: `linear-gradient(135deg, ${C.brand}, ${C.brandAccent})`,
          padding: "5px 14px", borderRadius: 999, textTransform: "uppercase",
        }}>{plan.badge || tr("billingRecommended","Recommended")}</span>
      )}
      {isCurrent && !highlighted && (
        <span style={{
          position: "absolute", top: -11, left: "50%", transform: "translateX(-50%)",
          fontSize: 11, fontWeight: 800, letterSpacing: 0.6,
          color: "#fff", background: C.green,
          padding: "5px 14px", borderRadius: 999, textTransform: "uppercase",
        }}>{tr("billingCurrent", "Current Plan")}</span>
      )}

      <div>
        <p style={{ fontSize: 11, fontWeight: 700, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase" }}>
          {plan.badge || ""}
        </p>
        <h3 style={{ fontSize: 20, fontWeight: 800, color: C.text, marginTop: 2 }}>{plan.name}</h3>
        <p style={{ fontSize: 12.5, color: C.textSub, marginTop: 6, minHeight: 56, lineHeight: 1.45 }}>
          {plan.short_description}
        </p>
      </div>

      <div style={{ borderTop: `1px solid ${C.borderLight}`, borderBottom: `1px solid ${C.borderLight}`, padding: "14px 0" }}>
        <p style={{ fontSize: 28, fontWeight: 800, color: C.text, fontVariantNumeric: "tabular-nums", lineHeight: 1 }}>{priceLine}</p>
        <p style={{ fontSize: 12, color: C.textSub, marginTop: 6 }}>{periodLine}</p>
      </div>

      <div>
        <p style={{ fontSize: 11, fontWeight: 700, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 8 }}>
          {tr("billingTopFeatures", "Top features")}
        </p>
        <ul style={{ display: "flex", flexDirection: "column", gap: 8, padding: 0, margin: 0, listStyle: "none" }}>
          {(plan.feature_list_json || []).slice(0, 5).map((f, i) => (
            <li key={i} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, color: f.highlighted ? C.text : C.textSub, fontWeight: f.highlighted ? 700 : 500 }}>
              <BIcon n="check" s={13} c={f.highlighted ? C.green : C.brand} strokeWidth={2.6} />
              {f.label}
            </li>
          ))}
        </ul>
      </div>

      <button
        disabled={cta.disabled}
        onClick={cta.action}
        className="zt-bill-btn"
        style={{
          marginTop: "auto", padding: "11px 14px", borderRadius: 10,
          fontWeight: 700, fontSize: 13, fontFamily: "inherit",
          cursor: cta.disabled ? "default" : "pointer",
          background: cta.disabled ? C.surfaceAlt : cta.primary ? C.brand : C.surface,
          color: cta.disabled ? C.textLight : cta.primary ? "#fff" : C.brand,
          border: cta.disabled ? `1px solid ${C.border}` : cta.primary ? "none" : `1px solid ${C.brandBdr}`,
          boxShadow: cta.primary && !cta.disabled ? "0 4px 14px rgba(17,73,106,.25)" : "none",
        }}>
        {cta.label}
      </button>
    </article>
  );
}


function OverviewCard({ C, icon, tint, label, value, hint, footer, progress, actionLabel, onAction }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: 10,
      padding: 18, background: C.surface,
      border: `1px solid ${C.border}`, borderRadius: 12,
      boxShadow: "0 1px 2px rgba(16,24,40,.04)",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: C.textLight, letterSpacing: 0.4, textTransform: "uppercase" }}>{label}</span>
        <span style={{ width: 32, height: 32, borderRadius: 10, background: tint.bg, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <BIcon n={icon} s={15} c={tint.color} />
        </span>
      </div>
      <div>
        <p style={{ fontSize: 22, fontWeight: 800, color: C.text, lineHeight: 1.1 }}>{value}</p>
        {hint && <p style={{ fontSize: 12, color: C.textSub, marginTop: 4 }}>{hint}</p>}
      </div>
      {progress !== undefined && (
        <div style={{ height: 6, background: C.borderLight, borderRadius: 4, overflow: "hidden" }}>
          <div style={{
            height: "100%", width: `${progress}%`,
            background: progress > 90 ? C.red : progress > 75 ? C.amber : tint.color,
            borderRadius: 4, transition: "width .4s",
          }} />
        </div>
      )}
      {footer && <p style={{ fontSize: 11, color: C.textLight }}>{footer}</p>}
      {actionLabel && (
        <button onClick={onAction} style={{
          marginTop: 4, alignSelf: "flex-start",
          background: "transparent", border: "none", padding: 0,
          color: tint.color, fontWeight: 700, fontSize: 12,
          cursor: "pointer", fontFamily: "inherit",
        }}>{actionLabel} →</button>
      )}
    </div>
  );
}


function Tag({ C, children }) {
  return (
    <span style={{ fontSize: 10.5, fontWeight: 600, color: C.textSub, background: C.surfaceAlt, border: `1px solid ${C.borderLight}`, padding: "3px 8px", borderRadius: 6 }}>
      {children}
    </span>
  );
}


function Toast({ C, toast }) {
  if (!toast) return null;
  const tint =
    toast.kind === "error" ? { color: C.red, bg: C.redBg, border: C.redBdr } :
    toast.kind === "info"  ? { color: C.brand, bg: C.brandBg, border: C.brandBdr } :
    { color: C.green, bg: C.greenBg, border: C.greenBdr };
  return (
    <div style={{
      position: "fixed", bottom: 24, right: 24, zIndex: 9999,
      padding: "12px 16px", borderRadius: 10, fontSize: 13, fontWeight: 600,
      background: tint.bg, color: tint.color, border: `1px solid ${tint.border}`,
      boxShadow: "0 8px 24px rgba(16,24,40,.12)", maxWidth: 360,
      animation: "billingPop .25s ease both",
    }}>{toast.msg}</div>
  );
}


// ── Modals ────────────────────────────────────────────────────────────────
function Modal({ C, title, subtitle, children, onClose, footer, maxWidth = 540 }) {
  return (
    <div className="zt-bill-modal-back" onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 9000, display: "flex",
      alignItems: "center", justifyContent: "center", padding: 16,
      animation: "billingFade .2s ease both",
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: C.surface, borderRadius: 14, width: "100%", maxWidth,
        boxShadow: "0 24px 60px rgba(16,24,40,.30)", overflow: "hidden",
        animation: "billingPop .25s ease both",
      }}>
        <header style={{ padding: "20px 24px 14px", borderBottom: `1px solid ${C.borderLight}` }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
            <div>
              <h3 style={{ fontSize: 17, fontWeight: 800, color: C.text }}>{title}</h3>
              {subtitle && <p style={{ fontSize: 13, color: C.textSub, marginTop: 4, lineHeight: 1.5 }}>{subtitle}</p>}
            </div>
            <button onClick={onClose} style={{
              border: "none", background: "transparent",
              color: C.textSub, padding: 4, cursor: "pointer", fontFamily: "inherit",
            }}><BIcon n="x" s={18} c={C.textSub} /></button>
          </div>
        </header>
        <div style={{ padding: "18px 24px" }}>{children}</div>
        {footer && (
          <footer style={{ padding: "14px 24px", borderTop: `1px solid ${C.borderLight}`, background: C.surfaceAlt, display: "flex", justifyContent: "flex-end", gap: 10 }}>
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}


function UpgradeConfirmModal({ C, plan, cycle, subscription, onClose, onConfirm, tr }) {
  const seats = subscription?.active_users || 0;
  const perUser = cycle === "annual" ? plan.annual_price_per_user / 12 : plan.monthly_price_per_user;
  const subtotal = (perUser * Math.max(seats, 1)).toFixed(2);
  return (
    <Modal C={C}
      title={`${tr("billingUpgradeTo","Upgrade to")} ${plan.name}`}
      subtitle={tr("billingUpgradeSubtitle","Changes apply immediately. Stripe will pro-rate the difference and reflect it on your next invoice.")}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose} style={btnGhost(C)}>{tr("billingCancel","Cancel")}</button>
          <button onClick={onConfirm} style={btnPrimary(C)}>
            <BIcon n="check" s={13} c="#fff" strokeWidth={2.6} />
            {tr("billingConfirmUpgrade","Confirm Upgrade")}
          </button>
        </>
      }>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
        <KV C={C} label={tr("billingCurrentPlanLabel","Current plan")} value={subscription?.plan?.name || "—"} />
        <KV C={C} label={tr("billingNewPlan","New plan")} value={plan.name} />
        <KV C={C} label={tr("billingCycle","Cycle")} value={titleCase(cycle)} />
        <KV C={C} label={tr("billingActiveUsersLabel","Active users")} value={seats} />
      </div>
      <div style={{ padding: 14, borderRadius: 10, background: C.brandBg, border: `1px solid ${C.brandBdr}` }}>
        <p style={{ fontSize: 12, fontWeight: 700, color: C.brand, letterSpacing: 0.3, textTransform: "uppercase" }}>
          {tr("billingEstimatedTotal","Estimated total")}
        </p>
        <p style={{ fontSize: 22, fontWeight: 800, color: C.brand, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>
          {fmtMoney(subtotal, plan.currency)} <span style={{ fontSize: 12, fontWeight: 600, color: C.textSub }}>/ {cycle === "annual" ? tr("billingMonth","month") : tr("billingMonth","month")}</span>
        </p>
        <p style={{ fontSize: 12, color: C.textSub, marginTop: 6 }}>
          {tr("billingTaxNote","Prices shown are final. No additional tax is applied.")}
        </p>
      </div>
    </Modal>
  );
}


function DowngradeReviewModal({ C, plan, onClose, onSubmit, tr }) {
  const [form, setForm] = useState({ reason: "", effective: "", acknowledged: false });
  const minLen = 20;
  return (
    <Modal C={C}
      title={tr("billingDowngradeTitle","Talk to us about downgrading")}
      subtitle={tr("billingDowngradeSubtitle","Downgrading may affect governance, audit, retention, reporting, SSO, and evidence access. Downgrades are not automatic — your request is recorded and must be actioned by your account manager.")}
      onClose={onClose}
      maxWidth={560}
      footer={
        <>
          <button onClick={onClose} style={btnGhost(C)}>{tr("billingCancel","Cancel")}</button>
          <button onClick={() => onSubmit(form)} style={btnPrimary(C)}>
            <BIcon n="check" s={13} c="#fff" strokeWidth={2.6} />
            {tr("billingSubmitRequest","Submit Request")}
          </button>
        </>
      }>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <KV C={C} label={tr("billingTargetPlan","Target plan")} value={plan.name} />
        <Field C={C} label={`${tr("billingReason","Reason")} ${tr("billingMin20Chars","(min 20 characters)")}`} required>
          <textarea
            className="zt-bill-textarea"
            value={form.reason}
            onChange={(e) => setForm(f => ({ ...f, reason: e.target.value }))}
            rows={4}
            style={{ ...inputStyle(C), minHeight: 96, resize: "vertical" }}
            placeholder={tr("billingReasonPlaceholder","Tell us a bit about why you want to downgrade — what's changing, when, and what you'd like preserved.")}
          />
          <p style={{ fontSize: 11, color: form.reason.length < minLen ? C.amber : C.textLight, marginTop: 4 }}>
            {form.reason.length} / {minLen}+ {tr("billingChars","characters")}
          </p>
        </Field>
        <Field C={C} label={tr("billingPreferredEffective","Preferred effective date")}>
          <input type="date" className="zt-bill-input"
            value={form.effective}
            onChange={(e) => setForm(f => ({ ...f, effective: e.target.value }))}
            style={inputStyle(C)} />
        </Field>
        <label style={{ display: "flex", gap: 10, alignItems: "flex-start", fontSize: 13, color: C.textSub, lineHeight: 1.5 }}>
          <input type="checkbox" checked={form.acknowledged}
            onChange={(e) => setForm(f => ({ ...f, acknowledged: e.target.checked }))}
            style={{ marginTop: 3 }} />
          {tr("billingDowngradeAck","I understand this may affect available features, retention, and compliance posture.")}
        </label>
        <div style={{ padding: 12, borderRadius: 8, background: C.amberBg, border: `1px solid ${C.amberBdr}`, fontSize: 12, color: C.amber, fontWeight: 600 }}>
          {tr("billingDowngradeSla","We'll review your request within one business day.")}
        </div>
      </div>
    </Modal>
  );
}


function EnterpriseQuoteModal({ C, onClose, onSubmit, defaults = {}, tr }) {
  const [form, setForm] = useState({
    company_name: defaults.company_name || "",
    work_email:   defaults.work_email   || "",
    user_count:   "",
    deployment_preference: "SaaS",
    required_integrations: "",
    compliance_requirements: "",
    notes: "",
  });
  const submit = () => {
    if (!form.company_name || !form.work_email || !form.user_count) return;
    onSubmit(form);
  };
  return (
    <Modal C={C}
      title={tr("billingEnterpriseTitle","Request Enterprise Quote")}
      subtitle={tr("billingEnterpriseSubtitle","Enterprise pricing depends on deployment, residency, integrations, and SLAs. We'll respond within one business day.")}
      onClose={onClose}
      maxWidth={620}
      footer={
        <>
          <button onClick={onClose} style={btnGhost(C)}>{tr("billingCancel","Cancel")}</button>
          <button onClick={submit} style={btnPrimary(C)}>
            <BIcon n="check" s={13} c="#fff" strokeWidth={2.6} />
            {tr("billingSubmitRequest","Submit Request")}
          </button>
        </>
      }>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field C={C} label={tr("billingCompanyName","Company name")} required>
          <input className="zt-bill-input" value={form.company_name}
            onChange={(e) => setForm(f => ({ ...f, company_name: e.target.value }))}
            style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingWorkEmail","Work email")} required>
          <input type="email" className="zt-bill-input" value={form.work_email}
            onChange={(e) => setForm(f => ({ ...f, work_email: e.target.value }))}
            style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingNumberOfUsers","Number of users")} required>
          <select className="zt-bill-select" value={form.user_count}
            onChange={(e) => setForm(f => ({ ...f, user_count: e.target.value }))}
            style={inputStyle(C)}>
            <option value="">{tr("billingSelectRange","Select range…")}</option>
            <option>100 – 250</option>
            <option>250 – 500</option>
            <option>500 – 1,000</option>
            <option>1,000 – 5,000</option>
            <option>5,000+</option>
          </select>
        </Field>
        <Field C={C} label={tr("billingDeployment","Deployment preference")} required>
          <select className="zt-bill-select" value={form.deployment_preference}
            onChange={(e) => setForm(f => ({ ...f, deployment_preference: e.target.value }))}
            style={inputStyle(C)}>
            {["SaaS","Private Cloud","On-Premise","Not Sure"].map(o => <option key={o}>{o}</option>)}
          </select>
        </Field>
        <Field C={C} label={tr("billingIntegrations","Required integrations")} span={2}>
          <textarea className="zt-bill-textarea" rows={2} value={form.required_integrations}
            onChange={(e) => setForm(f => ({ ...f, required_integrations: e.target.value }))}
            style={{ ...inputStyle(C), resize: "vertical" }}
            placeholder="Workday, SAP, Oracle, ADP, Jira, Slack…" />
        </Field>
        <Field C={C} label={tr("billingComplianceReqs","Compliance requirements")} span={2}>
          <textarea className="zt-bill-textarea" rows={2} value={form.compliance_requirements}
            onChange={(e) => setForm(f => ({ ...f, compliance_requirements: e.target.value }))}
            style={{ ...inputStyle(C), resize: "vertical" }}
            placeholder="SOC 2, GDPR, HIPAA, SOX, WTD, FLSA, legal hold…" />
        </Field>
        <Field C={C} label={tr("billingAnythingElse","Anything else")} span={2}>
          <textarea className="zt-bill-textarea" rows={2} value={form.notes}
            onChange={(e) => setForm(f => ({ ...f, notes: e.target.value }))}
            style={{ ...inputStyle(C), resize: "vertical" }} />
        </Field>
      </div>
    </Modal>
  );
}


function ContactFormModal({ C, value, onClose, onSubmit, tr }) {
  const [form, setForm] = useState({
    name:                    value.name                    || "",
    email:                   value.email                   || "",
    role:                    value.role                    || "Billing Admin",
    receives_invoices:       value.receives_invoices       ?? true,
    receives_payment_alerts: value.receives_payment_alerts ?? true,
    receives_renewal_notices: value.receives_renewal_notices ?? true,
    receives_tax_documents:  value.receives_tax_documents  ?? false,
    is_primary:              value.is_primary              ?? false,
  });
  return (
    <Modal C={C}
      title={value.id ? tr("billingEditContact","Edit billing contact") : tr("billingAddContactTitle","Add billing contact")}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose} style={btnGhost(C)}>{tr("billingCancel","Cancel")}</button>
          <button onClick={() => onSubmit(form)} style={btnPrimary(C)}>
            <BIcon n="check" s={13} c="#fff" strokeWidth={2.6} />
            {tr("billingSave","Save")}
          </button>
        </>
      }>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field C={C} label={tr("billingContactName","Name")} required>
          <input className="zt-bill-input" value={form.name}
            onChange={(e) => setForm(f => ({ ...f, name: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingContactEmail","Email")} required>
          <input type="email" className="zt-bill-input" value={form.email}
            onChange={(e) => setForm(f => ({ ...f, email: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingContactRole","Role")} span={2}>
          <select className="zt-bill-select" value={form.role}
            onChange={(e) => setForm(f => ({ ...f, role: e.target.value }))} style={inputStyle(C)}>
            {["Account Owner","Billing Admin","Finance","Procurement","External Accountant"].map(o => <option key={o}>{o}</option>)}
          </select>
        </Field>
      </div>
      <p style={{ fontSize: 12, fontWeight: 700, color: C.textLight, letterSpacing: 0.4, textTransform: "uppercase", marginTop: 14, marginBottom: 6 }}>
        {tr("billingNotifications","Notifications")}
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        {[
          ["receives_invoices",        tr("billingNotifInvoices",  "Invoices")],
          ["receives_payment_alerts",  tr("billingNotifPayments",  "Payment alerts")],
          ["receives_renewal_notices", tr("billingNotifRenewals",  "Renewal notices")],
          ["receives_tax_documents",   tr("billingNotifTax",       "Tax documents")],
        ].map(([k, l]) => (
          <label key={k} style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, color: C.textSub, padding: "8px 10px", borderRadius: 8, border: `1px solid ${C.borderLight}` }}>
            <input type="checkbox" checked={!!form[k]} onChange={(e) => setForm(f => ({ ...f, [k]: e.target.checked }))} />
            {l}
          </label>
        ))}
      </div>
      <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, color: C.textSub, marginTop: 12 }}>
        <input type="checkbox" checked={!!form.is_primary}
          onChange={(e) => setForm(f => ({ ...f, is_primary: e.target.checked }))} />
        {tr("billingMakePrimary","Make this the primary billing contact")}
      </label>
    </Modal>
  );
}


// PaymentMethodModal deleted in Phase 7: cards are collected on Stripe's hosted
// Setup Session, so Temveris has no card form and never sees a card number.

function TaxProfileForm({ C, value, onSave, tr }) {
  const [form, setForm] = useState({
    legal_entity_name: value.legal_entity_name || "",
    trading_name:      value.trading_name      || "",
    billing_address:   value.billing_address   || "",
    city:              value.city              || "",
    state:             value.state             || "",
    postal_code:       value.postal_code       || "",
    country:           value.country           || "",
    tax_id_label:      value.tax_id_label      || "VAT",
    tax_id_value:      value.tax_id_value      || "",
    tax_exempt:        !!value.tax_exempt,
    invoice_currency:  value.invoice_currency  || "USD",
  });
  // The parent re-creates `taxProfile || {}` on every render, so depending
  // on `value` directly caused this effect to fire on every parent update
  // and wipe whatever the admin was typing. Key on the field values so we
  // only re-sync when the loaded profile actually changes.
  const valueKey = JSON.stringify({
    a: value.legal_entity_name, b: value.trading_name, c: value.billing_address,
    d: value.city, e: value.state, f: value.postal_code, g: value.country,
    h: value.tax_id_label, i: value.tax_id_value, j: value.tax_exempt, k: value.invoice_currency,
  });
  useEffect(() => {
    setForm({
      legal_entity_name: value.legal_entity_name || "",
      trading_name:      value.trading_name      || "",
      billing_address:   value.billing_address   || "",
      city:              value.city              || "",
      state:             value.state             || "",
      postal_code:       value.postal_code       || "",
      country:           value.country           || "",
      tax_id_label:      value.tax_id_label      || "VAT",
      tax_id_value:      value.tax_id_value      || "",
      tax_exempt:        !!value.tax_exempt,
      invoice_currency:  value.invoice_currency  || "USD",
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [valueKey]);

  return (
    <SectionCard C={C}
      title={tr("billingTaxTitle","Tax Details")}
      subtitle={tr("billingTaxSubtitle","Used to generate compliant invoices and calculate tax via Stripe Tax based on your billing address.")}
      action={
        <button onClick={() => onSave(form)} style={btnPrimary(C)}>
          <BIcon n="check" s={13} c="#fff" strokeWidth={2.6} />
          {tr("billingSaveTax","Save Tax Details")}
        </button>
      }>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field C={C} label={tr("billingLegalEntity","Legal entity name")} required>
          <input className="zt-bill-input" value={form.legal_entity_name}
            onChange={(e) => setForm(f => ({ ...f, legal_entity_name: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingTradingName","Trading name")}>
          <input className="zt-bill-input" value={form.trading_name}
            onChange={(e) => setForm(f => ({ ...f, trading_name: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingAddress","Billing address")} span={2} required>
          <input className="zt-bill-input" value={form.billing_address}
            onChange={(e) => setForm(f => ({ ...f, billing_address: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingCity","City")}>
          <input className="zt-bill-input" value={form.city}
            onChange={(e) => setForm(f => ({ ...f, city: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingState","State / region")}>
          <input className="zt-bill-input" value={form.state}
            onChange={(e) => setForm(f => ({ ...f, state: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingPostal","Postal code")}>
          <input className="zt-bill-input" value={form.postal_code}
            onChange={(e) => setForm(f => ({ ...f, postal_code: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingCountry","Country")} required>
          <input className="zt-bill-input" value={form.country}
            onChange={(e) => setForm(f => ({ ...f, country: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingTaxIdLabel","Tax ID type")}>
          <select className="zt-bill-select" value={form.tax_id_label}
            onChange={(e) => setForm(f => ({ ...f, tax_id_label: e.target.value }))} style={inputStyle(C)}>
            {["VAT","EIN","GST","ABN","Other"].map(o => <option key={o}>{o}</option>)}
          </select>
        </Field>
        <Field C={C} label={tr("billingTaxIdValue","Tax ID")}>
          <input className="zt-bill-input" value={form.tax_id_value}
            onChange={(e) => setForm(f => ({ ...f, tax_id_value: e.target.value }))} style={inputStyle(C)} />
        </Field>
        <Field C={C} label={tr("billingInvoiceCurrency","Invoice currency")}>
          <select className="zt-bill-select" value={form.invoice_currency}
            onChange={(e) => setForm(f => ({ ...f, invoice_currency: e.target.value }))} style={inputStyle(C)}>
            {["USD","EUR","GBP","INR","AUD","CAD","SGD"].map(o => <option key={o}>{o}</option>)}
          </select>
        </Field>
        <Field C={C} label={tr("billingTaxExempt","Tax-exempt status")}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: C.textSub, height: "100%" }}>
            <input type="checkbox" checked={form.tax_exempt}
              onChange={(e) => setForm(f => ({ ...f, tax_exempt: e.target.checked }))} />
            {tr("billingTaxExemptHint","This organization is tax-exempt")}
          </label>
        </Field>
      </div>
      <p style={{ fontSize: 12, color: C.textLight, marginTop: 14 }}>
        {tr("billingTaxFooter","Taxes are calculated based on billing address and applicable rules at invoice generation.")}
      </p>
    </SectionCard>
  );
}


function Field({ C, label, required, span, children }) {
  return (
    <div style={{ gridColumn: span ? `span ${span}` : "auto", display: "flex", flexDirection: "column", gap: 4 }}>
      <label style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
        {label}{required && <span style={{ color: C.red, marginLeft: 4 }}>*</span>}
      </label>
      {children}
    </div>
  );
}


function KV({ C, label, value }) {
  return (
    <div style={{ padding: 12, borderRadius: 8, background: C.surfaceAlt, border: `1px solid ${C.borderLight}` }}>
      <p style={{ fontSize: 11, fontWeight: 700, color: C.textLight, letterSpacing: 0.4, textTransform: "uppercase" }}>{label}</p>
      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, marginTop: 4 }}>{value}</p>
    </div>
  );
}


// ── Style helpers ────────────────────────────────────────────────────────
const btnPrimary = (C) => ({
  display: "inline-flex", alignItems: "center", gap: 8,
  padding: "10px 18px", borderRadius: 10,
  background: C.brand, color: "#fff", border: "none",
  fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit",
  boxShadow: "0 4px 12px rgba(17,73,106,.25)",
  transition: "transform .14s, box-shadow .18s",
});

const btnGhost = (C) => ({
  display: "inline-flex", alignItems: "center", gap: 8,
  padding: "9px 16px", borderRadius: 10,
  background: C.surface, color: C.brand,
  border: `1px solid ${C.border}`,
  fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit",
  transition: "border-color .18s, background .18s, transform .14s",
});

const iconBtn = (C) => ({
  display: "inline-flex", alignItems: "center", justifyContent: "center",
  width: 32, height: 32, borderRadius: 8,
  background: C.surface, color: C.brand,
  border: `1px solid ${C.border}`,
  cursor: "pointer", fontFamily: "inherit",
  transition: "border-color .18s, background .18s",
});

const inputStyle = (C) => ({
  width: "100%", padding: "10px 12px", borderRadius: 8,
  background: C.surface, color: C.text,
  border: `1px solid ${C.border}`,
  fontSize: 13, fontFamily: "inherit",
  outline: "none",
});

const thStyle = (C) => ({
  padding: "12px 18px", textAlign: "left",
  fontSize: 11, fontWeight: 700, color: C.textSub,
  letterSpacing: 0.4, textTransform: "uppercase",
  borderBottom: `1px solid ${C.borderLight}`,
});

const tdStyle = (C) => ({
  padding: "14px 18px", fontSize: 13, color: C.text, verticalAlign: "middle",
});
