// Steps 3 and 4 of the commercial activation flow — Workspace and Ready.
// Routes: /workspace/preparing and /workspace/ready.
//
// SINGLE SOURCE OF TRUTH: GET /api/v1/activation/status(/me). Every milestone,
// percentage, reference id, trial length and workspace name on these screens is
// read from that response. There is deliberately no timer, no simulated
// progress and no client-side estimate of when provisioning will finish — the
// page reports what the backend has actually persisted.
//
// A NOTE ON HOW FAST THIS IS
// --------------------------
// Provisioning runs inline inside the signup transaction (see
// app/commercial/provisioning.py), so by the time anyone reaches Step 3 the
// workspace is normally already `ready` and this screen passes through in a
// beat. That is the truth of the system, not a shortcut: the read-model is
// agnostic to sync-vs-async, so if provisioning ever moves to a worker this
// page starts showing the intermediate states without changing.
//
// WHICH ENDPOINT
// --------------
// With no query string the tenant comes from the session (`/status/me`) — that
// is how Step 2 hands over, and it is what makes tenant isolation structural.
// The id-keyed variant is kept for the entry points that still carry one (the
// signup fallback link, support-shared URLs) so those keep working.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams, useNavigate, useLocation, Link } from "react-router-dom";
import { Check, Loader2, AlertTriangle, ArrowRight, Copy } from "lucide-react";

import { API_BASE, WORKSPACE_DOMAIN_SUFFIX } from "../config";
import { authFetch } from "../api/authFetch";
import ActivationHeader from "../components/activation/ActivationHeader";

const POLL_MS = 3000;
const SUPPORT_EMAIL = "support@temveris.example";
// Where "Configure your subscription" goes: the billing page the product
// already has. No second billing surface is introduced.
const BILLING_PATH = "/subscription/configure";

