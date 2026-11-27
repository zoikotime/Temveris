import { lazy, Suspense, useEffect, useState } from "react";
import {
  BrowserRouter as Router,
  Routes,
  Route,
  Navigate,
  useLocation,
} from "react-router-dom";
import PageTransition from "./components/PageTransition";
import ErrorBoundary from "./components/ErrorBoundary";
import OfflineBanner from "./components/OfflineBanner";
import NetworkInterruptOverlay from "./components/NetworkInterruptOverlay";
import { LanguageProvider } from "./context/LanguageContext";
import { ThemeProvider } from "./context/ThemeContext";
import { ConnectionProvider } from "./context/ConnectionContext";
import { AuthProvider } from "./context/AuthContext";
import { WorkspaceProvider } from "./context/WorkspaceContext";
import { EntitlementsProvider } from "./context/EntitlementsContext";
import { readToken, readMirror } from "./api/authStorage";
import { API_BASE } from "./config";
import ImpersonationBanner from "./components/ImpersonationBanner";
import BillingOverdueBanner from "./components/BillingOverdueBanner";
// Eager on purpose: this is the chrome that has to stay on screen while the
// lazy auth pages swap underneath it. Code-splitting it would put the shell
// itself behind the fallback it exists to avoid.
import AuthShell from "./components/auth/AuthShell";
// Eager: a tiny route guard the authenticated guards wrap their children in, so
// an unverified self-serve signup cannot reach the product by navigating around
// the verify step.
import VerifiedGate from "./components/VerifiedGate";

// Lazy-load every route so first paint only ships the chunk the user actually needs.
// This is the single biggest win for "time to interactive" on the login screen,
// since the admin dashboard alone pulls in recharts (~300 KB gz).
const Login           = lazy(() => import("./pages/Login"));
const ForgotPassword  = lazy(() => import("./pages/ForgotPassword"));
const VerifyOtp       = lazy(() => import("./pages/VerifyOtp"));
const SetPassword     = lazy(() => import("./pages/SetPassword"));
const ResetPassword   = lazy(() => import("./pages/ResetPassword"));
const Dashboard       = lazy(() => import("./pages/Dashboard"));
const ElectronDashboard = lazy(() => import("./pages/ElectronDashboard"));
const AdminDashboard  = lazy(() => import("./pages/AdminDashboard"));
const MyTeam          = lazy(() => import("./pages/MyTeam"));
const InviteSignup    = lazy(() => import("./pages/InviteSignup"));
const LeavePage       = lazy(() => import("./pages/LeavePage"));
const AdminLeave      = lazy(() => import("./pages/AdminLeave"));
const SettingsPage    = lazy(() => import("./pages/SettingsPage"));
const LanguagePage    = lazy(() => import("./pages/LanguagePage"));
const MyWorkingTime   = lazy(() => import("./pages/MyWorkingTime"));
const AccountSettings = lazy(() => import("./pages/AccountSettings"));
const Profile         = lazy(() => import("./pages/Profile"));
const SuperAdmin      = lazy(() => import("./pages/SuperAdmin"));

// Commercial Activation System (Tier-0 spec v3.0)
const Start            = lazy(() => import("./pages/Start"));
const WorkspaceActivation = lazy(() => import("./pages/WorkspaceActivation"));
const SubscriptionConfigure = lazy(() => import("./pages/SubscriptionConfigure"));
const SubscriptionConfirmed = lazy(() => import("./pages/SubscriptionConfirmed"));
const VerifyEmail      = lazy(() => import("./pages/VerifyEmail"));
const Onboarding       = lazy(() => import("./pages/Onboarding"));
const OrganizationSetup = lazy(() => import("./pages/OrganizationSetup"));
const LinuxInstallGuide = lazy(() => import("./pages/LinuxInstallGuide"));
const MacInstallGuide   = lazy(() => import("./pages/MacInstallGuide"));
const DownloadApp       = lazy(() => import("./pages/DownloadApp"));
const ContactSales     = lazy(() => import("./pages/ContactSales"));
const CommercialAdmin  = lazy(() => import("./pages/CommercialAdmin"));
const SsoCallback      = lazy(() => import("./pages/SsoCallback"));

