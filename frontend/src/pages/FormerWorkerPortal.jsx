/**
 * Former Worker Portal — isolated UI for the §1 portal.
 *
 * Hard rule: this page tree NEVER imports the admin layout, AdminSidebar,
 * AuthContext, or anything that would brand it as an active-worker surface.
 * It has its own minimal chrome and uses its own axios instance
 * (`formerWorkerPortal.js`) with its own session-scoped token. A signed-in
 * admin opening /former-worker sees a login screen — the two universes never
 * cross.
 *
 * Routes (mounted in App.jsx):
 *   /former-worker             → login
 *   /former-worker/verify      → MFA verify
 *   /former-worker/enroll      → first-run MFA enrollment
 *   /former-worker/home        → docs · expiry notice · support · DSAR · log
 *
 * The portal token is shown ONCE by the admin governance UI when the grant
 * is issued — the former worker pastes it on /former-worker.
 */
import React, { useEffect, useState } from "react";
import { Link, Routes, Route, useNavigate, Navigate } from "react-router-dom";
import {
  Download, Eye, FileText, Lock, LogOut, Mail, ShieldCheck, Ticket, Info,
} from "lucide-react";
import toast from "react-hot-toast";

import {
  portalLogin, portalVerifyMfa, portalEnrollMfa, portalEnrollMfaVerify,
  portalMe, portalListDocs, portalDownload, portalSupport, portalDsar,
  portalAccessLog, portalLogout, readPortalToken, writePortalToken,
  clearPortalToken,
} from "../api/formerWorkerPortal";
import { ActionButton, EmptyState, SectionHeading } from "../components/offboarding/primitives";

// ── Tiny portal-only chrome ──────────────────────────────────────────────
function PortalShell({ children, onSignOut }) {
  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <ShieldCheck className="text-slate-700" size={20} aria-hidden="true" />
            <span className="text-sm font-semibold text-slate-900">Temveris — Former Worker Portal</span>
          </div>
          {onSignOut && (
            <button
              type="button"
              onClick={onSignOut}
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100"
            >
              <LogOut size={14} aria-hidden="true" /> Sign out
            </button>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-3xl px-4 py-6">{children}</main>
      <footer className="mx-auto max-w-3xl px-4 pb-6 text-center text-[11px] text-slate-400">
        This portal is strictly limited to approved documents, support requests, and data-rights filings.
        It does not provide access to dashboards, analytics, conversations, or current-employee data.
      </footer>
    </div>
  );
}

// ── /former-worker (login) ──────────────────────────────────────────────
export function PortalLogin() {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await portalLogin(token);
      // Hold the pending-MFA session token in sessionStorage so the verify
      // screen can pick it up without re-typing. It is NOT the authenticated
      // bearer yet — that arrives after /verify succeeds.
      sessionStorage.setItem("zt_fw_pending_session", res.session_token);
      sessionStorage.setItem("zt_fw_portal_token_for_enroll", token); // for enroll fallback
      toast.success(`Verification code sent to your alternate email`);
      nav("/former-worker/verify");
    } catch (e) {
      const d = e?.response?.data?.detail;
      if (d?.code === "mfa_enrollment_required") {
        sessionStorage.setItem("zt_fw_portal_token_for_enroll", token);
        toast("MFA setup required first", { icon: "ℹ️" });
        nav("/former-worker/enroll");
      } else {
        toast.error(typeof d === "string" ? d : d?.message || "Login failed");
      }
    } finally { setBusy(false); }
  };

  return (
    <PortalShell>
      <h1 className="mb-1 text-xl font-semibold text-slate-900">Sign in</h1>
      <p className="mb-4 text-sm text-slate-600">
        Use the one-time portal token your former employer issued. After login,
        a verification code is sent to your alternate verified email.
      </p>
      <form onSubmit={submit} className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Portal token</span>
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="w-full rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200"
            aria-required="true"
            required
            minLength={10}
          />
        </label>
        <ActionButton type="submit" disabled={busy || token.length < 10}>
          {busy ? "Signing in…" : "Continue"}
        </ActionButton>
        <p className="text-[11px] text-slate-500">
          Don't have a token? Contact your former employer's HR team — tokens are issued once and never recoverable.
        </p>
      </form>
    </PortalShell>
  );
}

