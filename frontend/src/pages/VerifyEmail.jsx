// Step 2 — Verify. The second screen of the commercial activation flow
// (Account → Verify → Workspace → Ready), at /verify-email.
//
// TWO ARRIVALS, ONE ROUTE
// -----------------------
//   * With `?token=` — someone clicked the link in the welcome email. That is
//     the original behaviour of this page and is deliberately untouched: the
//     request is unauthenticated, because a person following a link from their
//     inbox may not be signed in, and links already sent have 48 hours of life.
//   * Without one — the activation flow's Verify step, reached from Step 1.
//     Here the visitor types the 6-digit code instead, against a session-gated
//     endpoint. Six digits cannot be accepted from an anonymous caller.
//
// Both credentials live on the same verification row server-side, so whichever
// is used first completes the verification and the other reports
// ALREADY_CONSUMED — which the server reports as success, not as an error.
//
// WHAT THIS SCREEN DOES NOT DECIDE
// --------------------------------
// The address shown comes from the session, not the query string. The resend
// countdown comes from the server's stored `last_sent_at`, not a local
// constant. The worker-count bands are served, not hardcoded. The organisation
// this configures is the one signup already created — nothing here creates an
// organisation, a workspace, a subscription or an entitlement.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { CheckCircle2, Loader2 } from "lucide-react";

import { API_BASE, WORKSPACE_DOMAIN_SUFFIX } from "../config";
import { authFetch } from "../api/authFetch";
import ActivationHeader from "../components/activation/ActivationHeader";
import WhyWeAsk from "../components/activation/WhyWeAsk";
import OtpInput from "../components/auth/OtpInput";
import TimezoneCombobox from "../components/TimezoneCombobox";
import SearchableSelect from "../components/SearchableSelect";
import { listCountries, guessCountry } from "../lib/countries";

const CODE_LENGTH = 6;

// Mirrors app/org/router.py::_SLUG_RE. The server is the authority; this only
// spares a round-trip on an obviously malformed value.
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SLUG_MAX = 100;
const NAME_MAX = 255;
const ROLE_MAX = 100;

const INPUT_BASE =
  "w-full rounded-xl border bg-white px-3.5 py-2.5 text-[14px] text-slate-800 " +
  "placeholder-slate-400 transition-colors focus:outline-none focus:ring-2";
const INPUT_OK = "border-slate-200 focus:border-brand-teal focus:ring-brand-teal/20";
const INPUT_ERR = "border-red-300 focus:border-red-400 focus:ring-red-200";

const PRIMARY_LINK =
  "inline-flex items-center justify-center rounded-xl bg-brand-teal px-4 py-2.5 text-[14px] " +
  "font-semibold text-white transition-colors hover:bg-brand-teal-dark focus:outline-none " +
  "focus-visible:ring-2 focus-visible:ring-brand-teal/50 focus-visible:ring-offset-2";

// TimezoneCombobox is styled by a token object rather than Tailwind classes.
// These map its slots onto the activation flow's light surface.
const TZ_TOKENS = {
  surface: "#FFFFFF",
  border: "#E2E8F0",
  text: "#1E293B",
  textLight: "#64748B",
  hover: "#F1F5F9",
  brand: "#1FB389",
  danger: "#DC2626",
};

/** Best-effort browser zone; the server validates whatever is submitted. */
function guessTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/** "Acme Corporation" → "acme-corporation", clamped to the server's rules. */
function slugify(text) {
  return (text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, SLUG_MAX);
}

export default function VerifyEmail() {
  const [params] = useSearchParams();
  const token = params.get("token");

  // The emailed-link landing is a different screen with different auth
  // requirements, so it stays its own component rather than being folded in.
  if (token) return <TokenLanding token={token} />;
  return <VerifyStep hintedEmail={params.get("email")} />;
}

