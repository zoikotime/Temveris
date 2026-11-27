/**
 * Compensatory Leave worked dates link to Time Playback for that employee/day.
 *
 * The reviewer clicks a claimed date and Time Playback opens (new tab) with the
 * employee and date pre-selected and loaded, so the work can be verified before
 * the comp-off is approved.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

import WorkedDateChips, { playbackLink, readPlaybackDeepLink }
  from "../components/leave/WorkedDateChips";
import TimelapseTab from "../components/admin/TimelapseTab";

afterEach(() => {
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
});

describe("playback deep link", () => {
  it("round-trips employee and date", () => {
    const url = playbackLink(42, "2026-09-05");
    expect(url).toBe("/admin?tab=timelapse&user=42&date=2026-09-05");
    expect(readPlaybackDeepLink(url.slice(url.indexOf("?")))).toEqual({ user: "42", date: "2026-09-05" });
  });

  it.each([
    ["?tab=overview&user=42&date=2026-09-05"],
    ["?tab=timelapse&user=abc&date=2026-09-05"],
    ["?tab=timelapse&user=42&date=05-09-2026"],
    ["?tab=timelapse&date=2026-09-05"],
  ])("ignores %s", (search) => {
    expect(readPlaybackDeepLink(search)).toBeNull();
  });
});

describe("WorkedDateChips", () => {
  it("links each worked date to Time Playback in a new tab", () => {
    render(<WorkedDateChips dates={["2026-09-05", "2026-09-12"]} userId={42} />);
    const link = screen.getByRole("link", { name: /2026-09-05/ });
    expect(link.getAttribute("href")).toBe("/admin?tab=timelapse&user=42&date=2026-09-05");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(screen.getByText(/Dates worked \(claimed\) \(2\)/)).toBeTruthy();
  });

  it("shows a malformed date as plain text, not a broken link", () => {
    render(<WorkedDateChips dates={["5th Sept"]} userId={42} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("5th Sept")).toBeTruthy();
  });

  it("renders nothing when there are no worked dates", () => {
    const { container } = render(<WorkedDateChips dates={[]} userId={42} />);
    expect(container.innerHTML).toBe("");
  });
});

describe("worked-date evidence in the review modal", () => {
  const EV = [
    { date: "2026-09-05", valid: true, worked_seconds: 27000, adjustment_seconds: 0, sessions: 2,
      first_clock_in: "09:00", last_clock_out: "17:30", still_clocked_in: false, screenshots: 42 },
    { date: "2026-09-06", valid: true, worked_seconds: 0, adjustment_seconds: 0, sessions: 0,
      first_clock_in: null, last_clock_out: null, still_clocked_in: false, screenshots: 0 },
    { date: "2026-09-07", valid: true, worked_seconds: 7200, adjustment_seconds: 7200, sessions: 0,
      first_clock_in: null, last_clock_out: null, still_clocked_in: false, screenshots: 0 },
  ];
  const DATES = EV.map(e => e.date);

  it("shows hours, clock times and screenshots for a worked day", () => {
    render(<WorkedDateChips dates={DATES} userId={42} evidence={EV} />);
    expect(screen.getByText("✅ Worked 7h 30m · 09:00–17:30 · 42 screenshots")).toBeTruthy();
  });

  it("flags a claimed date with nothing recorded", () => {
    render(<WorkedDateChips dates={DATES} userId={42} evidence={EV} />);
    expect(screen.getByText(/No work recorded on this date/)).toBeTruthy();
  });

  it("says when the time comes from a manual adjustment", () => {
    render(<WorkedDateChips dates={DATES} userId={42} evidence={EV} />);
    expect(screen.getByText(/Worked 2h 00m · 0 screenshots · incl\. 2h 00m manual adjustment/)).toBeTruthy();
  });

  it("keeps each date's Time Playback link next to its result", () => {
    render(<WorkedDateChips dates={DATES} userId={42} evidence={EV} />);
    expect(screen.getAllByTestId("worked-date-row")).toHaveLength(3);
    expect(screen.getAllByRole("link")).toHaveLength(3);
  });

  it("shows a checking state while loading", () => {
    render(<WorkedDateChips dates={DATES} userId={42} loading />);
    expect(screen.getAllByText("Checking records…")).toHaveLength(3);
  });

  it("marks a session that is still open", () => {
    const ev = [{ ...EV[0], still_clocked_in: true }];
    render(<WorkedDateChips dates={[EV[0].date]} userId={42} evidence={ev} />);
    expect(screen.getByText(/09:00–still clocked in/)).toBeTruthy();
  });

  it("falls back to plain links when evidence is unavailable", () => {
    render(<WorkedDateChips dates={DATES} userId={42} />);
    expect(screen.queryByTestId("worked-date-row")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(3);
  });
});

function renderPlayback(props = {}) {
  const noop = () => {};
  return render(
    <TimelapseTab
      C={{ border: "#ddd", surface: "#fff", text: "#111", textSub: "#666", brand: "#1a73e8" }}
      T={{}} isDark={false} Icon={() => null} card={() => ({})} employeesAlpha={[{ id: 42, name: "Mukul" }]}
      token="t" tlDate="2026-09-05" setTlDate={noop} tlEmpId="42" setTlEmpId={noop}
      tlShots={[]} setTlShots={noop} tlLoading={false} setTlLoading={noop}
      tlLightbox={null} setTlLightbox={noop} tlDelShot={null} setTlDelShot={noop}
      tlDelVideo={false} setTlDelVideo={noop} tlVideoUrl={null} setTlVideoUrl={noop}
      tlVideoMeta={null} setTlVideoMeta={noop} tlVideoLoading={false} setTlVideoLoading={noop}
      tlVideoError="" setTlVideoError={noop} {...props}
    />
  );
}

describe("Time Playback opened from a worked-date link", () => {
  it("loads that employee's day without pressing Load", () => {
    window.history.replaceState({}, "", "/admin?tab=timelapse&user=42&date=2026-09-05");
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] });
    renderPlayback();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/screenshots?");
    expect(url).toContain("date=2026-09-05");
    expect(url).toContain("user_id=42");
  });

  it("does not auto-load when opened normally", () => {
    window.history.replaceState({}, "", "/admin?tab=timelapse");
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] });
    renderPlayback({ tlEmpId: "" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