// Public unsubscribe landing (no auth) — backs the List-Unsubscribe link.
const Unsubscribe      = lazy(() => import("./pages/Unsubscribe"));

// People → Offboarding admin section (lifecycle + security + governance UI).
const AdminOffboarding = lazy(() => import("./pages/AdminOffboarding"));

// Former Worker Portal — strictly isolated; uses its own axios instance and
// its own session token. NEVER wrapped in <AdminRoute> / <PrivateRoute>.
const FormerWorkerPortalRoutes = lazy(() => import("./pages/FormerWorkerPortal"));

// `readToken` / `readMirror` look in BOTH localStorage and sessionStorage so the
// "Remember this device" choice on the login page (which decides the storage
// tier) is honoured by these route guards without duplicating the logic here.
const PrivateRoute = ({ children }) => {
  const token = readToken();
  const role  = readMirror("role");
  if (!token) return <Navigate to="/" replace />;
  if (role === "super_admin") return <Navigate to="/super-admin" replace />;
  return <VerifiedGate>{children}</VerifiedGate>;
};

const AdminRoute = ({ children }) => {
  const token = readToken();
  const role  = readMirror("role");

  if (!token) return <Navigate to="/" replace />;
  // super_admin has their own portal — never drop them into the tenant admin view
  if (!["admin", "hr"].includes(role)) return <Navigate to="/dashboard" replace />;

  return <VerifiedGate>{children}</VerifiedGate>;
};

/**
 * Sends an admin whose organization is mid-setup to the wizard.
 *
 * The server is the only authority on whether setup is outstanding, and it
 * answers "no" unless the organization explicitly started the wizard. That
 * direction matters: every pre-existing tenant — super-admin-provisioned or
 * self-serve from before this feature — has no wizard state, so a working admin
 * can never be dropped into a setup flow for an organization they already run.
 *
 * Fails OPEN on every uncertainty (still loading, request failed, no answer):
 * being wrong here means blocking a legitimate admin from their dashboard, so
 * the default must be to let them through. This is a navigation convenience,
 * not a security control — the backend enforces tenancy on every call
 * regardless of where the UI routes.
 *
 * PERF: "still loading" is treated as fail-open too, so children render while
 * the check is in flight instead of behind a spinner. That matters far more
 * than it looks: while this component returned a fallback, <AdminDashboard/>
 * was never rendered, so React.lazy never fired and the browser did not even
 * BEGIN downloading the dashboard chunk until this request came back. The
 * request and the chunk download now run concurrently. Redirect behaviour is
 * unchanged — an org that really is mid-setup still goes to /setup the moment
 * the server says so, and /setup is guarded by AdminRoute (not this gate), so
 * there is still no loop.
 */
const SetupGate = ({ children }) => {
  // `null` means "not answered yet". With no token there is nothing to ask, so
  // that case is resolved in the initializer rather than by a setState inside
  // the effect (which would cause a cascading render).
  const [required, setRequired] = useState(() => (readToken() ? null : false));
  const { pathname } = useLocation();

  useEffect(() => {
    const token = readToken();
    if (!token) return;

    let cancelled = false;
    fetch(`${API_BASE}/api/v1/onboarding/setup`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setRequired(Boolean(d && d.setup_required)); })
      .catch(() => { if (!cancelled) setRequired(false); });
    return () => { cancelled = true; };
  }, [pathname]);

  // `required === null` (still in flight) falls through to `children` — see the
  // PERF note above. Only a definite `true` from the server redirects.
  //
  // Sends the admin to the ACTIVATION flow, not to the old /setup wizard. The
  // four-step flow (Account → Verify → Workspace → Ready) is what collects
  // organisation details now, and its Step 2 closes the wizard state on
  // submit. An admin who abandoned activation half-way and later opened /admin
  // used to be dropped into the old three-step wizard instead — a surface the
  // new flow replaced, showing a Teams screen the new flow does not have.
  //
  // /setup itself is left routed so anyone already mid-wizard when this shipped
  // can still finish; nothing navigates there automatically any more.
  if (required) return <Navigate to="/verify-email" replace />;
  return children;
};

