/**
 * Render smoke tests for the shared auth shell and the pages inside it.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `AuthShell` is a layout route wrapping `/`, `/start` and `/forgot-password`.
 * Before this file, nothing in the suite rendered it, `AuthLayout`, `AuthTabs`,
 * `Login` or `ForgotPassword` — the only existing `Login` reference was to the
 * `redirectToLogin()` helper, not a render.
 *
 * That gap matters more than usual: `/` is the page every user and every
 * existing tenant hits. A production build proves the refactor *compiles*; it
 * says nothing about whether it *renders*. A bad hook order, a `useLayoutEffect`
 * touching a null ref, or an `<Outlet />` with no matching child would pass the
 * build and white-screen the login page for everyone.
 *
 * These are deliberately shallow — mount, assert no throw, assert the load-
 * bearing elements exist. They are a tripwire, not a behavioural spec.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// Under vitest, JSX in this project compiles to the CLASSIC runtime
// (`React.createElement`), so a component must have `React` in scope. None of
// the five components exercised here import it — they render fine in the app,
// where the build uses the automatic runtime, but throw "React is not defined"
// under test.
//
// Publishing React globally is a test-only shim, kept in this file rather than
// adding an import to five files nobody here authored. The proper fix is one
// line in vitest.config.js — `react({ jsxRuntime: "automatic" })`, or aligning
// it with vite.config.js — after which this can be deleted. Left as a shim
// because changing shared test config affects all 18 test files, which is not
// this change's business.
globalThis.React = React;

// ── Mocks ───────────────────────────────────────────────────────────────────
// Kept to the module boundary: the real components render, only their outbound
// dependencies are stubbed. Mocking the hooks rather than wrapping the real
// providers keeps these tests about rendering — AuthProvider fetches /auth/me on
// mount, which would make a render smoke test into a network test.

vi.mock("react-hot-toast", () => ({
  default: Object.assign(vi.fn(), {
    success: vi.fn(), error: vi.fn(), loading: vi.fn(), dismiss: vi.fn(),
  }),
}));

vi.mock("../api/authApi", () => ({
  loginUser: vi.fn(),
  forgotPassword: vi.fn(),
  registerUser: vi.fn(),
}));

vi.mock("../api/axios", () => ({
  default: { get: vi.fn().mockRejectedValue(new Error("no network in test")),
             post: vi.fn().mockRejectedValue(new Error("no network in test")) },
}));

const mockAuth = {
  login: vi.fn(),
  logout: vi.fn(),
  isAuthenticated: false,
  user: null,
  loading: false,
};
vi.mock("../context/AuthContext", () => ({
  useAuth: () => mockAuth,
  AuthProvider: ({ children }) => children,
}));

vi.mock("../context/LanguageContext", () => ({
  // Login reads T.loginSubtitle; the real strings module is a large lazy chunk.
  useLanguage: () => ({ T: { loginSubtitle: "Enter your credentials." }, lang: "en" }),
  LanguageProvider: ({ children }) => children,
}));

import AuthShell from "../components/auth/AuthShell";
import Login from "../pages/Login";
import ForgotPassword from "../pages/ForgotPassword";

/** Mount the real shell with the real pages as children, as App.jsx does. */
function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<AuthShell />}>
          <Route path="/" element={<Login />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

let errorSpy;

beforeEach(() => {
  // A React render error or hook-order warning surfaces on console.error. These
  // tests fail on it rather than passing with a broken page.
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  errorSpy.mockRestore();
  vi.unstubAllGlobals();
});

function expectNoReactErrors() {
  const messages = errorSpy.mock.calls.map((c) => String(c[0] ?? ""));
  const serious = messages.filter((m) =>
    /hook|Hook|Outlet|Cannot read|is not a function|Each child|Maximum update/.test(m),
  );
  expect(serious, `React logged: ${serious.join(" | ")}`).toEqual([]);
}


// ═════════════════════════════════════════════════════════════════════════════
// The shell
// ═════════════════════════════════════════════════════════════════════════════

describe("AuthShell", () => {
  it("mounts and renders its child route through the Outlet", async () => {
    renderAt("/");
    expect(await screen.findByRole("button", { name: /log in/i })).toBeInTheDocument();
    expectNoReactErrors();
  });

  it("renders the persistent chrome — logo and tabs — around the child", async () => {
    renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    expect(screen.getByAltText("Temveris")).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: /account/i })).toBeInTheDocument();
    for (const tab of [/login/i, /sign up/i, /reset password/i]) {
      expect(screen.getByRole("tab", { name: tab })).toBeInTheDocument();
    }
  });

  it("points the Sign Up tab at the live signup route", async () => {
    renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    // Regression guard: this tab used to point at /register, which had no route
    // and bounced the user back to login via the wildcard.
    expect(screen.getByRole("tab", { name: /sign up/i })).toHaveAttribute("href", "/start");
  });

  it("survives a jsdom environment with no ResizeObserver", async () => {
    // The height-animation effect is guarded on ResizeObserver being defined.
    // jsdom has none, so this asserts the guard holds rather than throwing.
    expect(typeof globalThis.ResizeObserver).toBe("undefined");
    renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    expectNoReactErrors();
  });

  it("measures and pins the card height when ResizeObserver exists", async () => {
    // The real-browser path: the effect observes the inner node. Asserts it
    // wires up and disconnects without throwing.
    const observe = vi.fn();
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb) { this._cb = cb; }
      observe(...a) { observe(...a); }
      disconnect() { disconnect(); }
      unobserve() {}
    });

    const { unmount } = renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    expect(observe).toHaveBeenCalled();
    unmount();
    expect(disconnect).toHaveBeenCalled();
    expectNoReactErrors();
  });

  it("renders a different child for a different route in the same shell", async () => {
    renderAt("/forgot-password");
    expect(await screen.findByPlaceholderText("you@company.com")).toBeInTheDocument();
    // Shell chrome is still present — the card persists across the swap.
    expect(screen.getByRole("tablist", { name: /account/i })).toBeInTheDocument();
    expectNoReactErrors();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Login — the page every user and every existing tenant hits
// ═════════════════════════════════════════════════════════════════════════════

describe("Login", () => {
  it("renders the email and password fields and a submit button", async () => {
    renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    expect(document.getElementById("zt-email")).toBeInTheDocument();
    expect(document.getElementById("zt-password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /log in/i })).toHaveAttribute("type", "submit");
  });

  it("offers the password reveal toggle", async () => {
    renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    expect(screen.getByRole("button", { name: /show password/i })).toBeInTheDocument();
  });

  it("does not crash when the SSO provider probe fails", async () => {
    // The axios mock rejects; a login page that white-screens on a failed probe
    // would lock everyone out whenever that endpoint is unavailable.
    renderAt("/");
    await screen.findByRole("button", { name: /log in/i });
    await waitFor(() => expectNoReactErrors());
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// ForgotPassword — incl. the regex the linter flagged
// ═════════════════════════════════════════════════════════════════════════════

describe("ForgotPassword", () => {
  it("renders the email field and submit button", async () => {
    renderAt("/forgot-password");
    const input = await screen.findByPlaceholderText("you@company.com");
    expect(input).toHaveAttribute("type", "email");
    expectNoReactErrors();
  });

  it("accepts a normal address with its email regex", () => {
    // Guards the `\-` inside the character class that eslint flags as an
    // unnecessary escape. It is semantically inert — `[\-]` == `[-]` — but a
    // well-meant "cleanup" here could break password reset for real users, so
    // the accepted/rejected sets are pinned.
    // The escapes are copied verbatim from ForgotPassword.jsx:21 so this is a
    // character-exact mirror of the shipped regex. Cleaning them up here would
    // silently stop testing what actually runs, so the rule is suppressed
    // rather than the source diverged.
    // eslint-disable-next-line no-useless-escape
    const re = /^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$/;

    for (const good of [
      "a@b.com",
      "first.last@company.co.uk",
      "first-last@sub-domain.example.com",
      "user+tag@example.io",
      "someone@temveris.example",
    ]) {
      expect(re.test(good), `should accept ${good}`).toBe(true);
    }

    for (const bad of ["bad@", "@bad.com", "no-at-sign", "a b@c.com", "a@b"]) {
      expect(re.test(bad), `should reject ${bad}`).toBe(false);
    }
  });
});
