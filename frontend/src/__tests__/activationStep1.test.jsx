/**
 * Step 1 — Account, the entry point of the commercial activation flow.
 *
 * WHAT THESE GUARD
 * ----------------
 * This screen is the only thing standing between a stranger and a provisioned
 * tenant, and the only place a visitor reads a price before they have an
 * account. So the tests are grouped around the three things that would actually
 * cost something if they broke:
 *
 *   1. **The gate.** Nothing is submitted until every field is valid and the
 *      terms box is ticked, and a double click cannot create two accounts.
 *   2. **The money.** Every figure on the plan summary comes from the server.
 *      A hardcoded "$8" or a locally computed trial-end date would look correct
 *      right up until someone changed the price.
 *   3. **The hand-off.** A successful signup lands on Step 2 — Verify, with the
 *      session stored, and a failed one says something a person can act on.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within, act } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// Classic JSX runtime under vitest — see the note in authShell.test.jsx.
globalThis.React = React;

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

const mockLogin = vi.fn().mockResolvedValue(null);
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ login: mockLogin, logout: vi.fn(), isAuthenticated: false, user: null }),
  AuthProvider: ({ children }) => children,
}));

// No site key in test, so the widget renders nothing and the form does not wait
// for a token — mirroring an unconfigured deployment.
vi.mock("../components/auth/challengeConfig", () => ({
  TURNSTILE_SITE_KEY: "",
  isChallengeConfigured: () => false,
}));

import Start from "../pages/Start";

// ── Fixtures ─────────────────────────────────────────────────────────────────

/** What GET /api/v1/billing/quote/preview serves. */
const OFFER = {
  plan: "verified",
  plan_label: "Temveris Verified",
  currency: "USD",
  unit_price_monthly_cents: 800,
  effective_unit_price_monthly_cents: 640,
  billing_cycle: "annual",
  annual_discount_pct: 20.0,
  trial_length_days: 14,
  // Deliberately NOT "today + trial_length_days". The panel must render the
  // timestamp it was served; a component quietly doing the arithmetic itself
  // would agree with a matching fixture and hide the bug.
  trial_ends_at: "2026-09-20T10:00:00+00:00",
  amount_due_today_cents: 0,
  card_required: false,
  terms_version: "2026-05-01",
  privacy_version: "2026-05-01",
};

const SIGNUP_OK = {
  signup_id: "sgn_1", user_id: 7, organization_id: "org_1",
  operational_org_id: 42, workspace_id: "wsp_1",
  commercial_state: "trial_active", verification_required: true,
  redirect_url: "/onboarding", message: "Workspace created successfully.",
  access_token: "jwt-token", token_type: "bearer", expires_in: 3600,
  role: "admin", setup_required: true,
};

const VALID = {
  email: "  Jane@Company.com  ",     // deliberately untrimmed / mixed case
  first: "Jane",
  last: "Doe",
  password: "Passw0rd!23",
};

/**
 * Route every request the page makes.
 * @param signup  a function (or value) producing the signup Response.
 * @param offer   `false` to fail the offer request.
 */
