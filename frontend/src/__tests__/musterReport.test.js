import { describe, it, expect } from "vitest";
import { buildMusterModel, classifyAbsence, renderWorkbook } from "../utils/musterReport";

// Frozen reference date so "report generated" / period-end are deterministic.
const TODAY = new Date(2026, 5, 23); // 23 Jun 2026 (month index 5 = June)

const sampleData = {
  month: "2026-06",
  first_date: "2026-06-01",
  last_date: "2026-06-30",
  days_in_month: 30,
  rows: [
    // Perfect (0 absences)
    { id: 1, name: "Aditya",  department: "Technology", designation: "Snr Web Designer",
      working_days: 17, present_days: 17, absent_days: 0, leave_days: 0 },
    // Acceptable (1 absence)
    { id: 2, name: "Abir",    department: "Marketing",  designation: "Content Writer",
      working_days: 17, present_days: 16, absent_days: 1, leave_days: 0 },
    // High Absence (3 absences) + half-day leave
    { id: 3, name: "Naresh",  department: "Technology", designation: "AI Associate",
      working_days: 17, present_days: 13, absent_days: 3, leave_days: 1 },
    // Critical (6 absences)
    { id: 4, name: "Mukul",   department: null,         designation: null,
      working_days: 17, present_days: 11, absent_days: 6, leave_days: 0 },
  ],
};

describe("classifyAbsence", () => {
  it("maps absence counts to the right status + band", () => {
    expect(classifyAbsence(0)).toMatchObject({ status: "Perfect", band: "green" });
    expect(classifyAbsence(1)).toMatchObject({ status: "Acceptable", band: "yellow" });
    expect(classifyAbsence(2)).toMatchObject({ status: "Acceptable", band: "yellow" });
    expect(classifyAbsence(3)).toMatchObject({ status: "High Absence", band: "lightred" });
    expect(classifyAbsence(4)).toMatchObject({ status: "High Absence", band: "lightred" });
    expect(classifyAbsence(5)).toMatchObject({ status: "Critical", band: "deepred" });
    expect(classifyAbsence(99)).toMatchObject({ status: "Critical", band: "deepred" });
  });

  it("flags only 3+ absences", () => {
    expect(classifyAbsence(2).flag).toBe("");
    expect(classifyAbsence(3).flag).toBe("🟠 High");
    expect(classifyAbsence(5).flag).toBe("🔴 Critical");
  });
});

describe("buildMusterModel", () => {
  const model = buildMusterModel(sampleData, TODAY);

  it("builds the title and subtitle from the month + today", () => {
    expect(model.title).toBe("ATTENDANCE MUSTER REPORT — JUNE 2026");
    expect(model.subtitle).toContain("Period: 01 Jun 2026 – 23 Jun 2026");
    expect(model.subtitle).toContain("Report generated: 23 Jun 2026");
    expect(model.subtitle).toContain("Total employees: 4");
  });

  it("sorts rows by name and assigns serial numbers", () => {
    expect(model.rows.map((r) => r.name)).toEqual(["Abir", "Aditya", "Mukul", "Naresh"]);
    expect(model.rows.map((r) => r.serial)).toEqual([1, 2, 3, 4]);
  });

  it("computes attendance % and status per employee", () => {
    const naresh = model.rows.find((r) => r.name === "Naresh");
    expect(naresh.pct).toBe(Math.round((13 / 17) * 100)); // 76
    expect(naresh.status).toBe("High Absence");
    expect(naresh.leave).toBe(1);

    const aditya = model.rows.find((r) => r.name === "Aditya");
    expect(aditya.pct).toBe(100);
    expect(aditya.status).toBe("Perfect");
  });

  it("falls back to em-dash for missing department / designation", () => {
    const mukul = model.rows.find((r) => r.name === "Mukul");
    expect(mukul.department).toBe("—");
    expect(mukul.designation).toBe("—");
  });

  it("computes totals across all employees", () => {
    expect(model.totals.working).toBe(68);
    expect(model.totals.present).toBe(57);
    expect(model.totals.absent).toBe(10);
    expect(model.totals.leave).toBe(1);
    expect(model.totals.pct).toBe(Math.round((57 / 68) * 100)); // 84
  });

  it("rolls up per-department summary", () => {
    const tech = model.deptSummary.find((d) => d.department === "Technology");
    expect(tech.headcount).toBe(2); // Aditya + Naresh
    expect(tech.working).toBe(34);
    expect(tech.present).toBe(30);
    expect(tech.avgPct).toBe(Math.round((30 / 34) * 100));
  });

  it("lists only 3+ absence employees in attention, worst first", () => {
    expect(model.attention.map((r) => r.name)).toEqual(["Mukul", "Naresh"]);
    expect(model.attention[0].flag).toBe("🔴 Critical");
    expect(model.attention[1].flag).toBe("🟠 High");
    expect(model.attention.map((r) => r.attnSerial)).toEqual([1, 2]);
  });

  it("uses last_date as period end for a completed past month", () => {
    const past = buildMusterModel(
      { ...sampleData, month: "2026-05", first_date: "2026-05-01", last_date: "2026-05-31" },
      TODAY,
    );
    expect(past.subtitle).toContain("Period: 01 May 2026 – 31 May 2026");
  });

  it("guards divide-by-zero when an employee has no working days", () => {
    const model0 = buildMusterModel(
      { month: "2026-06", first_date: "2026-06-01", last_date: "2026-06-30",
        rows: [{ id: 9, name: "Newjoiner", working_days: 0, present_days: 0, absent_days: 0, leave_days: 0 }] },
      TODAY,
    );
    expect(model0.rows[0].pct).toBe(0);
    expect(model0.rows[0].status).toBe("Perfect");
  });
});

