/**
 * openLeaveAttachment — the authenticated "view supporting document" action.
 *
 * The regression these guard is a popup blocker, which is invisible in a unit
 * test unless you assert on ORDER. Browsers only permit window.open while a
 * user gesture is still on the stack, so the obvious implementation —
 *
 *     const blob = await fetch(...)      // gesture ends here
 *     window.open(URL.createObjectURL(blob))
 *
 * — is blocked in Chrome, Safari and Firefox. The tab must be claimed BEFORE
 * the first await and navigated afterwards.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { openLeaveAttachment } from "../utils/leaveAttachment";

const mkWin = () => ({
  location: { href: "" },
  document: { write: vi.fn(), close: vi.fn() },
  close: vi.fn(),
});

let openSpy;

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => "blob:fake-url");
  globalThis.URL.revokeObjectURL = vi.fn();
  localStorage.setItem("zt_token", "tok123");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("openLeaveAttachment", () => {
  it("opens the tab BEFORE awaiting the fetch — otherwise popup blockers kill it", async () => {
    const order = [];
    const win = mkWin();
    openSpy = vi.fn(() => { order.push("open"); return win; });
    vi.stubGlobal("open", openSpy);
    vi.stubGlobal("fetch", vi.fn(() => {
      order.push("fetch");
      return Promise.resolve({ ok: true, blob: () => Promise.resolve(new Blob(["x"])) });
    }));

    await openLeaveAttachment(42);

    expect(order).toEqual(["open", "fetch"]);
  });

  it("sends the bearer token to the authenticated attachment route", async () => {
    const fetchSpy = vi.fn(() =>
      Promise.resolve({ ok: true, blob: () => Promise.resolve(new Blob(["x"])) }));
    vi.stubGlobal("open", vi.fn(() => mkWin()));
    vi.stubGlobal("fetch", fetchSpy);

    await openLeaveAttachment(42);

    const [url, opts] = fetchSpy.mock.calls[0];
    expect(url).toMatch(/\/leave\/42\/attachment$/);
    expect(opts.headers.Authorization).toBe("Bearer tok123");
  });

  it("points the already-open tab at the blob once the bytes arrive", async () => {
    const win = mkWin();
    vi.stubGlobal("open", vi.fn(() => win));
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({ ok: true, blob: () => Promise.resolve(new Blob(["x"])) })));

    await openLeaveAttachment(42);

    expect(win.location.href).toBe("blob:fake-url");
  });

  it("tells the user when the browser blocked the tab", async () => {
    vi.stubGlobal("open", vi.fn(() => null));
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    await expect(openLeaveAttachment(42)).rejects.toThrow(/blocked the document window/i);
    // No point fetching a document we have nowhere to show.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("surfaces the server's reason and closes the placeholder tab on failure", async () => {
    const win = mkWin();
    vi.stubGlobal("open", vi.fn(() => win));
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: false, status: 403,
      json: () => Promise.resolve({ detail: "You do not have access to this document" }),
    })));

    await expect(openLeaveAttachment(42)).rejects.toThrow(/do not have access/i);
    expect(win.close).toHaveBeenCalled();
  });

  it("falls back to the status code when the error body is not JSON", async () => {
    const win = mkWin();
    vi.stubGlobal("open", vi.fn(() => win));
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
      ok: false, status: 502,
      json: () => Promise.reject(new Error("not json")),
    })));

    await expect(openLeaveAttachment(42)).rejects.toThrow(/HTTP 502/);
    expect(win.close).toHaveBeenCalled();
  });
});