// ── /former-worker/verify (MFA) ────────────────────────────────────────
export function PortalVerify() {
  const [otp, setOtp] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const sessionToken = sessionStorage.getItem("zt_fw_pending_session") || "";

  if (!sessionToken) return <Navigate to="/former-worker" replace />;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await portalVerifyMfa(sessionToken, otp);
      if (res.status === "active") {
        writePortalToken(sessionToken);
        sessionStorage.removeItem("zt_fw_pending_session");
        toast.success("Signed in");
        nav("/former-worker/home");
      } else {
        toast.error("Verification did not complete");
      }
    } catch (e) {
      const d = e?.response?.data?.detail;
      toast.error(typeof d === "string" ? d : d?.message || "Invalid code");
    } finally { setBusy(false); }
  };

  return (
    <PortalShell>
      <h1 className="mb-1 text-xl font-semibold text-slate-900">Enter verification code</h1>
      <p className="mb-4 text-sm text-slate-600">
        A {String("6")}-digit code was just sent to your verified alternate email. It expires in a few minutes.
      </p>
      <form onSubmit={submit} className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Verification code</span>
          <input
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoFocus
            autoComplete="one-time-code"
            value={otp}
            onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 8))}
            className="w-32 rounded-md border border-slate-200 px-2 py-1.5 text-center text-base tracking-[0.4em] focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200"
            aria-required="true"
            required
            minLength={4}
          />
        </label>
        <ActionButton type="submit" disabled={busy || otp.length < 4}>
          {busy ? "Verifying…" : "Verify"}
        </ActionButton>
      </form>
    </PortalShell>
  );
}

// ── /former-worker/enroll (first-run MFA) ──────────────────────────────
export function PortalEnroll() {
  const [step, setStep] = useState("email");
  const [email, setEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [recovery, setRecovery] = useState(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const portalToken = sessionStorage.getItem("zt_fw_portal_token_for_enroll") || "";

  if (!portalToken) return <Navigate to="/former-worker" replace />;

  const sendOtp = async (e) => {
    e.preventDefault(); setBusy(true);
    try { await portalEnrollMfa(portalToken, email); toast.success("Verification code sent"); setStep("otp"); }
    catch (err) { toast.error(err?.response?.data?.detail?.message || err?.response?.data?.detail || "Failed"); }
    finally { setBusy(false); }
  };
  const verify = async (e) => {
    e.preventDefault(); setBusy(true);
    try {
      const res = await portalEnrollMfaVerify(portalToken, otp);
      setRecovery(res.recovery_code);
      toast.success("MFA enrolled");
      setStep("done");
    } catch (err) {
      toast.error(err?.response?.data?.detail?.message || err?.response?.data?.detail || "Failed");
    } finally { setBusy(false); }
  };

  return (
    <PortalShell>
      <h1 className="mb-1 text-xl font-semibold text-slate-900">Enroll multi-factor authentication</h1>
      <p className="mb-4 text-sm text-slate-600">
        Before we can sign you in, we need a verified alternate email to deliver one-time codes to.
      </p>
      {step === "email" && (
        <form onSubmit={sendOtp} className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Alternate email</span>
            <input
              type="email" required
              value={email} onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200"
              aria-required="true"
            />
          </label>
          <ActionButton type="submit" disabled={busy || !email.includes("@")}>
            {busy ? "Sending…" : "Send code"}
          </ActionButton>
        </form>
      )}
      {step === "otp" && (
        <form onSubmit={verify} className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Code from {email}</span>
            <input
              type="text" inputMode="numeric" pattern="[0-9]*" autoFocus autoComplete="one-time-code"
              value={otp} onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 8))}
              className="w-32 rounded-md border border-slate-200 px-2 py-1.5 text-center text-base tracking-[0.4em] focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200"
              aria-required="true"
              minLength={4}
            />
          </label>
          <ActionButton type="submit" disabled={busy || otp.length < 4}>
            {busy ? "Verifying…" : "Verify"}
          </ActionButton>
        </form>
      )}
      {step === "done" && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4">
          <h2 className="mb-2 text-sm font-semibold text-emerald-900">MFA enrolled</h2>
          <p className="mb-2 text-sm text-emerald-900">
            Save the recovery code below — it is shown ONCE and lets you recover access if you lose your alternate email.
          </p>
          <code className="block rounded bg-white px-2 py-1.5 font-mono text-sm tracking-wide">{recovery}</code>
          <div className="mt-3">
            <Link to="/former-worker" className="text-sm font-medium text-emerald-800 hover:underline">
              Back to sign in →
            </Link>
          </div>
        </div>
      )}
    </PortalShell>
  );
}

