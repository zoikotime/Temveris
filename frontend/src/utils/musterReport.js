// Attendance Muster Report — styled .xlsx generator.
//
// Replaces the old raw per-day CSV grid (P/A/W…) that payroll teams found
// hard to read. Produces a polished, colour-coded workbook with three sheets:
//
//   1. "Attendance Report"   — one row per employee, aggregate columns,
//                              colour-banded by absence count, totals row.
//   2. "Dept Summary"        — per-department headcount + roll-ups.
//   3. "Attention Required"  — only employees with 3+ absences, flagged.
//
// The pure data shaping lives in buildMusterModel() so it can be unit-tested
// without a browser / ExcelJS. The ExcelJS styling + download lives in
// exportAttendanceMuster(). ExcelJS is dynamically imported so it stays out
// of the main bundle (same lazy pattern as ReportsSection's loadXLSX).

// ── palette (ARGB, opaque) ───────────────────────────────────────────────
const NAVY      = "FF1F3864"; // title band
const BLUE_SUB  = "FF2E5496"; // subtitle band
const HEADER    = "FF4472C4"; // column-header band
const TOTAL_BG  = "FFD9D9D9"; // totals row
const BORDER    = "FFBFBFBF";
const RED_TITLE = "FFC00000"; // "Attention Required" title band

// Row bands keyed by absence severity — fill + matching text colour.
const BANDS = {
  green:    { fill: "FFE2EFDA", font: "FF375623" }, // 0 absences
  yellow:   { fill: "FFFFF2CC", font: "FF7F6000" }, // 1–2
  lightred: { fill: "FFFCE4D6", font: "FF833C00" }, // 3–4
  deepred:  { fill: "FFF4B0B0", font: "FF990000" }, // 5+
};

const FONT = "Arial";

// Absence-severity bands — ORGANIZATION POLICY, resolved by the backend.
//
// These are not a display legend: `attentionAt` filters employees into a
// red-banded "Attention Required" worksheet HR acts on, so "how many absences
// is too many" is a workforce judgement that differs by industry. They now
// come from the settings cascade
// (`reports.absence_acceptable_max` / `_high_max` / `_attention_threshold`,
// registered in backend/app/settings/router.py and delivered by GET /settings).
//
// The values below are the SYSTEM DEFAULTS, used only until the fetch lands —
// identical to the constants they replace, so nothing moved for existing
// tenants on the day this shipped.
export const ABSENCE_BANDS = { perfect: 0, acceptable: 2, high: 4 };
export const ATTENTION_AT = 3;

let _bands = { ...ABSENCE_BANDS };
let _attentionAt = ATTENTION_AT;

/** Point the muster export at THIS organization's resolved thresholds. */
export function setAbsencePolicy(settings) {
  const n = (v, fallback) => {
    const x = Number(v);
    return Number.isFinite(x) && x >= 0 ? x : fallback;
  };
  _bands = {
    perfect: 0,
    acceptable: n(settings?.["reports.absence_acceptable_max"], ABSENCE_BANDS.acceptable),
    high: n(settings?.["reports.absence_high_max"], ABSENCE_BANDS.high),
  };
  _attentionAt = n(settings?.["reports.absence_attention_threshold"], ATTENTION_AT);
  return { bands: _bands, attentionAt: _attentionAt };
}

export const getAbsencePolicy = () => ({ bands: _bands, attentionAt: _attentionAt });

export function classifyAbsence(absences) {
  const a = Number(absences) || 0;
  if (a <= _bands.perfect)    return { status: "Perfect",      band: "green",    flag: "" };
  if (a <= _bands.acceptable) return { status: "Acceptable",   band: "yellow",   flag: "" };
  if (a <= _bands.high)       return { status: "High Absence", band: "lightred", flag: "🟠 High" };
  return { status: "Critical", band: "deepred", flag: "🔴 Critical" };
}

const pad2 = (n) => String(n).padStart(2, "0");

