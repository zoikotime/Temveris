/**
 * "text" settings render as a text input (notification recipients).
 *
 * notifications.alert_recipients / notifications.leave_recipients are declared
 * type "text". The renderer had no text branch, so they fell through to the
 * number input and an email address could not be entered. These tests pin:
 *
 *  - a text row renders <input type="text"> and accepts comma-separated emails
 *  - the typed value is sent verbatim in the PUT /settings/scoped payload
 *  - int rows still render as number inputs (no change to the fallback)
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import OrganizationSettingsTab from "../components/OrganizationSettingsTab";

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({
    C: {
      brand: "#1a73e8", brandBg: "#e8f0fe", border: "#ddd", borderLight: "#eee",
      surface: "#fff", text: "#111", textSub: "#666", textLight: "#999", bg: "#fafafa",
    },
  }),
}));

const rows = () => [
  {
    key: "notifications.alert_recipients", group: "Notifications", label: "Alert recipients",
    type: "text", description: "", default: "", parent_value: "", value: null, is_override: false,
  },
  {
    key: "notifications.leave_recipients", group: "Notifications", label: "Leave recipients",
    type: "text", description: "", default: "", parent_value: "", value: null, is_override: false,
  },
  {
    key: "schedule.grace_minutes", group: "Notifications", label: "Grace period",
    type: "int", unit: "minutes", min: 0, max: 240,
    description: "", default: "15", parent_value: "15", value: null, is_override: false,
  },
];

function mockFetch() {
  const calls = [];
  global.fetch = vi.fn((url, opts = {}) => {
    calls.push({ url: String(url), method: opts.method || "GET", body: opts.body });
    const u = String(url);
    const json = (d) => Promise.resolve({ ok: true, json: () => Promise.resolve(d) });
    if (u.includes("/settings/scoped")) {
      return json({ scope: "org", group_order: ["Notifications"], keys: rows() });
    }
    if (u.includes("/schedule/shifts")) return json([]);
    if (u.includes("/employees")) return json([]);
    if (u.includes("/org/teams")) return json([]);
    return json({});
  });
  return calls;
}

beforeEach(() => vi.clearAllMocks());

describe("text settings", () => {
  it("render recipient settings as text inputs", async () => {
    mockFetch();
    const { container } = render(<OrganizationSettingsTab isAdmin showToast={() => {}} />);
    await screen.findByText("Leave recipients");
    const textInputs = container.querySelectorAll('input[type="text"]');
    // Search box + the two recipient fields.
    expect(textInputs.length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll('input[type="number"]').length).toBe(1);
  });

  it("accepts comma-separated emails and saves them verbatim", async () => {
    const calls = mockFetch();
    const { container } = render(<OrganizationSettingsTab isAdmin showToast={() => {}} />);
    await screen.findByText("Leave recipients");
    const recipientInputs = [...container.querySelectorAll('input[type="text"]')]
      .filter(i => i.placeholder?.includes("@"));
    expect(recipientInputs.length).toBe(2);

    fireEvent.change(recipientInputs[1], { target: { value: "hr@example.com, people@example.com" } });
    expect(recipientInputs[1].value).toBe("hr@example.com, people@example.com");

    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => {
      const put = calls.find(c => c.method === "PUT" && c.url.includes("/settings/scoped"));
      expect(put).toBeTruthy();
      expect(JSON.parse(put.body).settings["notifications.leave_recipients"])
        .toBe("hr@example.com, people@example.com");
    });
  });
});