function mockApi({ signup, offer = OFFER } = {}) {
  const calls = { signup: [], offer: 0 };
  const fetchMock = vi.fn(async (url, init) => {
    if (String(url).includes("/billing/quote/preview")) {
      calls.offer += 1;
      if (offer === false) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => offer };
    }
    if (String(url).includes("/api/v1/signup")) {
      calls.signup.push(init);
      const res = typeof signup === "function" ? await signup(calls.signup.length) : signup;
      return res || { ok: true, status: 200, json: async () => SIGNUP_OK };
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const ok = (body, status = 200) => ({ ok: true, status, json: async () => body });
const fail = (body, status) => ({ ok: false, status, json: async () => body });

function renderPage(url = "/start") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/start" element={<Start />} />
      </Routes>
    </MemoryRouter>,
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────
const field = (id) => document.getElementById(id);
const type = (id, value) => fireEvent.change(field(id), { target: { value } });
const blurField = (id) => fireEvent.blur(field(id));
const termsBox = () => screen.getByRole("checkbox");
const submitBtn = () =>
  screen.getByRole("button", { name: /create my trial workspace|continue to payment|creating your workspace/i });

function fillValidForm({ terms = true } = {}) {
  type("zt-email", VALID.email);
  type("zt-first-name", VALID.first);
  type("zt-last-name", VALID.last);
  type("zt-password", VALID.password);
  if (terms) fireEvent.click(termsBox());
}

let errorSpy;
beforeEach(() => {
  mockNavigate.mockClear();
  mockLogin.mockClear();
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  errorSpy.mockRestore();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});


// ═════════════════════════════════════════════════════════════════════════════
// Structure
// ═════════════════════════════════════════════════════════════════════════════

describe("Step 1 — page structure", () => {
  it("shows all four steps with Account as the current one", async () => {
    mockApi();
    renderPage();

    const progress = screen.getByRole("navigation", { name: /activation progress/i });
    for (const label of ["Account", "Verify", "Workspace", "Ready"]) {
      expect(within(progress).getByText(label)).toBeInTheDocument();
    }
    // Not carried by fill colour alone: the active step is marked in the
    // accessibility tree too.
    const current = progress.querySelector('[aria-current="step"]');
    expect(current).toHaveTextContent("Account");
    expect(within(progress).getByText(/Step 1 of 4: current step/i)).toBeInTheDocument();
  });

  it("renders the heading, the supporting line and every field", async () => {
    mockApi();
    renderPage();

    expect(screen.getByRole("heading", { name: /create your temveris account/i })).toBeInTheDocument();
    await screen.findByText(/start your 14-day free trial\. no credit card required\./i);

    expect(field("zt-email")).toHaveAttribute("placeholder", "name@company.com");
    expect(field("zt-first-name")).toHaveAttribute("placeholder", "Jane");
    expect(field("zt-last-name")).toHaveAttribute("placeholder", "Doe");
    expect(field("zt-password")).toHaveAttribute("placeholder", "At least 8 characters");
    expect(field("zt-password")).toHaveAttribute("type", "password");
  });

  it("labels every input so it is reachable by its name", () => {
    mockApi();
    renderPage();
    // Exact strings: /password/i would also match the "Show password" toggle.
    for (const [label, id] of [
      ["Work email", "zt-email"],
      ["First name", "zt-first-name"],
      ["Last name", "zt-last-name"],
      ["Password", "zt-password"],
    ]) {
      expect(screen.getByLabelText(label, { exact: true })).toBe(field(id));
    }
    expect(termsBox()).toHaveAccessibleName(/i agree to the terms of service and privacy policy/i);
  });

  it("links Terms of Service and Privacy Policy to the legal routes", () => {
    mockApi();
    renderPage();
    const terms = screen.getByRole("link", { name: /terms of service/i });
    const privacy = screen.getByRole("link", { name: /privacy policy/i });
    expect(terms).toHaveAttribute("href", "/terms");
    expect(privacy).toHaveAttribute("href", "/privacy");
    // Opened in a new tab, so reading them does not discard a part-filled form.
    for (const el of [terms, privacy]) {
      expect(el).toHaveAttribute("target", "_blank");
      expect(el.getAttribute("rel")).toContain("noopener");
    }
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// The plan summary — server-authoritative
// ═════════════════════════════════════════════════════════════════════════════

describe("Step 1 — plan summary", () => {
  it("renders price, cadence and trial end from the quote preview", async () => {
    mockApi();
    renderPage();

    const panel = screen.getByRole("complementary", { name: /your plan summary/i });
    expect(await within(panel).findByText("Temveris Verified")).toBeInTheDocument();
    expect(within(panel).getByText(/\$0 due today/)).toBeInTheDocument();
    expect(within(panel).getByText(/no card required/i)).toBeInTheDocument();
    expect(within(panel).getByText("$8 / worker / mo")).toBeInTheDocument();
    expect(within(panel).getByText("Annual (Save 20%)")).toBeInTheDocument();
    expect(within(panel).getByText(/no automatic charge without your authorisation/i))
      .toBeInTheDocument();
  });

  it("formats the trial end date from the served timestamp, not from today", async () => {
    // Asserted against the reader's locale rather than a pinned "September 7,
    // 2026" — the panel formats for whoever is looking, and CI is not
    // guaranteed to be en-US. What is actually under test is the *source* of
    // the date, which the second half pins.
    const fmt = (d) => new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "long", day: "numeric",
    }).format(d);
    const served = fmt(new Date(OFFER.trial_ends_at));
    const localGuess = fmt(new Date(Date.now() + 14 * 86400_000));

    mockApi();
    renderPage();
    const panel = screen.getByRole("complementary", { name: /your plan summary/i });
    expect(await within(panel).findByText(served)).toBeInTheDocument();

    // A page deriving `today + 14` itself would render this instead. The
    // fixture is a fixed past-or-future date, so the two never coincide.
    expect(served).not.toBe(localGuess);
    expect(within(panel).queryByText(localGuess)).not.toBeInTheDocument();
  });

  it("takes the price from the response rather than a built-in constant", async () => {
    // The whole point of the endpoint: change the price server-side and the
    // panel changes with it.
    mockApi({ offer: { ...OFFER, unit_price_monthly_cents: 1500, plan_label: "Temveris Governed" } });
    renderPage();
    const panel = screen.getByRole("complementary", { name: /your plan summary/i });
    expect(await within(panel).findByText("$15 / worker / mo")).toBeInTheDocument();
    expect(within(panel).queryByText("$8 / worker / mo")).not.toBeInTheDocument();
  });

  it("takes the trial length in the supporting copy from the server too", async () => {
    mockApi({ offer: { ...OFFER, trial_length_days: 30 } });
    renderPage();
    expect(await screen.findByText(/start your 30-day free trial/i)).toBeInTheDocument();
  });

  it("requests the plan named in the query string", async () => {
    const calls = mockApi();
    renderPage("/start?plan=governed");
    await waitFor(() => expect(calls.offer).toBe(1));
    expect(fetch.mock.calls[0][0]).toContain("plan=governed");
  });

  it("degrades the panel without blocking signup when the quote API fails", async () => {
    mockApi({ offer: false });
    renderPage();

    const panel = screen.getByRole("complementary", { name: /your plan summary/i });
    expect(await within(panel).findByText(/couldn't load your plan summary/i)).toBeInTheDocument();
    // The form beside it is untouched.
    fillValidForm();
    expect(submitBtn()).toBeEnabled();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Validation
// ═════════════════════════════════════════════════════════════════════════════

describe("Step 1 — validation", () => {
  it("keeps the button disabled until the form is complete", () => {
    mockApi();
    renderPage();
    expect(submitBtn()).toBeDisabled();
    fillValidForm();
    expect(submitBtn()).toBeEnabled();
  });

  it("will not submit without the terms checkbox", async () => {
    const calls = mockApi();
    renderPage();

    fillValidForm({ terms: false });
    expect(termsBox()).not.toBeChecked();
    expect(submitBtn()).toBeDisabled();

    fireEvent.submit(field("zt-email").closest("form"));
    await waitFor(() =>
      expect(screen.getByText(/please accept the terms of service and privacy policy/i))
        .toBeInTheDocument());
    expect(calls.signup).toHaveLength(0);
  });

  it("reports each required field that was left empty", async () => {
    mockApi();
    renderPage();
    fireEvent.submit(screen.getByRole("checkbox").closest("form"));

    for (const msg of [
      /enter your work email address/i,
      /enter your first name/i,
      /enter your last name/i,
      /choose a password/i,
    ]) {
      expect(await screen.findByText(msg)).toBeInTheDocument();
    }
  });

  it("rejects a malformed email on blur and marks the field invalid", async () => {
    mockApi();
    renderPage();
    type("zt-email", "not-an-email");
    blurField("zt-email");

    await screen.findByText(/enter a valid email address/i);
    expect(field("zt-email")).toHaveAttribute("aria-invalid", "true");
    // The message is wired to the input, so a screen reader hears it on focus.
    const describedBy = field("zt-email").getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy)).toHaveTextContent(/enter a valid email address/i);
  });

  it("enforces the backend password policy, not a weaker one", async () => {
    mockApi();
    renderPage();

    // Eight characters, which is all the old form asked for — but the platform
    // policy also wants an uppercase letter, a digit and a symbol from @$!%*?&.
    type("zt-password", "password");
    blurField("zt-password");
    const msg = await screen.findByText(/your password still needs/i);
    expect(msg).toHaveTextContent(/uppercase letter/i);
    expect(msg).toHaveTextContent(/a number/i);
    expect(msg).toHaveTextContent(/special character/i);

    // A symbol the backend does NOT count must not satisfy the frontend either,
    // or the form passes it and the server 422s.
    type("zt-password", "Password1-");
    await screen.findByText(/special character/i);

    type("zt-password", VALID.password);
    await waitFor(() =>
      expect(screen.queryByText(/your password still needs/i)).not.toBeInTheDocument());
  });

  it("rejects a password under eight characters", async () => {
    mockApi();
    renderPage();
    type("zt-password", "Pw1!a");
    blurField("zt-password");
    expect(await screen.findByText(/at least 8 characters/i)).toBeInTheDocument();
  });

  it("holds errors back until a field is blurred", () => {
    mockApi();
    renderPage();
    type("zt-email", "j");
    // Still mid-word — nagging here is what the touched-state exists to avoid.
    expect(screen.queryByText(/enter a valid email address/i)).not.toBeInTheDocument();
  });

  it("offers a password reveal toggle", () => {
    mockApi();
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /show password/i }));
    expect(field("zt-password")).toHaveAttribute("type", "text");
    fireEvent.click(screen.getByRole("button", { name: /hide password/i }));
    expect(field("zt-password")).toHaveAttribute("type", "password");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Submission
// ═════════════════════════════════════════════════════════════════════════════

describe("Step 1 — submission", () => {
  it("posts a normalised payload to the existing signup endpoint", async () => {
    const calls = mockApi();
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    await waitFor(() => expect(calls.signup).toHaveLength(1));
    const body = JSON.parse(calls.signup[0].body);

    // Trimmed and lower-cased exactly as the server stores it.
    expect(body.email).toBe("jane@company.com");
    expect(body.first_name).toBe("Jane");
    expect(body.last_name).toBe("Doe");
    expect(body.intent).toBe("trial");
    expect(body.plan).toBe("verified");
    // The organization is named on the Workspace step, not here.
    expect(body.company_name).toBeUndefined();
    // Consent is stamped with the versions the server served.
    expect(body.terms_version).toBe(OFFER.terms_version);
    expect(body.privacy_version).toBe(OFFER.privacy_version);
  });

  it("sends an idempotency key so a retry cannot create a second account", async () => {
    const calls = mockApi();
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());
    await waitFor(() => expect(calls.signup).toHaveLength(1));
    expect(calls.signup[0].headers["Idempotency-Key"]).toBeTruthy();
  });

  it("shows a loading state and blocks a second click while in flight", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    const calls = mockApi({ signup: async () => { await gate; return ok(SIGNUP_OK); } });

    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    await screen.findByText(/creating your workspace/i);
    expect(submitBtn()).toBeDisabled();

    // Hammer it — the in-flight guard and the disabled button both hold.
    fireEvent.click(submitBtn());
    fireEvent.click(submitBtn());
    fireEvent.submit(field("zt-email").closest("form"));

    await act(async () => { release(); await gate; });
    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    expect(calls.signup).toHaveLength(1);
  });

  it("stores the session and navigates to Step 2 — Verify", async () => {
    mockApi();
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith("jwt-token", null, { remember: true }));
    expect(mockNavigate).toHaveBeenCalledWith(
      "/verify-email?email=jane%40company.com",
      expect.objectContaining({ replace: true }),
    );
  });

  it("still reaches Verify when the server could not issue a session", async () => {
    // Session issuance is non-fatal server-side; the tenant exists either way.
    const { access_token, ...noSession } = SIGNUP_OK;   // eslint-disable-line no-unused-vars
    mockApi({ signup: ok(noSession) });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    expect(mockLogin).not.toHaveBeenCalled();
    expect(mockNavigate.mock.calls[0][0]).toContain("/verify-email");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Errors
// ═════════════════════════════════════════════════════════════════════════════

describe("Step 1 — error handling", () => {
  it("reports an already-registered email against the field, with a way out", async () => {
    mockApi({
      signup: fail({
        error: {
          code: "EMAIL_ALREADY_EXISTS",
          message: "An account already exists for this email address.",
          field: "email", recoverable: true, recovery_action: "login_or_reset_password",
          trace_id: "trc_1", timestamp: "2026-08-24T00:00:00Z",
        },
      }, 409),
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    // Two alerts, deliberately: a short marker on the field and one banner
    // carrying the explanation and the ways out.
    const alerts = await screen.findAllByRole("alert");
    const banner = alerts.find((a) => within(a).queryByRole("link", { name: /^sign in$/i }));
    expect(banner).toHaveTextContent(/an account already exists/i);
    expect(within(banner).getByRole("link", { name: /reset your password/i })).toBeInTheDocument();

    expect(field("zt-email")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/this email is already registered/i)).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("maps a backend validation error back onto its field", async () => {
    mockApi({
      signup: fail({
        detail: [{ loc: ["body", "password"], msg: "Value error, Password must contain a number." }],
      }, 422),
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    expect(await screen.findByText("Password must contain a number.")).toBeInTheDocument();
    expect(field("zt-password")).toHaveAttribute("aria-invalid", "true");
  });

  it("explains a rate limit instead of showing a raw 429", async () => {
    mockApi({ signup: fail({ detail: "Rate limit exceeded" }, 429) });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());
    expect(await screen.findByText(/too many signup attempts/i)).toBeInTheDocument();
  });

  it("recovers from a network failure and lets the visitor retry", async () => {
    let attempt = 0;
    const calls = mockApi({
      signup: async () => {
        attempt += 1;
        if (attempt === 1) throw new TypeError("Failed to fetch");
        return ok(SIGNUP_OK);
      },
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    expect(await screen.findByText(/couldn't reach the signup service/i)).toBeInTheDocument();
    expect(submitBtn()).toBeEnabled();

    fireEvent.click(submitBtn());
    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    // The retry reuses the key, so the server can replay rather than duplicate.
    expect(calls.signup[0].headers["Idempotency-Key"])
      .toBe(calls.signup[1].headers["Idempotency-Key"]);
  });

  it("mints a fresh idempotency key once the form has been edited", async () => {
    const calls = mockApi({
      signup: async (n) => (n === 1 ? fail({ error: { code: "X", message: "no" } }, 400) : ok(SIGNUP_OK)),
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());
    await screen.findByRole("alert");

    // A corrected resubmission is a different request, not a replay of the old.
    type("zt-email", "jane.doe@company.com");
    fireEvent.click(submitBtn());
    await waitFor(() => expect(calls.signup).toHaveLength(2));
    expect(calls.signup[0].headers["Idempotency-Key"])
      .not.toBe(calls.signup[1].headers["Idempotency-Key"]);
  });

  it("shows the review message without navigating when signup is held", async () => {
    mockApi({
      signup: ok({
        signup_id: "sgn_1", manual_review_required: true, case_id: "case_1",
        message: "Your signup is under review. We'll email you within one business day.",
      }, 202),
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    expect(await screen.findByText(/under review/i)).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
    // Nothing was created, so re-submitting must not be possible from here.
    expect(submitBtn()).toBeDisabled();
  });

  it("hands off to sales for an enterprise-routed signup", async () => {
    mockApi({
      signup: ok({
        signup_id: "sgn_1", route: "enterprise", handoff: "sales",
        redirect_url: "/contact-sales?route=enterprise",
        message: "Routed to our team — we'll be in touch.",
      }),
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    expect(await screen.findByText(/routed to our team/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /continue/i }))
      .toHaveAttribute("href", "/contact-sales?route=enterprise");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("shows a friendly message for an unexpected server error", async () => {
    mockApi({
      signup: fail({
        error: {
          code: "INTERNAL",
          message: "Something went wrong. Please try again or contact support.",
          recoverable: true, trace_id: "trc_9", timestamp: "2026-08-24T00:00:00Z",
        },
      }, 500),
    });
    renderPage();
    fillValidForm();
    fireEvent.click(submitBtn());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/something went wrong/i);
    // No internals leak to the visitor.
    expect(alert.textContent).not.toMatch(/traceback|sqlalchemy|psycopg|File "/i);
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// The hand-off target — /verify-email as Step 2's landing
// ═════════════════════════════════════════════════════════════════════════════

describe("Step 2 landing — arriving from Step 1", () => {
  async function renderVerify(url) {
    const { default: VerifyEmail } = await import("../pages/VerifyEmail");
    return render(
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/verify-email" element={<VerifyEmail />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("lands on Step 2 rather than reporting a missing token", async () => {
    // Step 1 navigates here with no token. Originally that rendered
    // "We couldn't verify that link. Missing token." — a failure screen shown
    // to someone whose signup had just succeeded. It now renders Step 2.
    //
    // What Step 2 then does with the code, the countdown and the organisation
    // form is covered in activationStep2.test.jsx; this only pins the hand-off.
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await renderVerify("/verify-email?email=jane%40company.com");

    expect(await screen.findByRole("heading", { name: /verify your email/i })).toBeInTheDocument();
    expect(screen.queryByText(/couldn't verify/i)).not.toBeInTheDocument();
    // The token endpoint is not called without a token — a refresh here cannot
    // consume anything or duplicate activation state.
    const calledVerify = fetchMock.mock.calls.some(
      (c) => String(c[0]).includes("/email-verification/verify"),
    );
    expect(calledVerify).toBe(false);
  });

  it("offers a way back to Step 1 to correct the address", async () => {
    // There is no email-change flow; `users.email` is the identity the session,
    // audit trail and consent record are keyed to. Correcting a typo means
    // returning to the Account step.
    vi.stubGlobal("fetch", vi.fn());
    await renderVerify("/verify-email");
    const back = await screen.findByRole("link", { name: /use a different email|back to sign up/i });
    expect(back).toHaveAttribute("href", "/start");
  });

  it("still consumes a token when the emailed link supplies one", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({ verified: true, user_id: 7, organization_id: "org_1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderVerify("/verify-email?token=raw-token");

    expect(await screen.findByRole("heading", { name: /you're verified/i })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/email-verification/verify");
  });

  it("still reports a token that the server rejected", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 400,
      json: async () => ({ error: { code: "TOKEN_EXPIRED", message: "This link has expired." } }),
    }));

    await renderVerify("/verify-email?token=stale");
    expect(await screen.findByRole("heading", { name: /couldn't verify that link/i })).toBeInTheDocument();
    expect(screen.getByText(/this link has expired/i)).toBeInTheDocument();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Subscribe mode — the pricing page's "Subscribe now" button
//
// The same form serves both CTAs. A paying customer gives exactly the same four
// fields, so a second signup page would only duplicate the password policy,
// consent capture, bot challenge, fraud checks and idempotency key. What
// `?mode=subscribe` changes is the copy, the summary panel and the destination —
// never what is sent to the API.
// ═════════════════════════════════════════════════════════════════════════════
describe("subscribe mode", () => {
  const SUBSCRIBE = "/start?plan=governed&mode=subscribe&cycle=annual&seats=25";

  it("drops the trial wording", async () => {
    mockApi();
    renderPage(SUBSCRIBE);
    await screen.findByRole("heading", { name: /create your temveris account/i });
    expect(screen.getByText(/choose seats and pay on the next step/i)).toBeInTheDocument();
    expect(screen.queryByText(/day free trial\. No credit card required/i)).not.toBeInTheDocument();
  });

  it("labels the button for payment, not for a trial", async () => {
    mockApi();
    renderPage(SUBSCRIBE);
    await screen.findByRole("heading", { name: /create your temveris account/i });
    expect(screen.getByRole("button", { name: /continue to payment/i })).toBeInTheDocument();
  });

  it("shows no total, because no seat count has been chosen yet", async () => {
    // A figure here would be the marketing page's guess or browser arithmetic.
    // The authoritative total is quoted on the configurator.
    mockApi();
    renderPage(SUBSCRIBE);
    await screen.findByText(/You'll choose seats and see the total next/i);
    expect(screen.queryByText(/due today/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Trial ends/i)).not.toBeInTheDocument();
  });

  it("takes the billing cadence from the link", async () => {
    mockApi();
    renderPage(SUBSCRIBE);
    await screen.findByText(/You'll choose seats and see the total next/i);
    // 20% is the server's number from the offer, not a literal.
    expect(screen.getByText(`Annual (Save ${OFFER.annual_discount_pct}%)`)).toBeInTheDocument();
  });

  it("goes to checkout, carrying the plan, cycle and seats", async () => {
    mockApi();
    renderPage(SUBSCRIBE);
    fillValidForm();
    fireEvent.click(submitBtn());

    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    expect(mockNavigate).toHaveBeenCalledWith(
      "/subscription/configure?plan=governed&cycle=annual&seats=25",
      expect.objectContaining({ replace: true }),
    );
    // Not to Verify — a paying customer is not sent to check their inbox first.
    expect(mockNavigate).not.toHaveBeenCalledWith(
      expect.stringContaining("/verify-email"), expect.anything(),
    );
  });

  it("still signs up as a TRIAL, whatever the UI says", async () => {
    // The trap this guards: provisioning reads
    //   state = "trial_active" if intent == "trial" else "active"
    // so sending "paid" would mark the subscription active before any payment,
    // leaving anyone who abandons Stripe an active, unpaid tenant with no trial
    // clock to expire them.
    const calls = mockApi();
    renderPage(SUBSCRIBE);
    fillValidForm();
    fireEvent.click(submitBtn());

    await waitFor(() => expect(calls.signup).toHaveLength(1));
    expect(JSON.parse(calls.signup[0].body).intent).toBe("trial");
  });

  it("sends the same payload the trial path sends", async () => {
    const calls = mockApi();
    renderPage(SUBSCRIBE);
    fillValidForm();
    fireEvent.click(submitBtn());
    await waitFor(() => expect(calls.signup).toHaveLength(1));
    const body = JSON.parse(calls.signup[0].body);

    expect(body.plan).toBe("governed");          // from the link
    // VALID.email is deliberately padded and mixed-case; the same trim+lowercase
    // normalisation applies on this path as on the trial one.
    expect(body.email).toBe(VALID.email.trim().toLowerCase());
    expect(body.terms_version).toBe(OFFER.terms_version);
    // No mode, cycle or seats leak into the signup contract — they are UI state.
    expect(body).not.toHaveProperty("mode");
    expect(body).not.toHaveProperty("cycle");
    expect(body).not.toHaveProperty("seats");
  });

  it("hides the four-step indicator, since the next screen is checkout", async () => {
    mockApi();
    renderPage(SUBSCRIBE);
    await screen.findByRole("heading", { name: /create your temveris account/i });
    expect(screen.queryByRole("navigation", { name: /activation progress/i })).toBeNull();
    expect(screen.getByAltText("Temveris")).toBeInTheDocument();   // branding stays
  });

  it("leaves the trial path untouched", async () => {
    mockApi();
    renderPage("/start?plan=governed");
    await screen.findByRole("heading", { name: /create your temveris account/i });
    expect(screen.getByText(/free trial/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create my trial workspace/i })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /activation progress/i })).toBeInTheDocument();
  });
});
