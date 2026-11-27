// Step 1 — Account. The public entry point of the commercial activation flow
// (Tier-0 spec v3.0 §6.1 / §14.1), at temveris.example/start.
//
// The flow is Account → Verify → Workspace → Ready. This screen creates the
// account and hands off to the existing email-verification step; it does not
// provision anything itself and it does not activate billing. Submitting it
// POSTs the same `/api/v1/signup` as before — the endpoint that runs the whole
// audited pipeline (fraud → routing → identity → provisioning → entitlements →
// consent → verification email → session).
//
// WHAT THIS SCREEN DOES NOT DECIDE
// --------------------------------
// Price, currency, annual discount, trial length, trial end date and how much
// is due today all come from `GET /api/v1/billing/quote/preview`, which prices
// from the same function checkout charges from. Nothing on this page computes
// money or dates. The one thing it does hold locally is a fallback pair of
// legal-document versions, used only if that request fails, because a consent
// record has to be stamped with something.
//
// The organization is deliberately NOT named here. It is named on the Workspace
// step (the setup wizard's "Organization name" field); until then the server
// derives a provisional name. See SignupRequest._derive_company_name.

import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Eye, EyeOff } from "lucide-react";

import { API_BASE } from "../config";
import { useAuth } from "../context/AuthContext";
import SignupChallenge from "../components/auth/SignupChallenge";
import { isChallengeConfigured } from "../components/auth/challengeConfig";
import { SIGNUP_PASSWORD_RULES, unmetPasswordRules } from "../components/auth/passwordRules";
import ActivationHeader from "../components/activation/ActivationHeader";
import PlanSummaryCard from "../components/activation/PlanSummaryCard";
import {
  useSignupOffer, FALLBACK_TERMS_VERSION, FALLBACK_PRIVACY_VERSION,
} from "../components/activation/useSignupOffer";

// The plan comes from the marketing link's ?plan= param, falling back to the
// entry tier. Anything else is ignored here and rejected server-side against
// constants.SELF_SERVE_PLANS — the allow-list, not this array, is the control.
const PLAN_CODES = ["verified", "governed", "sovereign"];
const DEFAULT_PLAN = "verified";

// Deliberately permissive, matching the shape the backend's EmailStr accepts.
// A stricter client regex only produces false rejections of addresses the
// server would have taken.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

// Mirrors SignupRequest's Field(max_length=120) so an over-long name is caught
// before the round trip rather than coming back as a 422.
const NAME_MAX = 120;

const INPUT_BASE =
  "w-full rounded-xl border bg-white px-3.5 py-2.5 text-[14px] text-slate-800 " +
  "placeholder-slate-400 transition-colors focus:outline-none focus:ring-2";
const INPUT_OK =
  "border-slate-200 focus:border-brand-teal focus:ring-brand-teal/20";
const INPUT_ERR =
  "border-red-300 focus:border-red-400 focus:ring-red-200";