const SuperAdminRoute = ({ children }) => {
  const token = readToken();
  const role  = readMirror("role");

  if (!token) return <Navigate to="/" replace />;
  if (role !== "super_admin") return <Navigate to="/dashboard" replace />;

  return children;
};

// Workspace Settings sub-route alias map.
//
// Per the Workspace Settings Navigation Naming Specification, the stable
// sub-routes are `/settings/workspace-profile`, `/settings/departments-teams`,
// and `/settings/workers-users`. Existing internal tab keys
// (`workspace-profile`, `manage-teams`, `manage-employees`) stay unchanged
// inside SettingsPage so we don't break deep-linked bookmarks or analytics.
//
// The map below covers the renamed slugs PLUS the legacy slugs PLUS the
// synonym list from §5 of the spec (employees, staff, workers, users, people,
// teams, departments) so global search can land users on the right tab.
export const SETTINGS_SLUG_MAP = {
  "workspace-profile":  "workspace-profile",
  "organization-settings": "organization-settings",
  "departments-teams":  "manage-teams",
  "workers-users":      "manage-employees",
  "roles-permissions":  "roles-permissions",
  "policies":           "policies",
  "leave-balance":      "leave-balance",
  "leave-balances":     "leave-balance",
  "balances":           "leave-balance",
  "public-holidays":    "public-holidays",
  "time-adjustments":   "time-adjustments",
  "time-requests":      "time-adjustments",   // alt phrasing for the same tab
  "billing-subscription": "billing-subscription",
  "billing":            "billing-subscription",
  "subscription":       "billing-subscription",
  "security":           "security",
  "connected-systems":  "connected-systems",
  "integrations":       "connected-systems",
  "audit-compliance":   "audit-compliance",
  "audit":              "audit-compliance",
  "compliance":         "audit-compliance",
  // Synonyms (search lands users here per §5 acceptance criterion 8)
  "employees":          "manage-employees",
  "staff":              "manage-employees",
  "workers":            "manage-employees",
  "users":              "manage-employees",
  "people":             "manage-employees",
  "teams":              "manage-teams",
  "departments":        "manage-teams",
  "roles":              "roles-permissions",
  "permissions":        "roles-permissions",
  // Legacy slugs — preserved with redirect semantics for ≥12 months per spec
  "manage-teams":       "manage-teams",
  "manage-employees":   "manage-employees",
  "platform-settings":  "workspace-profile",
  // Other Workspace Settings tabs that already exist inside SettingsPage
  "rebrand":            "workspace-profile",
  "branding":           "workspace-profile",
  "track-settings":     "policies",
  "risk-user":          "policies",
  "leave-settings":     "policies",
  "notifications":      "security",
};

// Legacy /platform-settings → /settings/workspace-profile redirect,
// query params preserved per spec §5 "Routes" implementation rule.
const PlatformSettingsRedirect = () => {
  const { search, hash } = useLocation();
  return <Navigate to={`/settings/workspace-profile${search}${hash}`} replace />;
};

const RouteFallback = () => (
  <div style={{
    position: "fixed", inset: 0, display: "flex",
    alignItems: "center", justifyContent: "center",
    background: "#fff", color: "#11496A",
    fontFamily: "'DM Sans', sans-serif", fontWeight: 600,
  }}>
    <div style={{
      width: 36, height: 36, borderRadius: "50%",
      border: "3px solid #E7F0F6", borderTopColor: "#11496A",
      animation: "zt-spin 0.8s linear infinite",
    }} />
    <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
  </div>
);

