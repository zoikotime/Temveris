/**
 * Step 2 — Verify, the second screen of the commercial activation flow.
 *
 * WHAT THESE GUARD
 * ----------------
 *   1. **The code entry.** Six cells that behave the way people expect a code
 *      field to behave — paste, auto-advance, backspace — because this is the
 *      one screen between a signed-up account and a usable workspace.
 *   2. **Server authority.** The address, the resend countdown and the worker
 *      bands are all rendered from what the server sent. A local constant that
 *      happened to match today would silently drift the moment the backend
 *      changed.
 *   3. **The hand-off.** Verification reveals the organisation form; the form
 *      posts to the endpoint that UPDATES the existing organisation and then
 *      enters the existing Step 3 route.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within, fireEvent, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// Classic JSX runtime under vitest — see the note in authShell.test.jsx.
globalThis.React = React;

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

// authFetch adds a bearer token and a refresh-and-replay; neither is the
// subject here, so it is stubbed at the module boundary and asserted on.
const mockAuthFetch = vi.fn();
vi.mock("../api/authFetch", () => ({
  authFetch: (...args) => mockAuthFetch(...args),
  authFetchJson: vi.fn(),
  default: (...args) => mockAuthFetch(...args),
}));

import VerifyEmail from "../pages/VerifyEmail";

// ── Fixtures ─────────────────────────────────────────────────────────────────
const STATUS_UNVERIFIED = {
  email: "ada@temveris.example",
  verified: false,
  code_length: 6,
  resend_cooldown_seconds: 60,
  resend_available_in: 22,
};

const SETUP = {
  current_step: "organization",
  setup_required: true,
  completed: false,
  organization: {
    id: 42,
    company_name: "Ada's workspace",   // the provisional name Step 1 seeded
    default_timezone: "Europe/London",
    primary_country: "GB",
  },
  admin: { id: 7, name: "Ada", email: "ada@temveris.example", role: "admin" },
  teams: { count: 0, limit: null },
  seats: { used: 1, limit: 25 },
  worker_count_bands: [
    { value: "1-10", label: "1–10 active workers", expected_users: 10 },
    { value: "11-50", label: "11–50 active workers", expected_users: 50 },
    { value: "51-200", label: "51–200 active workers", expected_users: 200 },
  ],
};

const ok = (body, status = 200) => ({ ok: true, status, json: async () => body });
const fail = (body, status) => ({ ok: false, status, json: async () => body });

/**
 * Route the three calls Step 2 makes.
 * @param status  the verification-status body, or a function of call count
 * @param verify  the verify-code response
 * @param save    the organisation-save response
 */
function mockApi({ status = STATUS_UNVERIFIED, verify, save, setup = SETUP } = {}) {
  const calls = { status: 0, verify: [], save: [], resend: 0 };
  mockAuthFetch.mockImplementation(async (url, init) => {
    const u = String(url);
    if (u.includes("/email-verification/status")) {
      calls.status += 1;
      const body = typeof status === "function" ? status(calls.status) : status;
      return body === false ? fail({}, 401) : ok(body);
    }
    if (u.includes("/email-verification/verify-code")) {
      calls.verify.push(JSON.parse(init.body));
      return (typeof verify === "function" ? verify(calls.verify.length) : verify)
        || ok({ verified: true, user_id: 7 });
    }
    if (u.includes("/email-verification/resend")) {
      calls.resend += 1;
      return ok({ resent: true }, 202);
    }
    if (u.includes("/onboarding/setup")) return ok(setup);
    if (u.includes("/onboarding/organization")) {
      calls.save.push(JSON.parse(init.body));
      return (typeof save === "function" ? save(calls.save.length) : save)
        || ok({ ...setup, current_step: "teams" });
    }
    throw new Error(`unexpected authFetch: ${u}`);
  });
  return calls;
}

function renderAt(url = "/verify-email") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/verify-email" element={<VerifyEmail />} />
      </Routes>
    </MemoryRouter>,
  );
}

const cells = () => screen.getAllByRole("textbox", { name: /digit \d of 6/i });
const saveBtn = () => screen.getByRole("button", { name: /continue|saving/i });

/** Fill the six cells the way a person would — one keystroke per cell. */
function typeCode(value) {
  const boxes = cells();
  for (let i = 0; i < value.length && i < boxes.length; i++) {
    fireEvent.change(boxes[i], { target: { value: value[i] } });
  }
}