export default function Start() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { login, logout } = useAuth();

  // "Sign in" from this signup flow must abandon the current session FIRST.
  // Signup auto-logs-in the newly created (still unverified) account, so a plain
  // <Link to="/"> lands a logged-in user on Login, which auto-forwards them into
  // the app — where the email-verification gate bounces them back to
  // /verify-email, an inescapable loop. logout() clears the session (storage +
  // in-memory state) and hard-navigates to the real login form.
  const goToLogin = (e) => { e.preventDefault(); logout(); };

  const preselected = params.get("plan");
  const plan = PLAN_CODES.includes(preselected) ? preselected : DEFAULT_PLAN;

  // ── Two ways in, one form ────────────────────────────────────────────────
  // The pricing page offers "Start 14-Day Free Trial" and "Subscribe now" for
  // the same plan. Both need exactly the same four fields — a paying customer
  // tells us nothing extra at signup — so they share this page rather than
  // getting a second signup surface that would have to duplicate the password
  // policy, consent capture, bot challenge, fraud checks and idempotency key.
  //
  // `mode=subscribe` changes the copy, the summary panel and where we go next.
  // It deliberately does NOT change what is sent to the API: see the note on
  // `intent` in the payload below.
  const subscribing = params.get("mode") === "subscribe";
  const cycle = params.get("cycle") === "annual" ? "annual" : "monthly";
  const seatsParam = Number.parseInt(params.get("seats") || "", 10);
  const seats = Number.isFinite(seatsParam) && seatsParam > 0 ? seatsParam : null;
  const { offer, error: offerError, loading: offerLoading, retry: retryOffer } =
    useSignupOffer(plan);

  // Some plans are not sold as a no-card trial — Sovereign is set up with the
  // team rather than self-started. The server says which (`trial_available` on
  // the offer) so this page does not carry its own copy of the rule, and the
  // router remains the authority: submitting anyway is routed to sales, this
  // only stops the page promising something that will not happen. While the
  // offer is still loading the trial copy stands, which is the common case.
  const trialUnavailable = !subscribing && offer?.trial_available === false;

  const [form, setForm] = useState({
    email: "", first_name: "", last_name: "", password: "",
  });
  const [accepted, setAccepted] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  // A field's error is shown once it has been blurred or once submit has been
  // attempted — never while someone is still mid-word in a field they have not
  // finished. `submitted` flips them all on at once.
  const [touched, setTouched] = useState({});
  const [submitted, setSubmitted] = useState(false);

  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState(null);   // { message, code? }
  const [fieldErrors, setFieldErrors] = useState({}); // from the server
  const [success, setSuccess] = useState(null);

  // Proof-of-humanity token. Null until the widget solves, and back to null on
  // expiry so a stale token is never submitted. When no site key is configured
  // the widget renders nothing and the token stays null — hence the
  // isChallengeConfigured() guard, so an unconfigured deployment is not blocked
  // by a challenge it never showed.
  const [challengeToken, setChallengeToken] = useState(null);

  // Idempotency key for the signup POST. Stable across retries of the same
  // submission (double-click, network retry → the backend replays the original
  // response), regenerated whenever the form changes so an edited resubmit is a
  // genuinely new request rather than a replay of the old one.
  const idemKey = useRef(null);
  // Second line of defence against a double submit: `busy` is React state, so
  // two events dispatched in the same tick can both read the old value. This
  // ref is set synchronously.
  const inFlight = useRef(false);

  const update = (key, value) => {
    idemKey.current = null;
    setFieldErrors((e) => (e[key] ? { ...e, [key]: undefined } : e));
    setForm((f) => ({ ...f, [key]: value }));
  };
  const blur = (key) => setTouched((t) => ({ ...t, [key]: true }));

  // ── Validation ───────────────────────────────────────────────────────────
  // Complements the server's; it never replaces it. Every rule here has a
  // counterpart in SignupRequest, and the server's answer always wins — a
  // field error returned by the API is merged in below.
  const validation = useMemo(() => {
    const email = form.email.trim();
    const first = form.first_name.trim();
    const last = form.last_name.trim();
    const errors = {};

    if (!email) errors.email = "Enter your work email address.";
    else if (!EMAIL_RE.test(email)) errors.email = "Enter a valid email address, like name@company.com.";

    if (!first) errors.first_name = "Enter your first name.";
    else if (first.length > NAME_MAX) errors.first_name = `Keep this under ${NAME_MAX} characters.`;

    if (!last) errors.last_name = "Enter your last name.";
    else if (last.length > NAME_MAX) errors.last_name = `Keep this under ${NAME_MAX} characters.`;

    if (!form.password) {
      errors.password = "Choose a password.";
    } else {
      const unmet = unmetPasswordRules(form.password, SIGNUP_PASSWORD_RULES);
      if (unmet.length) {
        errors.password =
          "Your password still needs: " +
          unmet.map((r) => r.label.toLowerCase()).join(", ") + ".";
      }
    }

    if (!accepted) errors.terms = "Please accept the Terms of Service and Privacy Policy to continue.";

    return errors;
  }, [form, accepted]);

  /** Server-reported errors take precedence over the local guess. */
  const errorFor = (key) =>
    fieldErrors[key] || ((touched[key] || submitted) ? validation[key] : undefined);

  const challengeReady = !isChallengeConfigured() || Boolean(challengeToken);
  const formValid = Object.keys(validation).length === 0;
  const canSubmit = formValid && challengeReady && !busy && !success;

  // ── Submit ───────────────────────────────────────────────────────────────
  async function submit(e) {
    e.preventDefault();
    setSubmitted(true);

    if (inFlight.current || busy || success) return;
    if (!formValid) {
      // Move focus to the first problem so the message is announced rather
      // than sitting silently below the fold.
      const first = ["email", "first_name", "last_name", "password", "terms"]
        .find((k) => validation[k]);
      if (first) {
        const id = first === "terms" ? "zt-terms" : `zt-${first.replace("_", "-")}`;
        document.getElementById(id)?.focus();
      }
      return;
    }
    if (!challengeReady) {
      setFormError({ message: "Please complete the verification challenge and try again." });
      return;
    }

    inFlight.current = true;
    setBusy(true);
    setFormError(null);
    setFieldErrors({});

    // Set on the path that navigates away, so the button keeps its loading
    // state through the transition instead of flicking back to "Create my
    // trial workspace" for a frame while the account already exists.
    let leaving = false;

    try {
      const payload = {
        first_name: form.first_name.trim(),
        last_name: form.last_name.trim(),
        // Normalised the same way the server does (`payload.email.strip().lower()`)
        // so the address the visitor is told about matches the one stored.
        email: form.email.trim().toLowerCase(),
        password: form.password,
        // No company_name: the organization is named on the Workspace step.
        country: null,
        plan,
        // ALWAYS "trial", including on the subscribe path — this is a UI mode,
        // not a commercial one. Provisioning reads it as
        //   state = "trial_active" if intent == "trial" else "active"
        // so sending "paid" would mark the subscription active before a single
        // payment, and anyone who abandoned Stripe would be left an active,
        // unpaid tenant with no trial clock to expire them. Signing them up as
        // a trial keeps that safety net for the two minutes before they pay;
        // the successful payment flips them to active through the existing
        // confirm/webhook path. They never see trial wording either way.
        intent: "trial",
        expected_users: 1,
        // Ticking the box IS the acceptance, recorded against the versions the
        // server served with the offer (falling back only if that failed).
        terms_version: offer?.terms_version || FALLBACK_TERMS_VERSION,
        privacy_version: offer?.privacy_version || FALLBACK_PRIVACY_VERSION,
        marketing_consent: false,
        sso_or_scim_requested: false,
        custom_data_residency: false,
        challenge_token: challengeToken,
        source: {
          utm_source: params.get("utm_source") || null,
          utm_medium: params.get("utm_medium") || null,
          utm_campaign: params.get("utm_campaign") || null,
          referrer: document.referrer || null,
        },
        client_context: {
          locale: navigator.language,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          device_fingerprint: localStorage.getItem("zt_device_fp") || null,
        },
      };

      if (!idemKey.current) {
        idemKey.current = (globalThis.crypto?.randomUUID?.())
          || `idem_${Date.now()}_${Math.random().toString(36).slice(2)}`;
      }

      const res = await fetch(`${API_BASE}/api/v1/signup`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idemKey.current,
        },
        body: JSON.stringify(payload),
      });

      let body = null;
      try { body = await res.json(); } catch { /* empty or non-JSON body */ }

      if (!res.ok) {
        handleFailure(res, body);
        return;
      }

      // 202 — routed to the manual-review queue.
      if (res.status === 202 && body?.manual_review_required) {
        setSuccess({ kind: "review", message: body.message });
        return;
      }

      // Assisted / enterprise: sales-led, no tenant was provisioned.
      if (body?.route === "assisted" || body?.route === "enterprise") {
        setSuccess({ kind: "handoff", message: body.message, redirect: body.redirect_url });
        return;
      }

      // Self-serve success. Signup issues a session, so store it before
      // navigating; Step 2 and everything past it are behind that session.
      // Issuance is non-fatal server-side, so a missing token means "sign in
      // manually", not "the signup failed".
      setSuccess({ kind: "ok" });
      leaving = true;
      if (body?.access_token) {
        try { await login(body.access_token, null, { remember: true }); } catch { /* the tenant exists either way */ }
      }

      if (subscribing) {
        // Straight to checkout. The tenant now exists, so the configurator has
        // an organization to price against and `/upgrade` has one to bill —
        // which is exactly why the pricing page cannot link there directly.
        // Workspace details (name, country, timezone) are collected after
        // payment; none of them are needed to take it.
        const q = new URLSearchParams({ plan, cycle });
        if (seats) q.set("seats", String(seats));
        navigate(`/subscription/configure?${q}`, { replace: true });
        return;
      }

      // → Step 2, Verify. The email address travels in the query string so the
      // verify screen can say which inbox to check; it carries no authority and
      // revisiting or refreshing that URL creates nothing.
      navigate(
        `/verify-email?email=${encodeURIComponent(payload.email)}`,
        { replace: true, state: { fromSignup: true } },
      );
    } catch {
      setFormError({
        message: "We couldn't reach the signup service. Check your connection and try again.",
      });
    } finally {
      inFlight.current = false;
      if (!leaving) setBusy(false);
    }
  }

  /** Turn a failed response into something a person can act on. */
  function handleFailure(res, body) {
    const err = body?.error;
    const code = err?.code;

    // A challenge token is single-use: once the server has rejected or consumed
    // it, the widget must mint a fresh one or every retry fails identically.
    if (code === "CHALLENGE_REQUIRED" || code === "CHALLENGE_FAILED") {
      setChallengeToken(null);
    }

    if (code === "EMAIL_ALREADY_EXISTS") {
      // Says only what the person who owns the address already knows. The
      // account-existence disclosure here is the server's decision, not ours —
      // it is what lets someone recover instead of retrying forever.
      // Marked on the field, explained once in the banner — the banner is where
      // the two ways out live, so repeating its full sentence inline would just
      // be announced twice.
      setFieldErrors({ email: "This email is already registered." });
      setFormError({ message: err.message, code });
      return;
    }

    // FastAPI/Pydantic validation — map each item back onto its field.
    if (res.status === 422 && Array.isArray(body?.detail)) {
      const mapped = {};
      for (const item of body.detail) {
        const field = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : null;
        if (field && typeof item?.msg === "string") {
          mapped[field] = item.msg.replace(/^Value error,\s*/, "");
        }
      }
      if (Object.keys(mapped).length) {
        setFieldErrors(mapped);
        setFormError({ message: "Please check the highlighted fields and try again." });
        return;
      }
    }

    if (res.status === 429) {
      setFormError({
        message: "Too many signup attempts from this network. Please wait a few minutes and try again.",
      });
      return;
    }

    if (err?.field && err?.message) {
      setFieldErrors({ [err.field]: err.message });
      setFormError({ message: err.message, code });
      return;
    }

    // Everything else, including a 500. The server's messages are written for
    // customers; the generic fallback is for the cases that have none. Neither
    // path ever surfaces a stack trace or a database error.
    setFormError({
      message: err?.message || "Something went wrong creating your account. Please try again.",
      code,
    });
  }

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen bg-brand-canvas px-4 py-5 sm:py-6">
      {/* The four-step indicator promises Account → Verify → Workspace → Ready.
            On the subscribe path the next screen is checkout, so showing it
            would advertise a route this customer is not taking. They still
            verify their email later — entitlements gate invites and exports on
            it — just not as the next step. */}
        <ActivationHeader current={1} steps={!subscribing} />

      <div className="mx-auto w-full max-w-[1120px]">

        <div className="mt-5 grid items-start gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,3fr)] lg:gap-6">
          {/* ── Account card ─────────────────────────────────────────── */}
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:p-6">
            <h1 className="text-[21px] font-bold tracking-tight text-slate-900 sm:text-[23px]">
              Create your Temveris account
            </h1>
            <p className="mt-1 text-[13.5px] text-slate-500">
              {subscribing
                ? "Set up your account, then choose seats and pay on the next step."
                : trialUnavailable
                ? `${offer?.plan_label ?? "This plan"} is set up together with our team. Create your account and we'll take it from there.`
                : `Start your ${offer?.trial_length_days ?? 14}-day free trial. No credit card required.`}
            </p>

            <form onSubmit={submit} className="mt-5 space-y-3.5" aria-busy={busy} noValidate>
              <Field
                id="zt-email"
                label="Work email"
                error={errorFor("email")}
              >
                {(props) => (
                  <input
                    {...props}
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    placeholder="name@company.com"
                    value={form.email}
                    onChange={(e) => update("email", e.target.value)}
                    onBlur={() => blur("email")}
                  />
                )}
              </Field>

              <div className="grid gap-3.5 sm:grid-cols-2">
                <Field id="zt-first-name" label="First name" error={errorFor("first_name")}>
                  {(props) => (
                    <input
                      {...props}
                      type="text"
                      autoComplete="given-name"
                      maxLength={NAME_MAX}
                      placeholder="Jane"
                      value={form.first_name}
                      onChange={(e) => update("first_name", e.target.value)}
                      onBlur={() => blur("first_name")}
                    />
                  )}
                </Field>

                <Field id="zt-last-name" label="Last name" error={errorFor("last_name")}>
                  {(props) => (
                    <input
                      {...props}
                      type="text"
                      autoComplete="family-name"
                      maxLength={NAME_MAX}
                      placeholder="Doe"
                      value={form.last_name}
                      onChange={(e) => update("last_name", e.target.value)}
                      onBlur={() => blur("last_name")}
                    />
                  )}
                </Field>
              </div>

              <Field id="zt-password" label="Password" error={errorFor("password")}>
                {(props) => (
                  <div className="relative">
                    <input
                      {...props}
                      className={`${props.className} pr-11`}
                      type={showPassword ? "text" : "password"}
                      autoComplete="new-password"
                      placeholder="At least 8 characters"
                      value={form.password}
                      onChange={(e) => update("password", e.target.value)}
                      onBlur={() => blur("password")}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((v) => !v)}
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      aria-pressed={showPassword}
                      className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md p-1 text-slate-400 hover:text-slate-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
                    >
                      {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                    </button>
                  </div>
                )}
              </Field>

              {/* Terms — mandatory. The label wraps the text, not the links, so
                  the links stay independently reachable by keyboard instead of
                  being swallowed by the checkbox's click target. */}
              <div>
                <div className="flex items-start gap-3">
                  <input
                    id="zt-terms"
                    type="checkbox"
                    checked={accepted}
                    onChange={(e) => { setAccepted(e.target.checked); blur("terms"); }}
                    aria-invalid={errorFor("terms") ? "true" : undefined}
                    aria-describedby={errorFor("terms") ? "zt-terms-error" : undefined}
                    className="mt-[3px] h-[18px] w-[18px] shrink-0 cursor-pointer rounded border-slate-300 text-brand-teal accent-brand-teal focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40 focus-visible:ring-offset-1"
                  />
                  <label htmlFor="zt-terms" className="text-[12.5px] leading-relaxed text-slate-600">
                    I agree to the{" "}
                    <a
                      href="/terms"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-semibold text-brand-green-dark underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
                    >
                      Terms of Service
                    </a>{" "}
                    and{" "}
                    <a
                      href="/privacy"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-semibold text-brand-green-dark underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
                    >
                      Privacy Policy
                    </a>.
                  </label>
                </div>
                {errorFor("terms") && (
                  <FieldError id="zt-terms-error" className="ml-[30px]">
                    {errorFor("terms")}
                  </FieldError>
                )}
              </div>

              <SignupChallenge onToken={setChallengeToken} />

              {formError && (
                <div
                  role="alert"
                  className="rounded-xl border border-red-200 bg-red-50 px-3.5 py-2.5 text-[12.5px] leading-snug text-red-800"
                >
                  {formError.message}
                  {formError.code === "EMAIL_ALREADY_EXISTS" && (
                    <>
                      {" "}
                      <Link to="/" onClick={goToLogin} className="font-semibold underline underline-offset-2">
                        Sign in
                      </Link>{" "}
                      or{" "}
                      <Link to="/forgot-password" className="font-semibold underline underline-offset-2">
                        reset your password
                      </Link>.
                    </>
                  )}
                </div>
              )}

              {success && success.kind !== "ok" && (
                <div
                  aria-live="polite"
                  className="rounded-xl border border-brand-green-mid bg-brand-green-light px-3.5 py-2.5 text-[12.5px] leading-snug text-brand-green-dark"
                >
                  {success.message}
                  {success.kind === "handoff" && success.redirect && (
                    <>
                      {" "}
                      <a href={success.redirect} className="font-semibold underline underline-offset-2">
                        Continue
                      </a>
                    </>
                  )}
                </div>
              )}

              <button
                type="submit"
                disabled={!canSubmit}
                className={
                  "mt-0.5 flex w-full items-center justify-center gap-2 rounded-xl py-2.5 " +
                  "text-[14.5px] font-semibold text-white transition-colors " +
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/50 focus-visible:ring-offset-2 " +
                  (canSubmit
                    ? "bg-brand-teal hover:bg-brand-teal-dark"
                    : "cursor-not-allowed bg-brand-teal/50")
                }
              >
                {busy ? (
                  <>
                    <span className="loader" aria-hidden />
                    Creating your workspace…
                  </>
                ) : subscribing ? (
                  "Continue to payment"
                ) : trialUnavailable ? (
                  "Request access"
                ) : (
                  "Create my trial workspace"
                )}
              </button>

              {/* Announced without stealing focus, so a screen reader hears the
                  submission state change. */}
              <p className="sr-only" role="status" aria-live="polite">
                {busy ? "Creating your account, please wait." : ""}
                {success?.kind === "ok" ? "Account created. Taking you to email verification." : ""}
              </p>

            </form>

            {/* One footer block rather than two: the first line warns that this
                page creates a NEW organization — employees join through their
                admin's invite, and signing up here would strand them in an
                organization of one — and the second is the way back to login,
                which this page owns now that it has left the auth tab shell. */}
            <div className="mt-3 space-y-1 text-center">
              <p className="text-[11.5px] leading-snug text-slate-500">
                This creates a new organization. Joining a team that already uses
                Temveris? Ask your administrator for an invite link.
              </p>
              <p className="text-[12.5px]">
                <span className="text-slate-500">Already have an account? </span>
                <Link
                  to="/"
                  onClick={goToLogin}
                  className="font-semibold text-brand-green-dark underline-offset-2 hover:underline"
                >
                  Sign in
                </Link>
              </p>
            </div>
          </section>

          {/* ── Plan summary ─────────────────────────────────────────── */}
          <PlanSummaryCard
            offer={offer}
            error={offerError}
            loading={offerLoading}
            onRetry={retryOffer}
            mode={subscribing ? "subscribe" : "trial"}
            cycle={cycle}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * A labelled field. Takes its input as a render prop so the id, the invalid
 * state and the description wiring are produced in exactly one place and no
 * field can be built missing one of them.
 */
function Field({ id, label, error, children }) {
  const errorId = `${id}-error`;
  return (
    <div>
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

/**
 * An inline field error. `role="alert"` so it is announced when it appears, and
 * the leading dot is decorative — the message itself carries the meaning, so
 * the error is never communicated by colour alone.
 */
function FieldError({ id, className = "", children }) {
  return (
    <p
      id={id}
      role="alert"
      className={`mt-1 flex items-start gap-1.5 text-[11.5px] leading-snug text-red-700 ${className}`}
    >
      <span aria-hidden className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-red-600" />
      <span>{children}</span>
    </p>
  );
}