export default function WorkspaceActivation() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { pathname } = useLocation();

  const signupId = params.get("signup_id");
  const workspaceId = params.get("workspace_id");
  const orgId = params.get("organization_id");

  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);   // "auth" | "notfound" | "network"
  const [retrying, setRetrying] = useState(false);
  const timer = useRef(null);
  // Retry is attempted once per mount. The endpoint is idempotent, but hammering
  // it on every poll would be pointless load and would make "retrying" look
  // permanent when it is not.
  const retryAttempted = useRef(false);

  const query = signupId
    ? `signup_id=${encodeURIComponent(signupId)}`
    : workspaceId
    ? `workspace_id=${encodeURIComponent(workspaceId)}`
    : orgId
    ? `organization_id=${encodeURIComponent(orgId)}`
    : null;

  const poll = useCallback(async () => {
    try {
      const res = query
        ? await authFetch(`${API_BASE}/api/v1/activation/status?${query}`)
        : await authFetch(`${API_BASE}/api/v1/activation/status/me`);

      if (res.status === 401 || res.status === 403) { setLoadError("auth"); return null; }
      if (res.status === 404) { setLoadError("notfound"); return null; }
      if (!res.ok) { setLoadError("network"); return null; }

      const data = await res.json();
      setLoadError(null);
      setStatus(data);
      return data;
    } catch {
      setLoadError("network");
      return null;
    }
  }, [query]);

  // Poll until terminal. `ready` and `failed` both stop the clock; a failed
  // state that is retryable is handled by the retry effect below rather than by
  // polling forever.
  useEffect(() => {
    let cancelled = false;
    const stop = () => { if (timer.current) { clearInterval(timer.current); timer.current = null; } };

    (async () => {
      const first = await poll();
      if (cancelled) return;
      if (first && (first.status === "ready" || first.status === "failed")) return;
      timer.current = setInterval(async () => {
        const data = await poll();
        if (data && (data.status === "ready" || data.status === "failed")) stop();
      }, POLL_MS);
    })();

    return () => { cancelled = true; stop(); };
  }, [poll]);

  // Backend-controlled recovery. When the server reports a retryable failure
  // the page asks it — once — to re-run provisioning, using the same
  // idempotent operation the admin console calls. It never re-runs
  // provisioning itself, and it never retries a terminal failure.
  const retryAvailable = Boolean(status?.provisioning?.retry_available);
  const hasFailed = status?.status === "failed";
  useEffect(() => {
    if (!hasFailed || !retryAvailable || retryAttempted.current || query) return;
    retryAttempted.current = true;
    let cancelled = false;
    (async () => {
      // State is set inside the async body rather than synchronously in the
      // effect: the eslint rule forbids the latter, and a flag flipped before
      // the request has even been made is a claim the backend has not backed.
      if (!cancelled) setRetrying(true);
      try {
        await authFetch(`${API_BASE}/api/v1/activation/retry`, { method: "POST" });
      } catch { /* the status poll reports whatever actually happened */ }
      await poll();
      if (!cancelled) setRetrying(false);
    })();
    return () => { cancelled = true; };
  }, [hasFailed, retryAvailable, poll, query]);

  // ── Route reconciliation ────────────────────────────────────────────────
  // Ready is a backend fact. Landing on /workspace/ready before provisioning
  // finishes sends you back to the preparing screen, and reaching `ready` on
  // the preparing screen moves you forward. Neither is a timer.
  const isReady = status?.status === "ready";
  const onReadyRoute = pathname.startsWith("/workspace/ready");

  useEffect(() => {
    if (!status) return;
    if (isReady && !onReadyRoute) navigate("/workspace/ready", { replace: true });
    if (!isReady && onReadyRoute) navigate("/workspace/preparing", { replace: true });
  }, [status, isReady, onReadyRoute, navigate]);

  // ── Render ──────────────────────────────────────────────────────────────
  const step = onReadyRoute && isReady ? 4 : 3;

  return (
    <div className="min-h-screen bg-brand-canvas px-4 py-5 sm:py-6">
      <ActivationHeader current={step} />

      <div className="mx-auto w-full max-w-[860px]">

        <div className="mt-5">
          {loadError === "auth" ? (
            <Card><SignedOut /></Card>
          ) : loadError === "notfound" ? (
            <Card><NoActivation /></Card>
          ) : !status ? (
            <Card><LoadingState stale={loadError === "network"} /></Card>
          ) : isReady && onReadyRoute ? (
            <Card><ReadyView status={status} /></Card>
          ) : (
            <Card>
              <PreparingView
                status={status}
                retrying={retrying}
                stale={loadError === "network"}
              />
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

function Card({ children }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:p-8">
      {children}
    </section>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Step 3 — Workspace
// ═══════════════════════════════════════════════════════════════════════════
function PreparingView({ status, retrying, stale }) {
  const milestones = status.milestones || [];
  const p = status.provisioning || {};
  const failed = status.status === "failed";

  return (
    <div>
      <h1 className="text-[21px] font-bold tracking-tight text-slate-900 sm:text-[23px]">
        {failed ? "We're still preparing your workspace" : "Preparing your workspace"}
      </h1>
      <p className="mt-1 text-[13.5px] text-slate-500">
        {failed
          ? "Something needed a second attempt. You can keep this page open."
          : "This usually takes a minute or two. You can keep this page open."}
        {stale && " Reconnecting…"}
      </p>

      {/* The timeline is the progress indicator; a numeric bar alongside it
          would be a second, redundant reading of the same state. */}
      <ol
        className="mt-6"
        aria-label="Provisioning progress"
        aria-live="polite"
        aria-busy={!failed && status.status !== "ready"}
      >
        {milestones.map((m, i) => (
          <Milestone key={m.key} milestone={m} last={i === milestones.length - 1} />
        ))}
      </ol>

      {typeof p.estimated_seconds_remaining === "number" && !failed && (
        <p className="mt-5 inline-block rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-[12.5px] text-slate-500">
          Estimated time{" "}
          <strong className="font-semibold text-slate-900">
            {formatEta(p.estimated_seconds_remaining)}
          </strong>
        </p>
      )}

      <SupportFooter status={status} retrying={retrying} failed={failed} />
    </div>
  );
}

function Milestone({ milestone, last }) {
  const { title, description, status: state } = milestone;
  const done = state === "complete";
  const active = state === "active";
  const failed = state === "failed";

  return (
    <li className="flex gap-3">
      {/* Marker + the rail joining it to the next one. */}
      <div className="flex flex-col items-center">
        <span
          aria-hidden
          className={
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 text-[11px] font-semibold " +
            (done
              ? "border-brand-teal bg-brand-teal text-white"
              : failed
              ? "border-red-300 bg-red-50 text-red-600"
              : active
              ? "border-brand-teal bg-white text-brand-teal"
              : "border-slate-200 bg-white text-slate-400")
          }
        >
          {done ? <Check size={14} strokeWidth={3} />
            : failed ? <AlertTriangle size={13} />
            : active ? <Loader2 size={13} className="animate-spin" />
            : null}
        </span>
        {!last && (
          <span
            aria-hidden
            className={"w-[2px] flex-1 " + (done ? "bg-brand-teal" : "bg-slate-200")}
            style={{ minHeight: 26 }}
          />
        )}
      </div>

      <div className={last ? "pb-0" : "pb-5"}>
        <p
          className={
            "text-[14px] font-semibold leading-tight " +
            (done || active ? "text-slate-900" : failed ? "text-red-700" : "text-slate-400")
          }
        >
          {title}
        </p>
        <p className="mt-0.5 text-[12.5px] leading-snug text-slate-500">{description}</p>
        {/* State in words as well as in colour and iconography. */}
        <span className="sr-only">
          {done ? "Complete" : active ? "In progress" : failed ? "Needs attention" : "Not started"}
        </span>
      </div>
    </li>
  );
}

function SupportFooter({ status, retrying, failed }) {
  const reference = status.support_reference;
  const retryInProgress = retrying || (failed && status.provisioning?.retry_available);

  return (
    <div className="mt-7 grid gap-5 border-t border-slate-100 pt-5 sm:grid-cols-3">
      <div>
        <p className="text-[12.5px] font-semibold text-slate-900">Experiencing issues?</p>
        <p className="mt-1 text-[12px] leading-snug text-slate-500">
          Our operations team is on it.
          {reference && (
            <>
              {" "}Reference ID: <ReferenceId value={reference} />.
            </>
          )}
        </p>
      </div>

      <div>
        <p className="text-[12.5px] font-semibold text-slate-900">
          {retryInProgress ? "Retry in progress" : "Automatic recovery"}
        </p>
        <p className="mt-1 text-[12px] leading-snug text-slate-500" aria-live="polite">
          {retryInProgress
            ? "We're retrying provisioning automatically — no action needed."
            : "If anything fails, we retry it for you automatically."}
        </p>
      </div>

      <div>
        <p className="text-[12.5px] font-semibold text-slate-900">Need help?</p>
        <p className="mt-1 text-[12px] leading-snug text-slate-500">
          <a
            href={`mailto:${SUPPORT_EMAIL}`}
            className="font-semibold text-brand-green-dark underline-offset-2 hover:underline"
          >
            Contact support →
          </a>{" "}
          · {SUPPORT_EMAIL}
        </p>
      </div>
    </div>
  );
}

/** The reference, with a copy affordance — people quote this to support. */
function ReferenceId({ value }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard unavailable — the value is on screen regardless */ }
  };
  return (
    <>
      <strong className="whitespace-nowrap font-semibold tabular-nums text-slate-900">{value}</strong>
      <button
        type="button"
        onClick={copy}
        aria-label={`Copy reference ID ${value}`}
        className="ml-1 inline-flex align-[-2px] text-slate-400 hover:text-slate-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
      >
        <Copy size={12} aria-hidden />
      </button>
      <span className="sr-only" role="status">{copied ? "Reference ID copied" : ""}</span>
    </>
  );
}

function formatEta(seconds) {
  if (seconds <= 0) return "a moment";
  if (seconds < 60) return `${seconds} seconds`;
  const mins = Math.round(seconds / 60);
  return mins === 1 ? "1 minute" : `${mins} minutes`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Step 4 — Ready
// ═══════════════════════════════════════════════════════════════════════════
function ReadyView({ status }) {
  const navigate = useNavigate();

  const trial = status.trial || {};
  const days = trial.length_days;
  const name = status.owner_first_name;
  // The workspace name the admin chose on Step 2, shown with the environment's
  // display suffix. DISPLAY ONLY — the platform does not route by host, so this
  // is a label and the real destination is `workspace_url`.
  const label = status.workspace_slug
    ? `${status.workspace_slug}.${WORKSPACE_DOMAIN_SUFFIX}`
    : status.workspace_name || status.organization_name;

  const planLabel = useMemo(() => {
    const code = trial.plan_code;
    return code ? `Temveris ${code.charAt(0).toUpperCase()}${code.slice(1)}` : "Temveris";
  }, [trial.plan_code]);

  const open = () => {
    const url = status.workspace_url || "/dashboard";
    if (/^https?:\/\//.test(url)) window.location.assign(url);
    else navigate(url);
  };

  return (
    <div className="mx-auto max-w-[520px] py-4 text-center">
      <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-brand-green-light">
        <Check size={26} strokeWidth={3} className="text-brand-teal" aria-hidden />
      </div>

      <h1 className="mt-4 text-[23px] font-bold tracking-tight text-slate-900 sm:text-[26px]">
        Your workspace is ready
      </h1>

      <p className="mx-auto mt-2 max-w-[420px] text-[13.5px] leading-relaxed text-slate-500">
        {name ? <>Welcome aboard, {name}. </> : null}
        Your {planLabel} trial workspace{" "}
        {label && <strong className="font-semibold text-slate-900 [overflow-wrap:break-word]">{label}</strong>}
        {typeof days === "number" ? <> is live for {days} days.</> : <> is live.</>}
      </p>

      <div className="mt-6 flex flex-col gap-2.5 sm:flex-row sm:justify-center">
        {/* The trial is already active — signup provisioned it and entitlements
            are computed. This opens the workspace; it does not create a second
            subscription, start a checkout, or charge anything. */}
        <button
          type="button"
          onClick={open}
          className="inline-flex items-center justify-center rounded-xl bg-brand-teal px-5 py-3 text-[14.5px] font-semibold text-white transition-colors hover:bg-brand-teal-dark focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/50 focus-visible:ring-offset-2"
        >
          {typeof days === "number" ? `Claim ${days} Day Free Trial` : "Open your workspace"}
        </button>

        {/* The billing page the product already has. */}
        <Link
          to={BILLING_PATH}
          className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-brand-teal px-5 py-3 text-[14.5px] font-semibold text-white transition-colors hover:bg-brand-teal-dark focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/50 focus-visible:ring-offset-2"
        >
          Configure your subscription <ArrowRight size={15} aria-hidden />
        </Link>
      </div>

      <p className="mt-4 text-[12px] text-slate-500">
        Your trial is already active — no card needed, and nothing is charged without
        your authorisation.
      </p>

      {!status.email_verified && (
        <p className="mt-3 text-[12px] text-amber-700" role="status">
          Verify your email to unlock invites and exports.
        </p>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Fallback states
// ═══════════════════════════════════════════════════════════════════════════
function LoadingState({ stale }) {
  return (
    <div className="py-8 text-center" aria-busy="true" aria-live="polite">
      <Loader2 size={26} className="mx-auto animate-spin text-brand-teal" aria-hidden />
      <p className="mt-3 text-[13.5px] text-slate-500">
        {stale ? "Reconnecting…" : "Loading your workspace status…"}
      </p>
    </div>
  );
}

function SignedOut() {
  return (
    <div className="py-4">
      <h1 className="text-[21px] font-bold tracking-tight text-slate-900">Sign in to continue</h1>
      <p className="mt-2 text-[13.5px] leading-relaxed text-slate-600">
        We need to know who you are before we can show your workspace.
      </p>
      <Link
        to="/"
        className="mt-4 inline-flex items-center justify-center rounded-xl bg-brand-teal px-4 py-2.5 text-[14px] font-semibold text-white hover:bg-brand-teal-dark"
      >
        Sign in
      </Link>
    </div>
  );
}

function NoActivation() {
  return (
    <div className="py-4">
      <h1 className="text-[21px] font-bold tracking-tight text-slate-900">
        We couldn't find that workspace
      </h1>
      <p className="mt-2 text-[13.5px] leading-relaxed text-slate-600">
        The link may be incomplete. Start again and we'll pick up where you left off.
      </p>
      <Link
        to="/start"
        className="mt-4 inline-flex items-center justify-center rounded-xl bg-brand-teal px-4 py-2.5 text-[14px] font-semibold text-white hover:bg-brand-teal-dark"
      >
        Back to sign up
      </Link>
    </div>
  );
}
