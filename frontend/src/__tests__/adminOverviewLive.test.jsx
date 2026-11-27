/**
 * Live Workforce Overview (admin Overview tab): the numbers it shows must add
 * up to the status counts AdminDashboard hands it, every card / action must
 * route where it says, and the worker table must agree with the grid cards.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, within, act } from "@testing-library/react";
import { makeC } from "../context/ThemeContext";
import {
  OverviewTitle, OverviewStatCards, OverviewSidePanel, OverviewWorkerTable,
} from "../components/admin/OverviewLive";
import {
  workforcePct, distributionRows, historyDistributionRows, rowView, LIVE_GRACE_MS,
} from "../components/admin/overviewModel";

const C = makeC(false);
const T = {};

// 65 active workers, the mix from the design mockup.
const COUNTS = {
  working: 1, on_break: 0, not_started: 0, idle: 55, on_leave: 5, absent: 4,
  late_pending: 0, in_meeting: 0, shift_ended: 0,
};
const TOTAL = 65;

const nowIso = () => new Date().toISOString();

describe("overview arithmetic", () => {
  it("rounds % of workforce and survives an empty org", () => {
    expect(workforcePct(1, 65)).toBe(2);
    expect(workforcePct(55, 65)).toBe(85);
    expect(workforcePct(5, 65)).toBe(8);
    expect(workforcePct(3, 0)).toBe(0);
  });

  it("distribution covers every worker: rare states only when present, the rest as Other", () => {
    const rows = distributionRows(COUNTS, TOTAL);
    expect(rows.map((r) => r.k)).toEqual(["working", "on_break", "not_started", "idle", "on_leave", "absent"]);
    expect(rows.reduce((s, r) => s + r.value, 0)).toBe(TOTAL);

    const withRare = distributionRows({ ...COUNTS, idle: 50, late_pending: 2 }, TOTAL);
    expect(withRare.find((r) => r.k === "late_pending").value).toBe(2);
    expect(withRare.find((r) => r.k === "other").value).toBe(3);
    expect(withRare.reduce((s, r) => s + r.value, 0)).toBe(TOTAL);
  });

  it("history distribution is present / on leave / absent plus any remainder", () => {
    const rows = historyDistributionRows({ total: 10, present: 6, on_leave: 1, absent: 2 }, 10);
    expect(rows.map((r) => [r.k, r.value])).toEqual([["present", 6], ["on_leave", 1], ["absent", 2], ["other", 1]]);
  });
});

describe("OverviewStatCards — live", () => {
  const setup = () => {
    const onOpen = vi.fn();
    render(<OverviewStatCards C={C} T={T} counts={COUNTS} total={TOTAL} requiresAttention={4} history={null} onOpen={onOpen} />);
    return onOpen;
  };

  it("shows each count with its share of the workforce", () => {
    setup();
    expect(screen.getByTestId("ov-card-working-value")).toHaveTextContent("1");
    expect(screen.getByTestId("ov-card-working-pct")).toHaveTextContent("2% of workforce");
    expect(screen.getByTestId("ov-card-idle-value")).toHaveTextContent("55");
    expect(screen.getByTestId("ov-card-idle-pct")).toHaveTextContent("85% of workforce");
    expect(screen.getByTestId("ov-card-on_leave-pct")).toHaveTextContent("8% of workforce");
    expect(screen.getByTestId("ov-card-absent-pct")).toHaveTextContent("6% of workforce");
    expect(screen.getByTestId("ov-card-attention-value")).toHaveTextContent("4");
  });

  it("each card opens the matching status filter", () => {
    const onOpen = setup();
    for (const k of ["working", "on_break", "not_started", "idle", "on_leave", "absent"]) {
      fireEvent.click(screen.getByTestId(`ov-card-${k}`));
      expect(onOpen).toHaveBeenLastCalledWith(k);
    }
    fireEvent.click(screen.getByTestId("ov-card-attention"));
    expect(onOpen).toHaveBeenLastCalledWith("requires_attention");
  });
});

describe("OverviewStatCards — a past date", () => {
  it("switches to the attendance summary and opens the historical filter", () => {
    const onOpen = vi.fn();
    render(<OverviewStatCards C={C} T={T} counts={COUNTS} total={TOTAL} requiresAttention={4}
      history={{ total: 20, present: 15, absent: 3, clocked_out: 15, on_leave: 2 }} onOpen={onOpen} />);
    expect(screen.queryByTestId("ov-card-working")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ov-card-attention")).not.toBeInTheDocument();
    expect(screen.getByTestId("ov-card-present-value")).toHaveTextContent("15");
    expect(screen.getByTestId("ov-card-present-pct")).toHaveTextContent("75% of workforce");
    fireEvent.click(screen.getByTestId("ov-card-absent"));
    expect(onOpen).toHaveBeenLastCalledWith("absent", true);
  });
});

describe("OverviewSidePanel", () => {
  it("summarises today from the same counts", () => {
    render(<OverviewSidePanel C={C} T={T} counts={COUNTS} total={TOTAL} history={null} dateLabel="Today" onAddWorker={() => {}} onNavigate={() => {}} />);
    expect(screen.getByTestId("ov-summary-total")).toHaveTextContent("65");
    expect(screen.getByTestId("ov-summary-present")).toHaveTextContent("1");
    expect(screen.getByTestId("ov-summary-on_leave")).toHaveTextContent("5");
    expect(screen.getByTestId("ov-summary-absent")).toHaveTextContent("4");
    expect(screen.getByTestId("ov-summary-idle")).toHaveTextContent("55");
    expect(screen.getByTestId("ov-dist-idle")).toHaveTextContent("55 (85%)");
    expect(screen.getByText("Present (Working + On Break)")).toBeInTheDocument();
  });

  it("counts a worker in a meeting as present and says so", () => {
    render(<OverviewSidePanel C={C} T={T} counts={{ ...COUNTS, in_meeting: 2, idle: 53 }} total={TOTAL} history={null} dateLabel="Today" onAddWorker={() => {}} onNavigate={() => {}} />);
    expect(screen.getByTestId("ov-summary-present")).toHaveTextContent("3");
    expect(screen.getByText("Present (Working, Break, Meeting)")).toBeInTheDocument();
  });

  it("quick actions open the invite modal and the existing pages", () => {
    const onAddWorker = vi.fn();
    const onNavigate = vi.fn();
    render(<OverviewSidePanel C={C} T={T} counts={COUNTS} total={TOTAL} history={null} dateLabel="Today" onAddWorker={onAddWorker} onNavigate={onNavigate} />);
    const qa = within(screen.getByTestId("ov-quick-actions"));
    fireEvent.click(qa.getByText("Add Worker"));
    expect(onAddWorker).toHaveBeenCalledTimes(1);
    fireEvent.click(qa.getByText("Request Leave"));
    expect(onNavigate).toHaveBeenLastCalledWith("/leave");
    fireEvent.click(qa.getByText("Run Report"));
    expect(onNavigate).toHaveBeenLastCalledWith("/admin?tab=reports");
    fireEvent.click(qa.getByText("Manage Policies"));
    expect(onNavigate).toHaveBeenLastCalledWith("/settings/policies");
  });
});

describe("OverviewTitle", () => {
  it("shows LIVE while the socket is connected", () => {
    render(<OverviewTitle C={C} T={T} wsStatus="connected" historical={false} dateLabel="Today" />);
    expect(screen.getByText("Live Workforce Overview")).toBeInTheDocument();
    expect(screen.getByTestId("ov-live-pill")).toHaveTextContent("LIVE");
  });

  describe("live badge stability", () => {
    afterEach(() => vi.useRealTimers());
    const pill = () => screen.getByTestId("ov-live-pill");

    it("ignores a short drop: it reconnects before the grace period ends", () => {
      vi.useFakeTimers();
      const { rerender } = render(<OverviewTitle C={C} T={T} wsStatus="connected" historical={false} dateLabel="Today" />);
      for (const status of ["disconnected", "connecting", "connected", "disconnected", "connecting", "connected"]) {
        rerender(<OverviewTitle C={C} T={T} wsStatus={status} historical={false} dateLabel="Today" />);
        act(() => { vi.advanceTimersByTime(LIVE_GRACE_MS / 4); });
        expect(pill()).toHaveTextContent("LIVE");
        expect(pill()).not.toHaveTextContent("RECONNECTING");
      }
      expect(pill()).toHaveAttribute("data-state", "live");
    });

    it("shows RECONNECTING only after the socket has been down for the whole grace period", () => {
      vi.useFakeTimers();
      const { rerender } = render(<OverviewTitle C={C} T={T} wsStatus="connected" historical={false} dateLabel="Today" />);
      rerender(<OverviewTitle C={C} T={T} wsStatus="disconnected" historical={false} dateLabel="Today" />);
      act(() => { vi.advanceTimersByTime(LIVE_GRACE_MS - 1); });
      expect(pill()).toHaveAttribute("data-state", "live");
      act(() => { vi.advanceTimersByTime(1); });
      expect(pill()).toHaveTextContent("RECONNECTING");
      rerender(<OverviewTitle C={C} T={T} wsStatus="connected" historical={false} dateLabel="Today" />);
      act(() => { vi.advanceTimersByTime(0); });
      expect(pill()).toHaveTextContent("LIVE");
      expect(pill()).toHaveAttribute("data-state", "live");
    });

    it("never shows CONNECTING text while the first connection is made", () => {
      vi.useFakeTimers();
      render(<OverviewTitle C={C} T={T} wsStatus="connecting" historical={false} dateLabel="Today" />);
      expect(pill()).toHaveTextContent("LIVE");
      expect(pill()).toHaveAttribute("data-state", "starting");
    });
  });

  it("drops the LIVE pill and shows the picked date for a past day", () => {
    render(<OverviewTitle C={C} T={T} wsStatus="connected" historical dateLabel="3 Nov 2026" />);
    expect(screen.queryByTestId("ov-live-pill")).not.toBeInTheDocument();
    expect(screen.getByTestId("ov-stamp")).toHaveTextContent("3 Nov 2026");
  });
});

describe("worker rows", () => {
  const working = {
    id: 1, name: "Yash Kumar", designation: "Back-End Engineer", status: "Working",
    employment_type: "part_time", clock_in_time: nowIso(),
    worked_today_seconds: 6 * 3600 + 59 * 60, active_pct: 89, expected_seconds: 8 * 3600,
  };
  const leave = { id: 2, name: "Amit Mehta", status: "On Leave", employment_type: "full_time" };
  const lowActive = { ...working, id: 3, name: "Low Active", active_pct: 40 };

  it("derives the same Active Time as the grid card and shift progress from expected hours", () => {
    const v = rowView(working);
    expect(v.cfg.label).toBe("Working");
    expect(v.worked).toBe("6h 59m");
    expect(v.activePct).toBe(89);
    expect(v.progress).toBe(87);
    expect(v.typeLabel).toBe("Part Time");
    expect(rowView(lowActive).activePct).toBe(65);   // EmpCard's floor
  });

  it("leaves time columns empty for someone who has not clocked in", () => {
    const v = rowView(leave);
    expect(v.cfg.label).toBe("On Leave");
    expect(v.clockIn).toBeNull();
    expect(v.worked).toBeNull();
    expect(v.activePct).toBeNull();
    expect(v.progress).toBeNull();
  });

  it("shows no progress without a server-resolved expected day", () => {
    expect(rowView({ ...working, expected_seconds: null }).progress).toBeNull();
  });

  it("row click views the worker; the row menu edits and deletes", () => {
    const onView = vi.fn(), onEdit = vi.fn(), onDelete = vi.fn();
    render(<OverviewWorkerTable C={C} T={T} isDark={false} emps={[working, leave]} onView={onView} onEdit={onEdit} onDelete={onDelete} />);
    const table = screen.getByTestId("ov-worker-table");
    expect(within(table).getAllByTestId("ov-row-status").map((n) => n.textContent)).toEqual(["Working", "On Leave"]);

    fireEvent.click(within(table).getByText("Yash Kumar"));
    expect(onView).toHaveBeenCalledWith(working);

    fireEvent.click(screen.getByRole("button", { name: "Actions: Amit Mehta" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit" }));
    expect(onEdit).toHaveBeenCalledWith(leave);
    expect(onView).toHaveBeenCalledTimes(1);   // the menu click did not also open the row

    fireEvent.click(screen.getByRole("button", { name: "Actions: Yash Kumar" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledWith(working);
  });
});
