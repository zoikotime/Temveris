/**
 * Logout-pipeline regression tests.
 *
 * The white-screen logout glitch came from TWO navigations racing (a renderer
 * hard-nav + the Electron main process's loadURL). The fix funnels every
 * renderer logout path through ONE decision point — redirectToLogin() — gated by
 * an idempotency latch (beginLogout). These tests lock in the invariants so a
 * future edit can't quietly reintroduce a second navigation:
 *
 *   • Web   → clears auth storage + navigates to "/" exactly once.
 *   • Desktop → hands off to the Electron main process (electronAuth.logout)
 *     exactly once, and does NOT hard-navigate or clear storage (main owns both,
 *     and clearing storage would trip main's watcher into a second logout).
 *   • A duplicate/concurrent logout is a no-op (guard) and warns in dev.
 *   • login (writeToken) resets the guard, so a desktop re-login-then-logout
 *     works — desktop never reloads the JS context, so the latch must be cleared.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  redirectToLogin,
  beginLogout,
  writeToken,
  readToken,
} from "../api/authStorage";

let assignSpy;
const realLocation = window.location;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.__ztLoggingOut = false;
  delete window.isDesktopApp;
  delete window.electronAuth;
  // jsdom locks window.location.assign, so swap the whole object for a stub.
  // pathname is a non-root path so the web `pathname !== "/"` nav actually fires.
  assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { assign: assignSpy, pathname: "/electron" },
  });
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: realLocation,
  });
});

describe("redirectToLogin — web", () => {
  it("clears auth storage and navigates to / exactly once", () => {
    localStorage.setItem("zt_token", "abc");
    redirectToLogin();
    expect(readToken()).toBeNull();                 // storage cleared
    expect(assignSpy).toHaveBeenCalledTimes(1);
    expect(assignSpy).toHaveBeenCalledWith("/");
  });

  it("does not double-navigate on a duplicate call", () => {
    redirectToLogin();
    redirectToLogin();
    expect(assignSpy).toHaveBeenCalledTimes(1);      // guard held
    expect(console.warn).toHaveBeenCalledTimes(1);   // dev warning fired once
  });
});

describe("redirectToLogin — desktop", () => {
  beforeEach(() => {
    window.isDesktopApp = true;
    window.electronAuth = { logout: vi.fn() };
  });

  it("hands off to main exactly once and does NOT hard-navigate or clear storage", () => {
    localStorage.setItem("zt_token", "abc");
    redirectToLogin();
    expect(window.electronAuth.logout).toHaveBeenCalledTimes(1);
    expect(assignSpy).not.toHaveBeenCalled();        // no renderer reload
    expect(readToken()).toBe("abc");                 // main owns storage teardown
  });

  it("does not fire a second handoff on a duplicate call", () => {
    redirectToLogin();
    redirectToLogin();
    expect(window.electronAuth.logout).toHaveBeenCalledTimes(1);
  });
});

describe("beginLogout latch", () => {
  it("first caller wins, second is rejected", () => {
    expect(beginLogout()).toBe(true);
    expect(beginLogout()).toBe(false);
  });

  it("login (writeToken) resets the latch so the next logout works", () => {
    beginLogout();
    expect(beginLogout()).toBe(false);               // latched
    writeToken("fresh-token", { remember: true });   // a new session begins
    expect(window.__ztLoggingOut).toBe(false);
    expect(beginLogout()).toBe(true);                // usable again
  });
});