// "01 Jun 2026" — locale-stable so the report reads the same everywhere.
function fmtDMY(date) {
  const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${pad2(date.getDate())} ${MON[date.getMonth()]} ${date.getFullYear()}`;
}

// Whole days show as "1", half-days as "0.5" — no trailing ".0".
const fmtDays = (n) => {
  const v = Number(n || 0);
  return Number.isInteger(v) ? v : Number(v.toFixed(1));
};

// Present / Working as a whole-number percent. Guards divide-by-zero for
// employees with no working days yet (joined late, fully on holiday, etc.).
function attendancePct(present, working) {
  const w = Number(working) || 0;
  if (w <= 0) return 0;
  return Math.round((Number(present) || 0) / w * 100);
}

/**
 * Shape the raw /reports/monthly-attendance payload into a render-ready model.
 * Pure — no DOM, no ExcelJS, no `new Date()` (caller passes `today`).
 *
 * @param {object} monthData  the endpoint response ({ month, first_date, last_date, rows, ... })
 * @param {Date}   today      reference date for "report generated" / period end
 */
export function buildMusterModel(monthData, today) {
  const md = monthData || {};
  const [yStr, mStr] = String(md.month || "").split("-");
  const year  = parseInt(yStr, 10);
  const month = parseInt(mStr, 10); // 1..12
  const MONTHS = ["JANUARY","FEBRUARY","MARCH","APRIL","MAY","JUNE",
                  "JULY","AUGUST","SEPTEMBER","OCTOBER","NOVEMBER","DECEMBER"];
  const monthLabel = (year && month)
    ? `${MONTHS[month - 1]} ${year}`
    : String(md.month || "").toUpperCase();

  // Period: from the month's first date to either today (current month) or
  // the month's last date (a completed past month).
  const firstDate = md.first_date ? new Date(`${md.first_date}T00:00:00`) : null;
  const lastDate  = md.last_date  ? new Date(`${md.last_date}T00:00:00`)  : null;
  const isCurrentMonth = year === today.getFullYear() && month === today.getMonth() + 1;
  const periodStart = firstDate ? fmtDMY(firstDate) : "—";
  const periodEnd   = isCurrentMonth ? fmtDMY(today) : (lastDate ? fmtDMY(lastDate) : "—");

  const sorted = [...(md.rows || [])].sort((a, b) =>
    (a.name || "").localeCompare(b.name || ""));

  const rows = sorted.map((r, i) => {
    const working = Number(r.working_days ?? 0);
    const present = Number(r.present_days ?? 0);
    const absent  = Number(r.absent_days  ?? 0);
    const leave   = fmtDays(r.leave_days   ?? 0);
    const pct     = attendancePct(present, working);
    const { status, band, flag } = classifyAbsence(absent);
    return {
      serial: i + 1,
      name: r.name || "—",
      department: r.department || "—",
      designation: r.designation || "—",
      working, present, absent, leave, pct, status, band, flag,
    };
  });

  // Totals across everyone (used by the bottom row of sheet 1).
  const totals = rows.reduce((t, r) => {
    t.working += r.working; t.present += r.present;
    t.absent  += r.absent;  t.leave   += r.leave;
    return t;
  }, { working: 0, present: 0, absent: 0, leave: 0 });
  totals.leave = fmtDays(totals.leave);
  totals.pct = attendancePct(totals.present, totals.working);

  // Department roll-up.
  const deptMap = new Map();
  rows.forEach((r) => {
    const key = r.department || "—";
    if (!deptMap.has(key)) {
      deptMap.set(key, { department: key, headcount: 0, working: 0, present: 0, absent: 0, leave: 0 });
    }
    const d = deptMap.get(key);
    d.headcount += 1; d.working += r.working; d.present += r.present;
    d.absent += r.absent; d.leave += r.leave;
  });
  const deptSummary = [...deptMap.values()]
    .map((d) => ({ ...d, leave: fmtDays(d.leave), avgPct: attendancePct(d.present, d.working) }))
    .sort((a, b) => a.department.localeCompare(b.department));

  // Attention list — anyone at or past the flag threshold, worst first.
  const attention = rows
    .filter((r) => r.absent >= _attentionAt)
    .sort((a, b) => b.absent - a.absent)
    .map((r, i) => ({ ...r, attnSerial: i + 1 }));

  return {
    monthLabel,
    title: `ATTENDANCE MUSTER REPORT — ${monthLabel}`,
    subtitle: `Period: ${periodStart} – ${periodEnd}  |  Report generated: ${fmtDMY(today)}  |  Total employees: ${rows.length}`,
    deptTitle: `DEPARTMENT SUMMARY — ${monthLabel}`,
    // The threshold is stated on the sheet: a red-banded action list whose
    // selection rule is invisible invites the reader to assume it is theirs.
    attnTitle: `ATTENTION REQUIRED — ${monthLabel}  ·  ${_attentionAt}+ absent days`,
    fileMonth: md.month || `${year}-${pad2(month)}`,
    rows, totals, deptSummary, attention,
  };
}

// ── ExcelJS styling helpers ───────────────────────────────────────────────
const solid = (argb) => ({ type: "pattern", pattern: "solid", fgColor: { argb } });
const thin  = { style: "thin", color: { argb: BORDER } };
const allBorders = { top: thin, left: thin, bottom: thin, right: thin };

function styleBand(ws, fromRow, toRow, ncols, argb, { size = 12, bold = true, color = "FFFFFFFF" } = {}) {
  ws.mergeCells(fromRow, 1, toRow, ncols);
  const cell = ws.getCell(fromRow, 1);
  cell.fill = solid(argb);
  cell.font = { name: FONT, bold, size, color: { argb: color } };
  cell.alignment = { vertical: "middle", horizontal: "center" };
}

function headerRow(ws, rowIdx, labels) {
  const row = ws.getRow(rowIdx);
  labels.forEach((label, i) => {
    const cell = row.getCell(i + 1);
    cell.value = label;
    cell.fill = solid(HEADER);
    cell.font = { name: FONT, bold: true, size: 11, color: { argb: "FFFFFFFF" } };
    cell.alignment = { vertical: "middle", horizontal: "center", wrapText: true };
    cell.border = allBorders;
  });
  row.height = 26;
}

/** Build the styled workbook from a model. Returns an ExcelJS.Workbook. */
export async function renderWorkbook(model) {
  const ExcelJS = (await import("exceljs")).default;
  const wb = new ExcelJS.Workbook();
  wb.creator = "Temveris";

  // ── Sheet 1: Attendance Report ──────────────────────────────────────────
  const COLS = [
    { header: "#",                 key: "serial",      width: 6,  align: "center" },
    { header: "Employee Name",     key: "name",        width: 24, align: "left"   },
    { header: "Department",        key: "department",  width: 22, align: "left"   },
    { header: "Designation",       key: "designation", width: 28, align: "left"   },
    { header: "Working Days",      key: "working",     width: 13, align: "center" },
    { header: "Present Days",      key: "present",     width: 13, align: "center" },
    { header: "Absent Days",       key: "absent",      width: 12, align: "center" },
    { header: "Leave Days",        key: "leave",       width: 11, align: "center" },
    { header: "Attendance %",      key: "pct",         width: 14, align: "center", pct: true },
    { header: "Status",            key: "status",      width: 15, align: "center" },
  ];
  const NCOL = COLS.length;
  const ws = wb.addWorksheet("Attendance Report", {
    views: [{ state: "frozen", ySplit: 4 }],
  });
  ws.columns = COLS.map((c) => ({ width: c.width }));

  styleBand(ws, 1, 1, NCOL, NAVY,     { size: 16 });
  ws.getRow(1).height = 30;
  ws.getCell(1, 1).value = model.title;
  styleBand(ws, 2, 2, NCOL, BLUE_SUB, { size: 10, bold: false });
  ws.getRow(2).height = 20;
  ws.getCell(2, 1).value = model.subtitle;
  ws.getRow(3).height = 6; // thin spacer
  headerRow(ws, 4, COLS.map((c) => c.header));

  model.rows.forEach((r, i) => {
    const rowIdx = 5 + i;
    const row = ws.getRow(rowIdx);
    const band = BANDS[r.band];
    COLS.forEach((c, ci) => {
      const cell = row.getCell(ci + 1);
      cell.value = r[c.key];
      if (c.pct) cell.numFmt = '0"%"';
      cell.fill = solid(band.fill);
      cell.font = { name: FONT, size: 11, color: { argb: band.font },
                    bold: c.key === "status" || c.key === "name" };
      cell.alignment = { vertical: "middle", horizontal: c.align };
      cell.border = allBorders;
    });
    row.height = 20;
  });

  // Totals row.
  const totalRowIdx = 5 + model.rows.length;
  const totRow = ws.getRow(totalRowIdx);
  const totals = {
    1: "", 2: "TOTAL", 3: "", 4: "",
    5: model.totals.working, 6: model.totals.present,
    7: model.totals.absent, 8: model.totals.leave,
    9: model.totals.pct, 10: "",
  };
  COLS.forEach((c, ci) => {
    const cell = totRow.getCell(ci + 1);
    cell.value = totals[ci + 1];
    if (c.pct) cell.numFmt = '0"%"';
    cell.fill = solid(TOTAL_BG);
    cell.font = { name: FONT, bold: true, size: 11, color: { argb: "FF000000" } };
    cell.alignment = { vertical: "middle", horizontal: c.align };
    cell.border = allBorders;
  });
  totRow.height = 22;

  ws.autoFilter = { from: { row: 4, column: 1 }, to: { row: 4, column: NCOL } };

  // ── Sheet 2: Dept Summary ───────────────────────────────────────────────
  const DCOLS = [
    { header: "Department",         key: "department", width: 26, align: "left"   },
    { header: "Headcount",          key: "headcount",  width: 12, align: "center" },
    { header: "Total Working Days", key: "working",    width: 18, align: "center" },
    { header: "Present Days",       key: "present",    width: 14, align: "center" },
    { header: "Absent Days",        key: "absent",     width: 13, align: "center" },
    { header: "Leave Days",         key: "leave",      width: 12, align: "center" },
    { header: "Avg Attendance %",   key: "avgPct",     width: 17, align: "center", pct: true },
  ];
  const ds = wb.addWorksheet("Dept Summary", { views: [{ state: "frozen", ySplit: 3 }] });
  ds.columns = DCOLS.map((c) => ({ width: c.width }));
  styleBand(ds, 1, 1, DCOLS.length, NAVY, { size: 14 });
  ds.getRow(1).height = 28;
  ds.getCell(1, 1).value = model.deptTitle;
  ds.getRow(2).height = 6;
  headerRow(ds, 3, DCOLS.map((c) => c.header));
  model.deptSummary.forEach((d, i) => {
    const row = ds.getRow(4 + i);
    DCOLS.forEach((c, ci) => {
      const cell = row.getCell(ci + 1);
      cell.value = d[c.key];
      if (c.pct) cell.numFmt = '0"%"';
      cell.font = { name: FONT, size: 11, color: { argb: "FF000000" }, bold: c.key === "department" };
      cell.alignment = { vertical: "middle", horizontal: c.align };
      cell.border = allBorders;
      cell.fill = solid(i % 2 ? "FFF2F2F2" : "FFFFFFFF");
    });
    row.height = 20;
  });
  ds.autoFilter = { from: { row: 3, column: 1 }, to: { row: 3, column: DCOLS.length } };

  // ── Sheet 3: Attention Required ─────────────────────────────────────────
  const ACOLS = [
    { header: "#",             key: "attnSerial",  width: 6,  align: "center" },
    { header: "Employee Name", key: "name",        width: 24, align: "left"   },
    { header: "Department",    key: "department",  width: 22, align: "left"   },
    { header: "Designation",   key: "designation", width: 28, align: "left"   },
    { header: "Absent Days",   key: "absent",      width: 13, align: "center" },
    { header: "Leave Days",    key: "leave",       width: 12, align: "center" },
    { header: "Attendance %",  key: "pct",         width: 14, align: "center", pct: true },
    { header: "Flag",          key: "flag",        width: 16, align: "center" },
  ];
  const as = wb.addWorksheet("Attention Required", { views: [{ state: "frozen", ySplit: 3 }] });
  as.columns = ACOLS.map((c) => ({ width: c.width }));
  styleBand(as, 1, 1, ACOLS.length, RED_TITLE, { size: 14 });
  as.getRow(1).height = 28;
  as.getCell(1, 1).value = model.attnTitle;
  as.getRow(2).height = 6;
  headerRow(as, 3, ACOLS.map((c) => c.header));
  if (model.attention.length === 0) {
    as.mergeCells(4, 1, 4, ACOLS.length);
    const cell = as.getCell(4, 1);
    cell.value = "🎉  No employees require attention — every employee is within the acceptable absence threshold.";
    cell.font = { name: FONT, size: 11, italic: true, color: { argb: "FF375623" } };
    cell.alignment = { vertical: "middle", horizontal: "center" };
    cell.fill = solid("FFE2EFDA");
    as.getRow(4).height = 24;
  } else {
    model.attention.forEach((r, i) => {
      const row = as.getRow(4 + i);
      const band = BANDS[r.band];
      ACOLS.forEach((c, ci) => {
        const cell = row.getCell(ci + 1);
        cell.value = r[c.key];
        if (c.pct) cell.numFmt = '0"%"';
        cell.fill = solid(band.fill);
        cell.font = { name: FONT, size: 11, color: { argb: band.font },
                      bold: c.key === "flag" || c.key === "name" };
        cell.alignment = { vertical: "middle", horizontal: c.align };
        cell.border = allBorders;
      });
      row.height = 20;
    });
    as.autoFilter = { from: { row: 3, column: 1 }, to: { row: 3, column: ACOLS.length } };
  }

  return wb;
}

/**
 * Generate the muster workbook and trigger a browser download.
 * @param {object} monthData  the /reports/monthly-attendance payload
 */
export async function exportAttendanceMuster(monthData) {
  // Resolve THIS organization's absence policy before building the model —
  // the thresholds decide who lands on the "Attention Required" sheet, so they
  // must be the tenant's, not the client's. A failed fetch leaves the system
  // defaults in place rather than blocking the export.
  try {
    const { default: API } = await import("../api/axios");
    const res = await API.get("/settings");
    setAbsencePolicy(res?.data?.settings || {});
  } catch {
    /* keep whatever policy is already loaded */
  }
  const model = buildMusterModel(monthData, new Date());
  const wb = await renderWorkbook(model);
  const buffer = await wb.xlsx.writeBuffer();
  const blob = new Blob([buffer], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `attendance_muster_${model.fileMonth}.xlsx`;
  a.click();
  URL.revokeObjectURL(url);
}