export default function App() {
  return (
    <ThemeProvider>
    <LanguageProvider>
    <ConnectionProvider>
    <AuthProvider>
    <WorkspaceProvider>
    <EntitlementsProvider>
    <OfflineBanner />
    <NetworkInterruptOverlay />
    <ErrorBoundary>
      <Router>
        <ImpersonationBanner />
        {/* Explains an HTTP 402 (organization read-only for non-payment) and offers
            the route to fix it. Inside <Router> because it navigates; alongside
            ImpersonationBanner because both are cross-cutting notices rather than
            route content. Renders nothing until a write is actually refused. */}
        <BillingOverdueBanner />
        <PageTransition>
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              {/* Auth. These routes share one persistent shell so switching
                  tabs swaps only the form — see AuthShell. `/start` is NOT in
                  this group: it is Step 1 of the four-step activation flow and
                  owns the full page (progress indicator + plan summary), which
                  does not fit inside the shell's single 440px card. The Sign Up
                  tab still points at it. */}
              <Route element={<AuthShell />}>
                <Route path="/"                   element={<Login />} />
                <Route path="/forgot-password"    element={<ForgotPassword />} />
              </Route>
              <Route path="/verify-otp"           element={<VerifyOtp />} />
              <Route path="/reset-password"       element={<ResetPassword />} />
              <Route path="/set-password/:token"  element={<SetPassword />} />
              {/* Password reset is OTP-only now. Any legacy token-based reset
                  link still in an inbox is redirected to the OTP flow's first
                  step instead of rendering a dead token page. (This is the
                  password RESET link — distinct from the employee INVITE
                  /set-password/:token route above, which stays.) */}
              <Route path="/reset-password/:token" element={<Navigate to="/forgot-password" replace />} />

              {/* SSO redirect landing page — backend bounces here with
                  ?token=...&provider=...&remember=... or ?error=... */}
              <Route path="/sso/callback" element={<SsoCallback />} />

              {/* Public unsubscribe landing (no auth) — List-Unsubscribe link */}
              <Route path="/unsubscribe" element={<Unsubscribe />} />

              {/* Desktop install guides — public on purpose. An employee who has
                  just set their password may not be signed in yet, and support
                  links people straight here.
                  Linux and macOS need guides rather than plain downloads:
                  double-clicking a .deb opens Archive Manager on most Ubuntu
                  desktops, and a downloaded .dmg is often blocked by Gatekeeper
                  with a misleading "app is damaged" message. Windows downloads
                  directly — its installer needs no explanation. */}
              <Route path="/install/linux" element={<LinuxInstallGuide />} />
              <Route path="/install/mac"   element={<MacInstallGuide />} />
              {/* The per-OS download page the admin header links to. Public for
                  the same reason as the guides above — it is the page support
                  hands out, and it only offers installers, never data. */}
              <Route path="/download"      element={<DownloadApp />} />

              {/* Commercial Activation System (Tier-0 v3.0).
                  Step 1 Account → Step 2 Verify → Step 3 Workspace → Step 4 Ready. */}
              <Route path="/start"               element={<Start />} />
              <Route path="/workspace/preparing" element={<WorkspaceActivation />} />
              <Route path="/workspace/ready"     element={<WorkspaceActivation />} />
              <Route path="/verify-email"   element={<VerifyEmail />} />
              {/* Step 4 → checkout. Guarded: it configures the caller's own
                  subscription, and every price and limit on it is decided
                  server-side. */}
              <Route path="/subscription/configure"
                     element={<AdminRoute><SubscriptionConfigure /></AdminRoute>} />
              {/* Where Stripe returns after a successful Checkout. Guarded the
                  same way: confirming a session is a billing write. */}
              <Route path="/subscription/confirmed"
                     element={<AdminRoute><SubscriptionConfirmed /></AdminRoute>} />
              <Route path="/contact-sales"  element={<ContactSales />} />
              <Route path="/onboarding"     element={<PrivateRoute><Onboarding /></PrivateRoute>} />
              {/* Self-service organization setup wizard. AdminRoute (not
                  SetupGate) so a half-set-up admin can always reach it — the
                  gate redirects INTO here, so gating this route too would loop. */}
              <Route path="/setup"          element={<AdminRoute><OrganizationSetup /></AdminRoute>} />
              <Route path="/admin/commercial" element={<AdminRoute><CommercialAdmin /></AdminRoute>} />

              {/* Former Worker Portal */}
              <Route path="/former-worker/*" element={<FormerWorkerPortalRoutes />} />

              {/* People → Offboarding admin section */}
              <Route path="/admin/offboarding/*" element={<AdminRoute><AdminOffboarding /></AdminRoute>} />

              {/* Invite */}
              <Route path="/invite/:token" element={<InviteSignup />} />

              {/* Electron-only minimal route — locked-down view rendered inside the desktop shell */}
              <Route path="/electron" element={<PrivateRoute><ElectronDashboard /></PrivateRoute>} />

              {/* Employee routes */}
              <Route path="/dashboard" element={<PrivateRoute><Dashboard /></PrivateRoute>} />
              <Route path="/leave"     element={<PrivateRoute><LeavePage /></PrivateRoute>} />
              <Route path="/settings"  element={<PrivateRoute><SettingsPage /></PrivateRoute>} />
              {/* Workspace Settings stable sub-routes + legacy aliases
                  (see SETTINGS_SLUG_MAP above for the resolution rules). */}
              <Route path="/settings/:tab" element={<PrivateRoute><SettingsPage /></PrivateRoute>} />
              <Route path="/platform-settings" element={<PrivateRoute><PlatformSettingsRedirect /></PrivateRoute>} />
              <Route path="/platform-settings/*" element={<PrivateRoute><PlatformSettingsRedirect /></PrivateRoute>} />
              <Route path="/language"  element={<PrivateRoute><LanguagePage /></PrivateRoute>} />
              {/* WF-ECO-03 — the Worker's own ECO v2 day view. Deliberately
                  NOT in navigation: §45's OD-03 (the surface's user-visible
                  name) is unruled, and a menu entry would settle it by
                  accident. Reachable by direct URL until it is ruled. */}
              <Route path="/my-working-time" element={<PrivateRoute><MyWorkingTime /></PrivateRoute>} />
              <Route path="/account-settings" element={<PrivateRoute><AccountSettings /></PrivateRoute>} />
              <Route path="/profile" element={<PrivateRoute><Profile /></PrivateRoute>} />
              <Route path="/profile/:userId" element={<PrivateRoute><Profile /></PrivateRoute>} />

              {/* Admin / HR routes */}
              {/* SetupGate only on the admin landing route: it is where both
                  login and the wizard's own "go to dashboard" button send an
                  admin, so one checkpoint is enough. Gating every route would
                  add a request per navigation for no extra safety. */}
              <Route path="/admin"         element={<AdminRoute><SetupGate><AdminDashboard /></SetupGate></AdminRoute>} />
              <Route path="/admin/leave"   element={<AdminRoute><AdminLeave /></AdminRoute>} />
              <Route path="/my-team"       element={<AdminRoute><MyTeam /></AdminRoute>} />

              {/* Super Admin platform portal — completely separate from tenant admin */}
              <Route path="/super-admin"   element={<SuperAdminRoute><SuperAdmin /></SuperAdminRoute>} />
              <Route path="/super-admin/*" element={<SuperAdminRoute><SuperAdmin /></SuperAdminRoute>} />

              {/* Fallback */}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </PageTransition>
      </Router>
    </ErrorBoundary>
    </EntitlementsProvider>
    </WorkspaceProvider>
    </AuthProvider>
    </ConnectionProvider>
    </LanguageProvider>
    </ThemeProvider>
  );
}