// ── /former-worker/home ────────────────────────────────────────────────
export function PortalHome() {
  const [me, setMe] = useState(null);
  const [docs, setDocs] = useState({ documents: [] });
  const [log, setLog] = useState([]);
  const [tab, setTab] = useState("documents");
  const nav = useNavigate();

  useEffect(() => {
    if (!readPortalToken()) { nav("/former-worker"); return; }
    portalMe().then(setMe).catch(() => nav("/former-worker"));
    portalListDocs().then(setDocs).catch(() => {});
    portalAccessLog().then(setLog).catch(() => {});
  }, [nav]);

  const signOut = async () => {
    try { await portalLogout(); } catch { /* ignore */ }
    clearPortalToken();
    nav("/former-worker");
  };

  if (!me) return <PortalShell><div className="text-sm text-slate-500" aria-live="polite">Loading…</div></PortalShell>;

  return (
    <PortalShell onSignOut={signOut}>
      <ExpiryNotice me={me} />

      <nav role="tablist" aria-label="Portal sections" className="mb-3 flex flex-wrap gap-1">
        {[
          { k: "documents",     label: "Documents",       icon: <FileText size={14} /> },
          { k: "support",       label: "Support",         icon: <Ticket size={14} /> },
          { k: "data_rights",   label: "Data rights",     icon: <ShieldCheck size={14} /> },
          { k: "access_log",    label: "Access log",      icon: <Eye size={14} /> },
        ].map((t) => (
          <button
            key={t.k}
            role="tab"
            aria-selected={tab === t.k}
            onClick={() => setTab(t.k)}
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium ring-1 ring-inset focus:outline-none focus:ring-2 ${
              tab === t.k
                ? "bg-slate-900 text-white ring-slate-900"
                : "bg-white text-slate-700 ring-slate-200 hover:bg-slate-50"
            }`}
          >
            <span aria-hidden="true">{t.icon}</span>
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "documents" && <DocumentsPanel docs={docs} />}
      {tab === "support" && <SupportPanel />}
      {tab === "data_rights" && <DataRightsPanel />}
      {tab === "access_log" && <AccessLogPanel log={log} />}
    </PortalShell>
  );
}

function ExpiryNotice({ me }) {
  const days = me?.days_remaining;
  const tone = days == null
    ? "bg-slate-50 text-slate-700 ring-slate-200"
    : days <= 14
      ? "bg-amber-50 text-amber-800 ring-amber-200"
      : "bg-emerald-50 text-emerald-800 ring-emerald-200";
  return (
    <section
      className={`mb-4 flex items-start gap-2 rounded-xl px-3 py-2 text-sm ring-1 ring-inset ${tone}`}
      aria-live="polite"
    >
      <Info size={16} className="mt-0.5" aria-hidden="true" />
      <div>
        <div className="font-medium">
          Access {days == null ? "has no expiry set" : days === 0 ? "expires today" : `expires in ${days} day${days === 1 ? "" : "s"}`}
          {me?.expires_at && ` (${new Date(me.expires_at).toLocaleDateString()})`}.
        </div>
        <div className="mt-0.5 text-xs">
          Scopes granted: {(me?.scopes || []).join(", ") || "—"}. Sessions auto-expire 30 minutes after sign-in.
        </div>
      </div>
    </section>
  );
}

function DocumentsPanel({ docs }) {
  const [busy, setBusy] = useState("");

  const download = async (scope, ref) => {
    setBusy(`${scope}:${ref}`);
    try {
      const res = await portalDownload(scope, ref);
      toast.success(`Download issued · ${res.download_url}`);
      // In a real deployment, this is where we'd window.open(res.download_url).
      // We deliberately do not auto-navigate; signing the URL is the worker's
      // explicit confirmation.
    } catch (e) {
      const status = e?.response?.status;
      const d = e?.response?.data?.detail;
      if (status === 429) {
        toast.error(d === "download_throttled" ? "Slow down — too many downloads" : "Daily download cap reached");
      } else if (status === 403) {
        toast.error(typeof d === "string" ? `Forbidden: ${d}` : "Forbidden");
      } else {
        toast.error("Download failed");
      }
    } finally { setBusy(""); }
  };

  return (
    <div className="space-y-2">
      <SectionHeading title="Approved documents" description="Only categories you were granted access to are listed. Other documents are not visible from this portal." />
      {(docs.documents || []).length === 0 && <EmptyState title="No documents in your granted scope" hint="Contact support if you expected something here." />}
      {(docs.documents || []).map((group) => (
        <section key={group.scope} className="rounded-xl border border-slate-200 bg-white p-3">
          <h3 className="mb-2 text-sm font-semibold text-slate-800">{labelFor(group.scope)}</h3>
          {(group.documents || []).length === 0 ? (
            <EmptyState title="Nothing yet" />
          ) : (
            <ul className="divide-y divide-slate-100">
              {group.documents.map((d) => (
                <li key={d.ref} className="flex items-center justify-between py-2">
                  <span className="text-sm text-slate-900">{d.ref}</span>
                  <ActionButton
                    variant="subtle"
                    onClick={() => download(group.scope, d.ref)}
                    disabled={busy === `${group.scope}:${d.ref}`}
                    aria-label={`Download ${d.ref}`}
                  >
                    <Download size={14} /> Download
                  </ActionButton>
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  );
}

const labelFor = (scope) => ({
  final_timesheets:        "Final timesheets",
  approved_payslips:       "Approved payslips",
  approved_tax_documents:  "Tax documents",
  approved_engagement_docs:"Engagement documents",
}[scope] || scope);

function SupportPanel() {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try { await portalSupport({ subject, body }); toast.success("Ticket opened"); setSubject(""); setBody(""); }
    catch { toast.error("Failed to open ticket"); }
    finally { setBusy(false); }
  };
  return (
    <form onSubmit={submit} className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
      <SectionHeading title="Open a support request" description="Use this if you need help with documents, MFA, or account questions." />
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-600">Subject</span>
        <input value={subject} onChange={(e) => setSubject(e.target.value)} required minLength={3} maxLength={255}
          className="w-full rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200" />
      </label>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-600">Message</span>
        <textarea value={body} onChange={(e) => setBody(e.target.value)} required minLength={3} maxLength={4000} rows={6}
          className="w-full rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200" />
      </label>
      <ActionButton type="submit" disabled={busy}>{busy ? "Sending…" : "Send"}</ActionButton>
    </form>
  );
}

function DataRightsPanel() {
  const [type, setType] = useState("access");
  const [details, setDetails] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault(); setBusy(true);
    try { await portalDsar({ request_type: type, details }); toast.success("Request submitted (SLA 30 days)"); setDetails(""); }
    catch { toast.error("Failed to submit"); }
    finally { setBusy(false); }
  };
  return (
    <form onSubmit={submit} className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
      <SectionHeading title="Data-rights request" description="Access, erasure, rectification, portability, or objection. The team has 30 days to respond." />
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-600">Type</span>
        <select value={type} onChange={(e) => setType(e.target.value)} className="rounded-md border border-slate-200 px-2 py-1.5 text-sm">
          <option value="access">Access (Art. 15)</option>
          <option value="erasure">Erasure (Art. 17)</option>
          <option value="rectification">Rectification (Art. 16)</option>
          <option value="portability">Portability (Art. 20)</option>
          <option value="objection">Objection (Art. 21)</option>
        </select>
      </label>
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-600">Details (optional)</span>
        <textarea value={details} onChange={(e) => setDetails(e.target.value)} maxLength={4000} rows={5}
          className="w-full rounded-md border border-slate-200 px-2 py-1.5 text-sm focus:border-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-200" />
      </label>
      <ActionButton type="submit" disabled={busy}>{busy ? "Submitting…" : "Submit"}</ActionButton>
    </form>
  );
}

function AccessLogPanel({ log }) {
  return (
    <div>
      <SectionHeading title="Your access log" description="Every action on this portal — yours and any abnormal activity we observed." />
      {log.length === 0 && <EmptyState title="No activity yet" />}
      <ul className="space-y-1">
        {log.map((r, i) => (
          <li key={i} className="rounded-md border border-slate-200 bg-white p-2 text-xs">
            <div className="flex flex-wrap justify-between gap-2">
              <span className="font-medium text-slate-800">{r.action}</span>
              <span className="text-slate-500">{new Date(r.occurred_at).toLocaleString()}</span>
            </div>
            <div className="mt-0.5 text-slate-500">
              {r.outcome === "ok" ? "" : <span className="text-amber-700">{r.outcome} · </span>}
              {r.scope || ""} {r.document_ref ? `· ${r.document_ref}` : ""} {r.ip_address ? `· ${r.ip_address}` : ""}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Router-driven default export — single file with internal sub-routes so
 * App.jsx only has to mount one `Routes` block under `/former-worker/*`.
 */
export default function FormerWorkerPortalRoutes() {
  return (
    <Routes>
      <Route index element={<PortalLogin />} />
      <Route path="verify" element={<PortalVerify />} />
      <Route path="enroll" element={<PortalEnroll />} />
      <Route path="home"   element={<PortalHome />} />
      <Route path="*" element={<Navigate to="/former-worker" replace />} />
    </Routes>
  );
}
