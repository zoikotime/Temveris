/**
 * The admin dashboard's live-update socket must not fight itself.
 *
 * The teardown flag used to be a ref shared by every run of the socket
 * effect. A re-run (React StrictMode's dev re-mount, or a refreshed token
 * changing the fetch callbacks) reset it before the old socket had finished
 * closing, so the old socket's onclose reconnected and closed the new socket;
 * the two runs then replaced each other's sockets on a doubling timer and the
 * Overview's LIVE badge flickered LIVE / CONNECTING / RECONNECTING.
 *
 * Asserted on the source, in the idiom this page's other socket/fetch tests
 * use (adminEmployeeStaleData.test.jsx): each pins one decision that, if
 * reverted, brings the flicker back.
 */
import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

const DASH = fs.readFileSync(path.resolve(__dirname, "../pages/AdminDashboard.jsx"), "utf8");

const effect = (() => {
  const start = DASH.indexOf("function connectWebSocket()");
  expect(start, "connectWebSocket not found — was it renamed?").toBeGreaterThan(-1);
  const from = DASH.lastIndexOf("let wsBackoff = 1000;", start);
  const end = DASH.indexOf("}, [fetchEmployees", start);
  expect(from).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return DASH.slice(from, end);
})();

describe("admin dashboard live socket", () => {
  it("keeps the teardown flag per effect run", () => {
    expect(effect).toMatch(/let tearingDown = false;/);
    expect(effect).toMatch(/return \(\) => \{\s*tearingDown = true;/);
    // The shared ref may still be written, but must not gate reconnection.
    expect(effect).not.toMatch(/if \(wsTearingDownRef\.current\) return;/);
    expect(effect).not.toMatch(/if \(!wsTearingDownRef\.current\)/);
  });

  it("ignores events from a socket that is no longer the current one", () => {
    expect(effect).toMatch(/ws\.onopen = \(\) => \{\s*if \(wsRef\.current !== ws\) return;/);
    expect(effect).toMatch(/ws\.onclose = async \(event\) => \{[\s\S]{0,200}if \(tearingDown \|\| wsRef\.current !== ws\) return;/);
    expect(effect).toMatch(/ws\.onerror = \(\) => \{ if \(wsRef\.current === ws\)/);
  });

  it("releases the socket at teardown so the next run never closes it mid-handshake", () => {
    expect(effect).toMatch(/const sock = wsRef\.current;[\s\S]{0,300}wsRef\.current = null;/);
  });
});