// ═══════════════════════════════════════════════════════════════════════════
// The emailed link — unchanged behaviour, unauthenticated
// ═══════════════════════════════════════════════════════════════════════════
function TokenLanding({ token }) {
  const [state, setState] = useState({ phase: "verifying", message: "" });

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/v1/email-verification/verify`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        const body = await res.json();
        if (res.ok) {
          setState({
            phase: "ok",
            message: "Email verified — you can now invite teammates and export data.",
          });
        } else {
          setState({
            phase: "error",
            message: body?.error?.message || "Verification failed.",
            recovery: body?.error?.recovery_action,
          });
        }
      } catch {
        setState({ phase: "error", message: "We couldn't reach the verification service." });
      }
    })();
  }, [token]);

  return (
    <div className="min-h-screen bg-brand-canvas px-4 py-10">
      <div className="mx-auto w-full max-w-[520px]">
        <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:p-8">
          <h1 className="text-[21px] font-bold tracking-tight text-slate-900">
            {state.phase === "verifying" && "Verifying…"}
            {state.phase === "ok" && "You're verified."}
            {state.phase === "error" && "We couldn't verify that link."}
          </h1>
          <p className="mt-2 text-[14px] leading-relaxed text-slate-600" aria-live="polite">
            {state.message}
          </p>
          {state.recovery && (
            <p className="mt-3 rounded-xl bg-slate-50 px-3.5 py-2.5 text-[13px] leading-relaxed text-slate-600">
              {state.recovery === "request_new_verification_email"
                ? "Your link may have expired. Sign in and request a fresh verification email."
                : state.recovery}
            </p>
          )}
          <div className="mt-5">
            {state.phase === "ok" ? (
              <Link to="/verify-email" className={PRIMARY_LINK}>Continue setup</Link>
            ) : (
              <Link to="/" className={PRIMARY_LINK}>Sign in</Link>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Step 2 proper
// ═══════════════════════════════════════════════════════════════════════════
function VerifyStep({ hintedEmail }) {
  const navigate = useNavigate();

  const [status, setStatus] = useState(null);            // null = still loading
  const [statusError, setStatusError] = useState(null);  // "auth" | "network"
  const [code, setCode] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [codeError, setCodeError] = useState(null);
  const [locked, setLocked] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [resending, setResending] = useState(false);
  const [resendNote, setResendNote] = useState(null);

  const verified = Boolean(status?.verified);
  // The query-string address is a fallback for the case where signup could not
  // issue a session; it is never preferred over what the server reports.
  const email = status?.email || hintedEmail || "";

  const loadStatus = useCallback(async () => {
    try {
      const res = await authFetch(`${API_BASE}/api/v1/email-verification/status`);
      if (res.status === 401 || res.status === 403) {
        setStatusError("auth");
        return;
      }
      if (!res.ok) {
        setStatusError("network");
        return;
      }
      const body = await res.json();
      setStatus(body);
      setStatusError(null);
      setCooldown(body.resend_available_in ?? 0);
    } catch {
      setStatusError("network");
    }
  }, []);

  useEffect(() => { loadStatus(); }, [loadStatus]);

  // One ticking clock, seeded from the server and re-seeded after every
  // resend — so a refresh resumes the real remaining time rather than
  // restarting a countdown the backend never agreed to.
  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const id = setInterval(() => setCooldown((s) => Math.max(0, s - 1)), 1000);
    return () => clearInterval(id);
  }, [cooldown]);

  const inFlight = useRef(false);

  const submitCode = useCallback(async (value) => {
    const submitted = (value ?? code).trim();
    if (inFlight.current || verifying || verified) return;
    if (submitted.length !== CODE_LENGTH) {
      setCodeError(`Enter all ${CODE_LENGTH} digits.`);
      return;
    }
    inFlight.current = true;
    setVerifying(true);
    setCodeError(null);
    try {
      const res = await authFetch(`${API_BASE}/api/v1/email-verification/verify-code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: submitted }),
      });
      let body = null;
      try { body = await res.json(); } catch { /* empty body */ }

      if (res.ok) {
        setStatus((s) => ({ ...(s || {}), verified: true, email: s?.email || email }));
        setCodeError(null);
        setLocked(false);
        return;
      }
      if (res.status === 401 || res.status === 403) {
        setStatusError("auth");
        return;
      }
      const codeName = body?.error?.code;
      setLocked(codeName === "VERIFICATION_CODE_LOCKED");
      setCodeError(body?.error?.message || "We couldn't verify that code.");
      setCode("");
    } catch {
      setCodeError("We couldn't reach the verification service. Check your connection and try again.");
    } finally {
      inFlight.current = false;
      setVerifying(false);
    }
  }, [code, verifying, verified, email]);

  async function resend() {
    if (cooldown > 0 || resending || verified) return;
    setResending(true);
    setResendNote(null);
    setCodeError(null);
    try {
      const res = await authFetch(`${API_BASE}/api/v1/email-verification/resend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      // The endpoint answers uniformly whether or not the address exists — an
      // account-existence oracle would otherwise be trivial. So a 202 means
      // "handled", not "definitely sent", and the copy says as much.
      if (res.ok || res.status === 202) {
        setResendNote("If that address needs verifying, a new code is on its way.");
        setCode("");
        setLocked(false);
      } else if (res.status === 429) {
        setResendNote("Too many requests. Please wait a moment before trying again.");
      } else {
        setResendNote("We couldn't send a new code just now. Please try again shortly.");
      }
    } catch {
      setResendNote("We couldn't reach the verification service. Please try again.");
    } finally {
      setResending(false);
      // Re-read the authoritative cooldown rather than assuming it reset.
      loadStatus();
    }
  }

  return (
    <div className="min-h-screen bg-brand-canvas px-4 py-5 sm:py-6">
      <ActivationHeader current={2} />

      <div className="mx-auto w-full max-w-[1120px]">

        <div className="mt-5 grid items-start gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,3fr)] lg:gap-6">
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:p-6">
            {statusError === "auth" ? (
              <SignedOutNotice email={hintedEmail} />
            ) : (
              <>
                <VerificationSection
                  email={email}
                  verified={verified}
                  loading={status === null && statusError === null}
                  code={code}
                  setCode={setCode}
                  onSubmit={submitCode}
                  verifying={verifying}
                  error={codeError}
                  locked={locked}
                  cooldown={cooldown}
                  resending={resending}
                  onResend={resend}
                  resendNote={resendNote}
                  networkError={statusError === "network"}
                />

                {verified && (
                  <>
                    <hr className="my-5 border-slate-100" />
                    <OrganizationSection onDone={() => navigate("/workspace/preparing")} />
                  </>
                )}
              </>
            )}
          </section>

          <WhyWeAsk />
        </div>
      </div>
    </div>
  );
}

/** Signup issues a session, but issuance is non-fatal — this is that case. */
function SignedOutNotice({ email }) {
  return (
    <div>
      <h1 className="text-[21px] font-bold tracking-tight text-slate-900">Verify your email</h1>
      <p className="mt-2 text-[13.5px] leading-relaxed text-slate-600">
        We've emailed a verification link{email ? <> to <strong>{email}</strong></> : null}.
        Open it to confirm your address, or sign in first to enter the code here.
      </p>
      <div className="mt-4 flex flex-wrap gap-2.5">
        <Link to="/" className={PRIMARY_LINK}>Sign in</Link>
        <Link
          to="/start"
          className="inline-flex items-center justify-center rounded-xl border border-slate-200 px-4 py-2.5 text-[14px] font-semibold text-slate-700 hover:bg-slate-50"
        >
          Back to sign up
        </Link>
      </div>
    </div>
  );
}

// ── Section 1 — the code ────────────────────────────────────────────────────
function VerificationSection({
  email, verified, loading, code, setCode, onSubmit, verifying,
  error, locked, cooldown, resending, onResend, resendNote, networkError,
}) {
  if (verified) {
    return (
      <div className="flex items-start gap-2.5">
        <CheckCircle2 size={18} className="mt-[3px] shrink-0 text-brand-teal" aria-hidden />
        <div>
          <h1 className="text-[16px] font-bold text-slate-900">Email verified</h1>
          <p className="text-[13px] text-slate-500">
            {email
              ? <><strong className="font-semibold text-slate-700">{email}</strong> is confirmed.</>
              : "Your address is confirmed."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-[21px] font-bold tracking-tight text-slate-900">Verify your email</h1>
      <p className="mt-1 text-[13.5px] text-slate-500">
        Enter the {CODE_LENGTH}-digit code we sent to{" "}
        <strong className="font-semibold text-slate-700 break-all">
          {email || "your work email address"}
        </strong>.
      </p>

      <div className="mt-4 max-w-[340px]">
        <OtpInput
          id="zt-code"
          value={code}
          onChange={(v) => setCode(v)}
          onComplete={(v) => onSubmit(v)}
          length={CODE_LENGTH}
          disabled={verifying || loading || locked}
          invalid={Boolean(error)}
          label={`${CODE_LENGTH}-digit verification code`}
          describedBy={error ? "zt-code-error" : undefined}
        />
      </div>

      {error && (
        <p
          id="zt-code-error"
          role="alert"
          className="mt-2 flex items-start gap-1.5 text-[12px] leading-snug text-red-700"
        >
          <span aria-hidden className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-red-600" />
          <span>{error}</span>
        </p>
      )}

      {networkError && !error && (
        <p role="alert" className="mt-2 text-[12px] text-red-700">
          We couldn't load your verification status. The code above may still work.
        </p>
      )}

      {verifying && (
        <p className="mt-2 flex items-center gap-1.5 text-[12px] text-slate-500" role="status">
          <Loader2 size={13} className="animate-spin" aria-hidden /> Checking your code…
        </p>
      )}

      <p className="mt-3 text-[12.5px] text-slate-500">
        Didn't receive the code?{" "}
        {cooldown > 0 ? (
          <span className="font-semibold tabular-nums text-slate-700">
            Resend in {cooldown}s
          </span>
        ) : (
          <button
            type="button"
            onClick={onResend}
            disabled={resending}
            className="font-semibold text-brand-green-dark underline-offset-2 hover:underline disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
          >
            {resending ? "Sending…" : "Resend code"}
          </button>
        )}
        {" · "}
        {/* No email-change flow exists, and users.email is the identity the
            session, audit trail and consent record are keyed to. Correcting a
            typo means going back to the Account step, not mutating that column
            through a new endpoint. */}
        <Link
          to="/start"
          className="font-semibold text-brand-green-dark underline-offset-2 hover:underline"
        >
          Use a different email
        </Link>
      </p>

      {resendNote && (
        <p className="mt-1.5 text-[12px] text-slate-500" aria-live="polite">{resendNote}</p>
      )}
    </div>
  );
}

// ── Section 2 — the organisation ────────────────────────────────────────────
function OrganizationSection({ onDone }) {
  const [setup, setSetup] = useState(null);
  const [form, setForm] = useState({
    organization_name: "", role: "", worker_band: "",
    country: "", timezone: "", workspace_slug: "",
  });
  // Once the visitor edits the slug it stops tracking the organisation name,
  // so their choice is not silently overwritten as they keep typing.
  const [slugTouched, setSlugTouched] = useState(false);
  const [touched, setTouched] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);
  const [fieldErrors, setFieldErrors] = useState({});
  const inFlight = useRef(false);

  // SearchableSelect takes {value,label,search}. The code is included in the
  // search text so typing "DE" or "GB" finds the country as readily as its
  // name — the field stores the code, so that is what some admins will reach
  // for first.
  const countryOptions = useMemo(
    () => listCountries().map((c) => ({
      value: c.code, label: c.name, search: `${c.name} ${c.code}`,
    })),
    [],
  );

  // Prefill from the organisation signup already created — this form edits an
  // existing record, so it must open showing what is currently stored.
  useEffect(() => {
    (async () => {
      try {
        const res = await authFetch(`${API_BASE}/api/v1/onboarding/setup`);
        if (!res.ok) return;
        const body = await res.json();
        setSetup(body);
        const org = body.organization || {};
        setForm((f) => ({
          ...f,
          // The provisional "{First}'s workspace" is left out on purpose: it is
          // a placeholder the server invented, not something the admin chose.
          organization_name: /'s workspace$/.test(org.company_name || "")
            ? ""
            : (org.company_name || ""),
          country: org.primary_country || guessCountry(),
          timezone: org.default_timezone || guessTimezone(),
        }));
      } catch { /* the form still works unprefilled */ }
    })();
  }, []);

  const bands = setup?.worker_count_bands || [];

  const update = (key, value) => {
    setFieldErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
    setForm((f) => {
      const next = { ...f, [key]: value };
      if (key === "organization_name" && !slugTouched) next.workspace_slug = slugify(value);
      return next;
    });
  };
  const blur = (key) => setTouched((t) => ({ ...t, [key]: true }));

  const validation = useMemo(() => {
    const errors = {};
    const name = form.organization_name.trim();
    if (!name) errors.organization_name = "Enter your organisation's name.";
    else if (name.length > NAME_MAX) errors.organization_name = `Keep this under ${NAME_MAX} characters.`;

    if (form.role.trim().length > ROLE_MAX) errors.role = `Keep this under ${ROLE_MAX} characters.`;

    if (!form.worker_band) errors.worker_band = "Choose an approximate team size.";
    if (!form.country) errors.country = "Choose your primary operating country.";
    if (!form.timezone) errors.timezone = "Choose a time zone.";

    const slug = form.workspace_slug.trim();
    if (!slug) errors.workspace_slug = "Choose a workspace name.";
    else if (slug.length > SLUG_MAX) errors.workspace_slug = `Keep this under ${SLUG_MAX} characters.`;
    else if (!SLUG_RE.test(slug)) {
      errors.workspace_slug = "Use lowercase letters, numbers and single hyphens.";
    }
    return errors;
  }, [form]);

  const errorFor = (key) =>
    fieldErrors[key] || ((touched[key] || submitted) ? validation[key] : undefined);

  const formValid = Object.keys(validation).length === 0;
  const canSubmit = formValid && !busy;

  async function submit(e) {
    e.preventDefault();
    setSubmitted(true);
    if (inFlight.current || busy) return;
    if (!formValid) return;

    inFlight.current = true;
    setBusy(true);
    setFormError(null);
    setFieldErrors({});
    try {
      const res = await authFetch(`${API_BASE}/api/v1/onboarding/organization`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          organization_name: form.organization_name.trim(),
          role: form.role.trim() || null,
          worker_band: form.worker_band || null,
          country: form.country || null,
          timezone: form.timezone || null,
          workspace_slug: form.workspace_slug.trim() || null,
        }),
      });

      if (res.ok) {
        onDone();
        return;
      }

      let body = null;
      try { body = await res.json(); } catch { /* empty body */ }
      const detail = typeof body?.detail === "string" ? body.detail : null;

      if (res.status === 403) {
        setFormError("You don't have permission to configure this organisation.");
      } else if (res.status === 409) {
        setFormError(detail || "That workspace name is already taken. Try another.");
        setFieldErrors({ workspace_slug: "This name is taken." });
      } else if (res.status === 422 && Array.isArray(body?.detail)) {
        const mapped = {};
        for (const item of body.detail) {
          const field = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : null;
          if (field && typeof item?.msg === "string") {
            mapped[field] = item.msg.replace(/^Value error,\s*/, "");
          }
        }
        setFieldErrors(mapped);
        setFormError("Please check the highlighted fields and try again.");
      } else if (detail) {
        // The server's field messages are written for customers — slug format,
        // country code, IANA zone. Surfaced as-is; never a stack trace.
        setFormError(detail);
      } else {
        setFormError("We couldn't save your organisation details. Please try again.");
      }
    } catch {
      setFormError("We couldn't reach the server. Your details are still here — please try again.");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div>
      <h2 className="text-[19px] font-bold tracking-tight text-slate-900">
        Great! Let&rsquo;s set up your organisation
      </h2>
      <p className="mt-1 text-[13.5px] text-slate-500">
        This helps us configure your workspace securely and apply the right controls.
      </p>

      <form onSubmit={submit} className="mt-4 space-y-3.5" aria-busy={busy} noValidate>
        <Field id="zt-org-name" label="Organisation name" error={errorFor("organization_name")}>
          {(props) => (
            <input
              {...props}
              type="text"
              autoComplete="organization"
              maxLength={NAME_MAX}
              placeholder="Acme Corporation"
              value={form.organization_name}
              onChange={(e) => update("organization_name", e.target.value)}
              onBlur={() => blur("organization_name")}
            />
          )}
        </Field>

        <div className="grid gap-3.5 sm:grid-cols-2">
          <Field id="zt-org-role" label="Your role" error={errorFor("role")}>
            {(props) => (
              <input
                {...props}
                required={false}
                type="text"
                autoComplete="organization-title"
                maxLength={ROLE_MAX}
                placeholder="IT / Security Manager"
                value={form.role}
                onChange={(e) => update("role", e.target.value)}
                onBlur={() => blur("role")}
              />
            )}
          </Field>

          <Field id="zt-org-workers" label="Active workers (approx)" error={errorFor("worker_band")}>
            {(props) => (
              <select
                {...props}
                value={form.worker_band}
                onChange={(e) => update("worker_band", e.target.value)}
                onBlur={() => blur("worker_band")}
              >
                <option value="">Select a range</option>
                {bands.map((b) => (
                  <option key={b.value} value={b.value}>{b.label}</option>
                ))}
              </select>
            )}
          </Field>
        </div>

        <div className="grid gap-3.5 sm:grid-cols-2">
          <div className="min-w-0">
            <label
              htmlFor="zt-org-country"
              className="mb-1 block text-[12.5px] font-semibold text-slate-800"
            >
              Primary operating country
            </label>
            {/* The shared searchable list rather than a native <select>. With
                263 countries the native popup is taller than the viewport, so
                the browser opens it off the top of the screen and scrolling to
                a country means hunting through a wall of names. This is the
                same control, and the same interaction contract, as the
                timezone picker beside it: a panel capped at a readable height
                with a visible scrollbar, and type-to-filter. */}
            <SearchableSelect
              id="zt-org-country"
              value={form.country}
              onChange={(code) => { update("country", code); blur("country"); }}
              options={countryOptions}
              C={TZ_TOKENS}
              ariaLabel="Primary operating country"
              placeholder="Select a country"
              searchPlaceholder="Search countries…"
              emptyText="No matching country"
              marginBottom={0}
              minWidth={0}
            />
            {errorFor("country") && (
              <FieldError id="zt-org-country-error">{errorFor("country")}</FieldError>
            )}
          </div>

          <div className="min-w-0">
            <label
              htmlFor="zt-org-timezone"
              className="mb-1 block text-[12.5px] font-semibold text-slate-800"
            >
              Time zone
            </label>
            {/* The shared IANA picker, so nothing outside the real zone list can
                be chosen. PUT /org/settings validates it again server-side. */}
            <TimezoneCombobox
              id="zt-org-timezone"
              value={form.timezone}
              onChange={(tz) => { update("timezone", tz); blur("timezone"); }}
              C={TZ_TOKENS}
              invalid={Boolean(errorFor("timezone"))}
            />
            {errorFor("timezone") && (
              <FieldError id="zt-org-timezone-error">{errorFor("timezone")}</FieldError>
            )}
          </div>
        </div>

        <Field id="zt-org-slug" label="Workspace name" error={errorFor("workspace_slug")}>
          {(props) => (
            <div className="relative">
              <input
                {...props}
                className={`${props.className} pr-[132px]`}
                type="text"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                maxLength={SLUG_MAX}
                placeholder="acme-workspace"
                value={form.workspace_slug}
                onChange={(e) => { setSlugTouched(true); update("workspace_slug", e.target.value); }}
                onBlur={() => blur("workspace_slug")}
              />
              {/* Display only — the platform does not route by host. See
                  WORKSPACE_DOMAIN_SUFFIX in config.js. */}
              <span
                aria-hidden
                className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 text-[13px] text-slate-400"
              >
                .{WORKSPACE_DOMAIN_SUFFIX}
              </span>
            </div>
          )}
        </Field>

        {formError && (
          <div
            role="alert"
            className="rounded-xl border border-red-200 bg-red-50 px-3.5 py-2.5 text-[12.5px] leading-snug text-red-800"
          >
            {formError}
          </div>
        )}

        <button
          type="submit"
          disabled={!canSubmit}
          className={
            "mt-0.5 flex w-full items-center justify-center gap-2 rounded-xl py-2.5 " +
            "text-[14.5px] font-semibold text-white transition-colors " +
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/50 focus-visible:ring-offset-2 " +
            (canSubmit ? "bg-brand-teal hover:bg-brand-teal-dark" : "cursor-not-allowed bg-brand-teal/50")
          }
        >
          {busy ? (<><span className="loader" aria-hidden /> Saving…</>) : "Continue"}
        </button>

        <p className="sr-only" role="status" aria-live="polite">
          {busy ? "Saving your organisation details, please wait." : ""}
        </p>
      </form>
    </div>
  );
}

// ── Shared field plumbing (same shape as Step 1) ────────────────────────────
function Field({ id, label, error, children }) {
  const errorId = `${id}-error`;
  return (
    // min-w-0: as a grid item this would otherwise refuse to shrink below its
    // content width and push the page sideways on a narrow screen.
    <div className="min-w-0">
      <label htmlFor={id} className="mb-1 block text-[12.5px] font-semibold text-slate-800">
        {label}
      </label>
      {children({
        id,
        required: true,
        "aria-invalid": error ? "true" : undefined,
        "aria-describedby": error ? errorId : undefined,
        className: `${INPUT_BASE} ${error ? INPUT_ERR : INPUT_OK}`,
      })}
      {error && <FieldError id={errorId}>{error}</FieldError>}
    </div>
  );
}

function FieldError({ id, children }) {
  return (
    <p
      id={id}
      role="alert"
      className="mt-1 flex items-start gap-1.5 text-[11.5px] leading-snug text-red-700"
    >
      <span aria-hidden className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-red-600" />
      <span>{children}</span>
    </p>
  );
}