let errorSpy;
beforeEach(() => {
  vi.useRealTimers();
  mockNavigate.mockClear();
  mockAuthFetch.mockReset();
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  errorSpy.mockRestore();
  vi.restoreAllMocks();
});


// ═════════════════════════════════════════════════════════════════════════════
// Structure
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 2 — page structure", () => {
  it("marks Account complete and Verify current", async () => {
    mockApi();
    renderAt();

    const progress = screen.getByRole("navigation", { name: /activation progress/i });
    expect(within(progress).getByText(/Step 1 of 4: completed/i)).toBeInTheDocument();
    expect(within(progress).getByText(/Step 2 of 4: current step/i)).toBeInTheDocument();
    expect(progress.querySelector('[aria-current="step"]')).toHaveTextContent("Verify");
  });

  it("shows the heading and the address the server reported", async () => {
    mockApi();
    renderAt();
    expect(screen.getByRole("heading", { name: /verify your email/i })).toBeInTheDocument();
    expect(await screen.findByText("ada@temveris.example")).toBeInTheDocument();
    expect(screen.getByText(/enter the 6-digit code we sent to/i)).toBeInTheDocument();
  });

  it("prefers the session address over the one in the query string", async () => {
    // The query-string value is a hint for the signed-out case, never the
    // identity — trusting it would let a link claim any address.
    mockApi();
    renderAt("/verify-email?email=attacker%40evil.example.com");
    expect(await screen.findByText("ada@temveris.example")).toBeInTheDocument();
    expect(screen.queryByText("attacker@evil.example.com")).not.toBeInTheDocument();
  });

  it("renders exactly six code cells", async () => {
    mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));
    expect(cells()[0]).toHaveAttribute("inputmode", "numeric");
    expect(cells()[0]).toHaveAttribute("autocomplete", "one-time-code");
  });

  it("renders the Why we ask panel", async () => {
    mockApi();
    renderAt();
    const panel = screen.getByRole("complementary", { name: /why we ask/i });
    for (const t of ["Privacy by design", "Secure provisioning",
                     "Right controls", "Audit ready"]) {
      // Exact strings: "right controls" also appears inside the panel's own
      // description sentence.
      expect(within(panel).getByText(t, { exact: true })).toBeInTheDocument();
    }
    expect(within(panel).getByText(/we verify work, not private lives/i)).toBeInTheDocument();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Code entry
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 2 — code entry", () => {
  it("advances between cells as you type and submits when full", async () => {
    const calls = mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    typeCode("123456");
    await waitFor(() => expect(calls.verify).toHaveLength(1));
    expect(calls.verify[0]).toEqual({ code: "123456" });
  });

  it("distributes a pasted code across the cells", async () => {
    const calls = mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    fireEvent.paste(cells()[0], {
      clipboardData: { getData: () => "  98 76 54  " },   // stripped to digits
    });
    await waitFor(() => expect(calls.verify).toHaveLength(1));
    expect(calls.verify[0]).toEqual({ code: "987654" });
  });

  it("drops non-numeric input instead of showing then rejecting it", async () => {
    mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    fireEvent.change(cells()[0], { target: { value: "a" } });
    expect(cells()[0]).toHaveValue("");
  });

  it("rewinds on backspace", async () => {
    mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    typeCode("12");
    expect(cells()[1]).toHaveValue("2");
    fireEvent.keyDown(cells()[1], { key: "Backspace" });
    expect(cells()[1]).toHaveValue("");
    fireEvent.keyDown(cells()[1], { key: "Backspace" });
    expect(cells()[0]).toHaveValue("");
  });

  it("moves between cells with the arrow keys", async () => {
    mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    cells()[2].focus();
    fireEvent.keyDown(cells()[2], { key: "ArrowLeft" });
    expect(document.activeElement).toBe(cells()[1]);
    fireEvent.keyDown(cells()[1], { key: "ArrowRight" });
    expect(document.activeElement).toBe(cells()[2]);
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Verification outcomes
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 2 — verification", () => {
  it("reveals the organisation form once verified", async () => {
    mockApi();
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    expect(screen.queryByText(/set up your organisation/i)).not.toBeInTheDocument();
    typeCode("123456");
    expect(await screen.findByText(/set up your organisation/i)).toBeInTheDocument();
    expect(screen.getByText(/email verified/i)).toBeInTheDocument();
  });

  it("goes straight to the organisation form when already verified", async () => {
    // The refresh-after-success case: nothing is re-submitted, the server's
    // settled state is simply rendered.
    mockApi({ status: { ...STATUS_UNVERIFIED, verified: true } });
    renderAt();
    expect(await screen.findByText(/set up your organisation/i)).toBeInTheDocument();
  });

  it("shows the server's message for a wrong code", async () => {
    mockApi({
      verify: fail({
        error: { code: "VERIFICATION_CODE_INVALID", field: "code",
                 message: "That code isn't right. Check the email and try again." },
      }, 400),
    });
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    typeCode("111111");
    expect(await screen.findByText(/that code isn't right/i)).toBeInTheDocument();
    expect(screen.queryByText(/set up your organisation/i)).not.toBeInTheDocument();
    expect(cells()[0]).toHaveAttribute("aria-invalid", "true");
  });

  it("clears the cells after a wrong code so the next attempt starts clean", async () => {
    mockApi({
      verify: fail({ error: { code: "VERIFICATION_CODE_INVALID", message: "Nope." } }, 400),
    });
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    typeCode("111111");
    await screen.findByText("Nope.");
    await waitFor(() => expect(cells()[0]).toHaveValue(""));
  });

  it("disables entry once the code is locked", async () => {
    mockApi({
      verify: fail({
        error: { code: "VERIFICATION_CODE_LOCKED",
                 message: "Too many incorrect codes. Send yourself a new one to try again." },
      }, 429),
    });
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    typeCode("111111");
    expect(await screen.findByText(/too many incorrect codes/i)).toBeInTheDocument();
    await waitFor(() => expect(cells()[0]).toBeDisabled());
  });

  it("reports an expired code with a way forward", async () => {
    mockApi({
      verify: fail({
        error: { code: "VERIFICATION_CODE_EXPIRED",
                 message: "That code has expired. Send yourself a new one." },
      }, 400),
    });
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));
    typeCode("111111");
    expect(await screen.findByText(/that code has expired/i)).toBeInTheDocument();
  });

  it("survives a network failure without losing the page", async () => {
    mockAuthFetch.mockImplementation(async (url) => {
      if (String(url).includes("/email-verification/status")) return ok(STATUS_UNVERIFIED);
      throw new TypeError("Failed to fetch");
    });
    renderAt();
    await waitFor(() => expect(cells()).toHaveLength(6));

    typeCode("123456");
    expect(await screen.findByText(/couldn't reach the verification service/i)).toBeInTheDocument();
  });

  it("degrades to sign-in prompts when the session is missing", async () => {
    // Signup issues a session, but issuance is non-fatal server-side.
    mockApi({ status: false });
    renderAt("/verify-email?email=ada%40temveris.example");
    expect(await screen.findByRole("link", { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to sign up/i })).toHaveAttribute("href", "/start");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Resend + different email
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 2 — resend", () => {
  it("shows the countdown the server reported, not a local constant", async () => {
    mockApi({ status: { ...STATUS_UNVERIFIED, resend_available_in: 43 } });
    renderAt();
    // 43 came from the server. A hardcoded 22s — the number in the mockup —
    // would be wrong the moment the backend cooldown changed.
    expect(await screen.findByText(/resend in 43s/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resend code/i })).not.toBeInTheDocument();
  });

  it("offers resend once the cooldown has elapsed", async () => {
    const calls = mockApi({ status: { ...STATUS_UNVERIFIED, resend_available_in: 0 } });
    renderAt();

    const btn = await screen.findByRole("button", { name: /resend code/i });
    fireEvent.click(btn);
    await waitFor(() => expect(calls.resend).toBe(1));
    expect(await screen.findByText(/a new code is on its way/i)).toBeInTheDocument();
  });

  it("re-reads the authoritative cooldown after resending", async () => {
    const calls = mockApi({
      status: (n) => ({ ...STATUS_UNVERIFIED, resend_available_in: n === 1 ? 0 : 60 }),
    });
    renderAt();

    fireEvent.click(await screen.findByRole("button", { name: /resend code/i }));
    // The countdown restarts from what the server says, not from a guess.
    expect(await screen.findByText(/resend in 60s/i)).toBeInTheDocument();
    expect(calls.status).toBeGreaterThanOrEqual(2);
  });

  it("counts down once a second", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi({ status: { ...STATUS_UNVERIFIED, resend_available_in: 3 } });
    renderAt();
    await vi.waitFor(() => expect(screen.getByText(/resend in 3s/i)).toBeInTheDocument());

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText(/resend in 1s/i)).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("points Use a different email back at Step 1", async () => {
    // There is no email-change flow; users.email is the identity the session,
    // audit trail and consent record are keyed to. Correcting a typo means
    // returning to the Account step.
    mockApi();
    renderAt();
    expect(await screen.findByRole("link", { name: /use a different email/i }))
      .toHaveAttribute("href", "/start");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// The organisation form
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 2 — organisation form", () => {
  async function reachForm() {
    const calls = mockApi({ status: { ...STATUS_UNVERIFIED, verified: true } });
    renderAt();
    await formReady();
    return calls;
  }

  /** Choose a country from the searchable list.
   *
   * It is a button + listbox rather than a native <select> — 263 options in a
   * native popup opens taller than the viewport — so the test opens it and
   * clicks the option, which is also what a person does. `mousedown` is the
   * commit event the component listens for.
   */
  async function pickCountry(name) {
    fireEvent.click(screen.getByRole("combobox", { name: /primary operating country/i }));
    fireEvent.mouseDown(await screen.findByRole("option", { name: new RegExp(`^${name}$`) }));
  }

  /** Wait until the organisation form can actually be submitted.
   *
   * The heading renders before the onboarding fetch resolves, and that fetch is
   * what seeds the timezone — a required field. Until it lands, Continue stays
   * disabled and a click does nothing, so any test that fills the visible
   * fields and immediately submits races the network. It passes on a quiet
   * machine and fails under parallel load, which is the worst way for a test to
   * be wrong. The closed combobox is a <button> announcing the current zone, so
   * the selection is its text content rather than a value.
   */
  async function formReady() {
    await screen.findByText(/set up your organisation/i);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: /timezone/i }).textContent.trim())
        .not.toBe(""));
  }

  it("renders every field with the reference placeholders", async () => {
    await reachForm();
    expect(screen.getByLabelText("Organisation name")).toHaveAttribute("placeholder", "Acme Corporation");
    expect(screen.getByLabelText("Your role")).toHaveAttribute("placeholder", "IT / Security Manager");
    expect(screen.getByLabelText("Active workers (approx)")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /primary operating country/i }))
      .toBeInTheDocument();
    expect(screen.getByLabelText("Workspace name")).toHaveAttribute("placeholder", "acme-workspace");
  });

  it("renders the worker bands the server served", async () => {
    await reachForm();
    const select = screen.getByLabelText("Active workers (approx)");
    // Read off the DOM rather than by accessible name: the labels carry en
    // dashes, and what matters is that they came from the response.
    await waitFor(() => {
      const options = [...select.querySelectorAll("option")].map((o) => ({
        value: o.value, label: o.textContent,
      }));
      expect(options.slice(1)).toEqual(SETUP.worker_count_bands.map((b) => ({
        value: b.value, label: b.label,
      })));
    });
  });

  it("prefills country and timezone from the organisation, but not the provisional name", async () => {
    await reachForm();
    // The prefill arrives from GET /onboarding/setup, a second round-trip.
    await waitFor(() =>
      // The trigger announces the selected country by name; the form still
      // stores and submits the ISO code, which the POST assertion covers.
      expect(screen.getByRole("combobox", { name: /primary operating country/i }))
        .toHaveTextContent("United Kingdom"));
    // "Ada's workspace" is a placeholder the server invented because Step 1
    // collects no company details — offering it back as their answer would be
    // putting words in the admin's mouth.
    expect(screen.getByLabelText("Organisation name")).toHaveValue("");
  });

  it("shows the workspace domain suffix as a label", async () => {
    await reachForm();
    expect(screen.getByText(/\.temveris\.example/)).toBeInTheDocument();
    // A label, not a link — the platform does not route by host.
    expect(screen.queryByRole("link", { name: /temveris\.example/i })).not.toBeInTheDocument();
  });

  it("derives the workspace name from the organisation name until edited", async () => {
    await reachForm();
    fireEvent.change(screen.getByLabelText("Organisation name"),
                     { target: { value: "Acme Corporation" } });
    expect(screen.getByLabelText("Workspace name")).toHaveValue("acme-corporation");

    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "acme-eu" } });
    fireEvent.change(screen.getByLabelText("Organisation name"),
                     { target: { value: "Acme Corporation Ltd" } });
    // Their choice is not overwritten as they keep typing the org name.
    expect(screen.getByLabelText("Workspace name")).toHaveValue("acme-eu");
  });

  it("blocks submission until the required fields are valid", async () => {
    const calls = await reachForm();

    // Disabled rather than clickable-then-rejected, matching Step 1.
    expect(saveBtn()).toBeDisabled();
    fireEvent.click(saveBtn());
    expect(calls.save).toHaveLength(0);

    // The reason is still explained, on blur, rather than left to guesswork.
    fireEvent.blur(screen.getByLabelText("Organisation name"));
    fireEvent.blur(screen.getByLabelText("Active workers (approx)"));
    expect(await screen.findByText(/enter your organisation's name/i)).toBeInTheDocument();
    expect(screen.getByText(/choose an approximate team size/i)).toBeInTheDocument();
  });

  it("enables Continue once every required field is filled", async () => {
    await reachForm();
    expect(saveBtn()).toBeDisabled();
    await fillValid();
    await waitFor(() => expect(saveBtn()).toBeEnabled());
  });

  it("offers each country once, under its current ISO code", async () => {
    // Regression: the list is derived by resolving every two-letter code
    // through Intl, and Intl still knows the withdrawn ones — DD resolves to
    // "Germany", UK to "United Kingdom", SU to "Russia". Fifteen names
    // appeared twice, and picking the wrong one stored a code retired decades
    // ago that the backend's ^[A-Z]{2}$ happily accepts.
    await reachForm();
    fireEvent.click(screen.getByRole("combobox", { name: /primary operating country/i }));

    // Scoped to this listbox: the worker-band field is a native <select>, and
    // its <option>s carry the same implicit role.
    const listbox = await screen.findByRole("listbox");
    // The label span, not textContent: the selected row appends an
    // aria-hidden checkmark, which would make its name unique by accident.
    const names = within(listbox)
      .getAllByRole("option")
      .map((o) => o.querySelector("span").textContent.trim());
    const seen = new Set();
    const repeated = names.filter((n) => (seen.has(n) ? true : (seen.add(n), false)));
    expect(repeated, `repeated countries: ${[...new Set(repeated)].join(", ")}`).toEqual([]);
    expect(names).toContain("Germany");
    expect(names).toContain("United Kingdom");
  });

  it("filters the country list by name or code", async () => {
    await reachForm();
    fireEvent.click(screen.getByRole("combobox", { name: /primary operating country/i }));
    // Once open the trigger is replaced by the search input. Its *placeholder*
    // becomes the current selection, so it is addressed by its label.
    const search = screen.getByRole("combobox", { name: /search countries/i });

    fireEvent.change(search, { target: { value: "United King" } });
    const box = await screen.findByRole("listbox");
    await waitFor(() =>
      expect(within(box).getByRole("option", { name: /^United Kingdom$/ })).toBeInTheDocument());

    // The stored value is the code, so an admin may reach for that first.
    fireEvent.change(search, { target: { value: "DE" } });
    await waitFor(() =>
      expect(within(box).getByRole("option", { name: /^Germany$/ })).toBeInTheDocument());
  });

  it("rejects a malformed workspace name client-side too", async () => {
    await reachForm();
    const slug = screen.getByLabelText("Workspace name");
    fireEvent.change(slug, { target: { value: "Not A Slug" } });
    fireEvent.blur(slug);
    expect(await screen.findByText(/lowercase letters, numbers and single hyphens/i))
      .toBeInTheDocument();
  });

  // Declared with `function` so the hoisting works for the tests above.
  async function fillValid() {
    fireEvent.change(screen.getByLabelText("Organisation name"),
                     { target: { value: "Acme Corporation" } });
    fireEvent.change(screen.getByLabelText("Your role"),
                     { target: { value: "IT / Security Manager" } });
    fireEvent.change(screen.getByLabelText("Active workers (approx)"),
                     { target: { value: "11-50" } });
    await pickCountry("United States");
  }

  it("posts the form to the organisation endpoint", async () => {
    const calls = await reachForm();
    await fillValid();
    fireEvent.click(saveBtn());

    await waitFor(() => expect(calls.save).toHaveLength(1));
    expect(calls.save[0]).toMatchObject({
      organization_name: "Acme Corporation",
      role: "IT / Security Manager",
      worker_band: "11-50",
      country: "US",
      timezone: "Europe/London",
      workspace_slug: "acme-corporation",
    });
    // No organisation id is ever sent — the server derives the tenant.
    expect(calls.save[0]).not.toHaveProperty("organization_id");
  });

  it("enters the existing Step 3 route on success", async () => {
    await reachForm();
    await fillValid();
    fireEvent.click(saveBtn());
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/workspace/preparing"));
  });

  it("shows a loading state and cannot be submitted twice", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    const calls = mockApi({ status: { ...STATUS_UNVERIFIED, verified: true } });
    mockAuthFetch.mockImplementation(async (url, init) => {
      const u = String(url);
      if (u.includes("/email-verification/status")) {
        return ok({ ...STATUS_UNVERIFIED, verified: true });
      }
      if (u.includes("/onboarding/setup")) return ok(SETUP);
      if (u.includes("/onboarding/organization")) {
        calls.save.push(JSON.parse(init.body));
        await gate;
        return ok({ ...SETUP, current_step: "teams" });
      }
      throw new Error(`unexpected ${u}`);
    });

    renderAt();
    await formReady();
    await fillValid();
    fireEvent.click(saveBtn());

    await waitFor(() => expect(saveBtn()).toHaveTextContent(/saving/i));
    expect(saveBtn()).toBeDisabled();
    fireEvent.click(saveBtn());
    fireEvent.click(saveBtn());

    await act(async () => { release(); await gate; });
    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
    expect(calls.save).toHaveLength(1);
  });

  it("keeps entered values after a recoverable failure", async () => {
    const calls = mockApi({
      status: { ...STATUS_UNVERIFIED, verified: true },
      save: (n) => (n === 1
        ? fail({ detail: "Slug must be lowercase letters, numbers, and single hyphens" }, 400)
        : ok({ ...SETUP, current_step: "teams" })),
    });
    renderAt();
    await formReady();
    await fillValid();
    fireEvent.click(saveBtn());

    expect(await screen.findByText(/slug must be lowercase/i)).toBeInTheDocument();
    // Nothing was cleared — retyping the whole form after a server hiccup is
    // the fastest way to lose someone at the last step.
    expect(screen.getByLabelText("Organisation name")).toHaveValue("Acme Corporation");
    expect(screen.getByLabelText("Your role")).toHaveValue("IT / Security Manager");

    fireEvent.click(saveBtn());
    await waitFor(() => expect(calls.save).toHaveLength(2));
    expect(mockNavigate).toHaveBeenCalledWith("/workspace/preparing");
  });

  it("explains an authorization failure without exposing internals", async () => {
    mockApi({
      status: { ...STATUS_UNVERIFIED, verified: true },
      save: fail({ detail: "Only an organization admin can run setup" }, 403),
    });
    renderAt();
    await formReady();
    await fillValid();
    fireEvent.click(saveBtn());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/don't have permission/i);
    expect(alert.textContent).not.toMatch(/traceback|sqlalchemy|psycopg/i);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("marks a taken workspace name against the field", async () => {
    mockApi({
      status: { ...STATUS_UNVERIFIED, verified: true },
      save: fail({ detail: "Slug is already in use" }, 409),
    });
    renderAt();
    await formReady();
    await fillValid();
    fireEvent.click(saveBtn());

    expect(await screen.findByText(/this name is taken/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace name")).toHaveAttribute("aria-invalid", "true");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// The emailed link still lands here
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 2 — the emailed link", () => {
  it("consumes a token without a session", async () => {
    const plainFetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({ verified: true, user_id: 7 }),
    });
    vi.stubGlobal("fetch", plainFetch);

    renderAt("/verify-email?token=raw-token");
    expect(await screen.findByRole("heading", { name: /you're verified/i })).toBeInTheDocument();

    // The link path uses plain fetch, NOT authFetch — someone clicking from
    // their inbox may not be signed in.
    expect(plainFetch).toHaveBeenCalledTimes(1);
    expect(String(plainFetch.mock.calls[0][0])).toContain("/api/v1/email-verification/verify");
    expect(mockAuthFetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("reports a rejected token", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 400,
      json: async () => ({ error: { code: "TOKEN_EXPIRED", message: "This link has expired." } }),
    }));

    renderAt("/verify-email?token=stale");
    expect(await screen.findByRole("heading", { name: /couldn't verify that link/i })).toBeInTheDocument();
    expect(screen.getByText(/this link has expired/i)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