describe("renderWorkbook", () => {
  it("builds the three styled sheets with the expected structure", async () => {
    const model = buildMusterModel(sampleData, TODAY);
    const wb = await renderWorkbook(model);

    // All three sheets exist, in order.
    expect(wb.worksheets.map((w) => w.name)).toEqual([
      "Attendance Report", "Dept Summary", "Attention Required",
    ]);

    const ws = wb.getWorksheet("Attendance Report");
    // Title + subtitle bands.
    expect(ws.getCell("A1").value).toBe("ATTENDANCE MUSTER REPORT — JUNE 2026");
    expect(String(ws.getCell("A2").value)).toContain("Total employees: 4");
    // Column headers on row 4.
    expect(ws.getRow(4).getCell(1).value).toBe("#");
    expect(ws.getRow(4).getCell(2).value).toBe("Employee Name");
    expect(ws.getRow(4).getCell(10).value).toBe("Status");
    // First data row (row 5) = Abir (alphabetical), Acceptable / yellow band.
    expect(ws.getRow(5).getCell(2).value).toBe("Abir");
    expect(ws.getRow(5).getCell(10).value).toBe("Acceptable");
    expect(ws.getRow(5).getCell(2).fill.fgColor.argb).toBe("FFFFF2CC"); // yellow
    expect(ws.getRow(5).getCell(2).font.name).toBe("Arial");
    // Totals row after the 4 employees.
    const totalRow = ws.getRow(5 + model.rows.length);
    expect(totalRow.getCell(2).value).toBe("TOTAL");
    expect(totalRow.getCell(5).value).toBe(68); // total working days
    // Header frozen + auto-filtered.
    expect(ws.views[0]).toMatchObject({ state: "frozen", ySplit: 4 });
    expect(ws.autoFilter).toBeTruthy();

    // Attention sheet lists the 2 flagged employees, Critical first.
    const as = wb.getWorksheet("Attention Required");
    expect(as.getRow(4).getCell(2).value).toBe("Mukul");
    expect(as.getRow(4).getCell(8).value).toBe("🔴 Critical");

    // A real buffer is produced without error.
    const buf = await wb.xlsx.writeBuffer();
    expect(buf.byteLength).toBeGreaterThan(2000);
  // 15s, not the 5s default: rendering a real xlsx workbook takes ~1.6s alone
  // and up to ~5s under full-suite load, so it sat right on the default
  // timeout and failed intermittently. Not a logic change.
  }, 15000);

  it("renders a friendly placeholder when nobody needs attention", async () => {
    const clean = buildMusterModel(
      { month: "2026-06", first_date: "2026-06-01", last_date: "2026-06-30",
        rows: [{ id: 1, name: "Star", department: "Ops", designation: "Lead",
                 working_days: 17, present_days: 17, absent_days: 0, leave_days: 0 }] },
      TODAY,
    );
    const wb = await renderWorkbook(clean);
    const as = wb.getWorksheet("Attention Required");
    expect(String(as.getRow(4).getCell(1).value)).toContain("No employees require attention");
  });
});
