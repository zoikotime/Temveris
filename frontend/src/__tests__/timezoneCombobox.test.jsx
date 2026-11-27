/**
 * The Organization Timezone combobox — behaviour an admin depends on.
 *
 * The control this replaced was free text, which is how one production
 * workspace came to store "California, Sacramento": not an IANA zone, so it
 * silently fell back to another organization's clock while the settings screen
 * displayed a US city. A <datalist> fixed the validity problem but only
 * revealed options after typing, which is useless to an admin who does not
 * already know IANA naming.
 *
 * So the two things under test are: the list is visible on click WITHOUT
 * typing, and nothing outside the list can be chosen.
 */
import React, { useState } from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

import TimezoneCombobox from "../components/TimezoneCombobox";
import { isSelectableZone, listIanaZones, zoneLabel, zoneSearchText } from "../lib/orgTime";

const C = { border: "#333", surface: "#111", text: "#eee", textLight: "#999",
            brand: "#44B97B", danger: "#DC2626", hover: "#222" };

const openIt = () => fireEvent.click(screen.getByRole("combobox"));

/** Controlled wrapper — the real caller keeps the value in its own state. */
function Harness({ initial = "", onPick = () => {}, invalid = false }) {
  const [tz, setTz] = useState(initial);
  return (
    <>
      <TimezoneCombobox
        value={tz}
        onChange={(v) => { setTz(v); onPick(v); }}
        C={C} invalid={invalid} />
      <output data-testid="value">{tz}</output>
    </>
  );
}

describe("opens without typing", () => {
  it("shows timezone options as soon as it is clicked", () => {
    render(<Harness />);
    expect(screen.queryByRole("listbox")).toBeNull();
    openIt();
    const list = screen.getByRole("listbox");
    // A full panel of browsable options, unfiltered. The list is capped (each
    // row builds two Intl formatters for its readable name; all 420 would
    // stall the click by ~450ms) and the remainder is announced.
    const shown = within(list).getAllByRole("option").length;
    expect(shown).toBeGreaterThan(50);
    expect(within(list).getByText(/more — keep typing/i)).toBeTruthy();
  });

  it("shows the well-known zones in that first view, without scrolling past 400 rows", () => {
    // Identifiers sort alphabetically, so UTC lands near position 400 and
    // Europe/London past 250 — with a capped list, browsing would never reach
    // either. The common zones are pinned to the top for exactly that reason.
    render(<Harness />);
    openIt();
    const list = screen.getByRole("listbox");
    for (const zone of ["UTC", "Asia/Kolkata", "America/Los_Angeles",
                        "America/New_York", "Europe/London", "Europe/Berlin"]) {
      expect(within(list).getByText(zone)).toBeTruthy();
    }
  });

  it("pins the currently saved zone first, so it is always visible", () => {
    render(<Harness initial="Pacific/Chatham" />);
    openIt();
    const first = within(screen.getByRole("listbox")).getAllByRole("option")[0];
    expect(first.textContent).toContain("Pacific/Chatham");
  });

  it("opens from the keyboard with ArrowDown", () => {
    render(<Harness />);
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "ArrowDown" });
    expect(screen.getByRole("listbox")).toBeTruthy();
  });
});

describe("searching filters the options", () => {
  const search = (text) => {
    openIt();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: text } });
    return screen.getByRole("listbox");
  };

  it("narrows the list as you type", () => {
    render(<Harness />);
    const all = listIanaZones().length;
    const list = search("Europe");
    const shown = within(list).getAllByRole("option").length;
    expect(shown).toBeGreaterThan(0);
    expect(shown).toBeLessThan(all);
    within(list).getAllByRole("option")
      .forEach(o => expect(o.textContent.toLowerCase()).toContain("europe"));
  });

  it("is case-insensitive and matches the city alone", () => {
    render(<Harness />);
    expect(within(search("london")).getByText("Europe/London")).toBeTruthy();
  });

  it("finds Asia/Calcutta when an admin types Kolkata", () => {
    // The browser tz database exposes the CLDR-canonical "Asia/Calcutta"; no
    // admin should have to know that to configure an Indian workspace.
    render(<Harness />);
    const list = search("Kolkata");
    const shown = within(list).getAllByRole("option").map(o => o.textContent);
    expect(shown.some(t => t.includes("Asia/Calcutta"))).toBe(true);
    expect(shown.some(t => t.includes("Asia/Kolkata"))).toBe(true);
  });

  it("treats underscores as spaces, so 'Los Angeles' matches", () => {
    render(<Harness />);
    expect(within(search("los angeles")).getByText("America/Los_Angeles")).toBeTruthy();
  });

  it("says so when nothing matches, and offers nothing", () => {
    render(<Harness />);
    const list = search("California, Sacramento");
    expect(within(list).queryAllByRole("option")).toHaveLength(0);
    expect(within(list).getByText(/no timezones match/i)).toBeTruthy();
  });
});

describe("selection", () => {
  it("selects Asia/Kolkata by click and reports it to the caller", () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    openIt();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "Asia/Kolkata" } });
    fireEvent.mouseDown(within(screen.getByRole("listbox")).getByText("Asia/Kolkata"));
    expect(onPick).toHaveBeenCalledWith("Asia/Kolkata");
    expect(screen.getByTestId("value").textContent).toBe("Asia/Kolkata");
    expect(screen.queryByRole("listbox")).toBeNull();      // closes on select
  });

  it("selects with the keyboard: arrows then Enter", () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    openIt();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "Europe/Berlin" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith("Europe/Berlin");
  });

  it("closes on Escape without changing the value", () => {
    const onPick = vi.fn();
    render(<Harness initial="Europe/London" onPick={onPick} />);
    openIt();
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onPick).not.toHaveBeenCalled();
    expect(screen.getByTestId("value").textContent).toBe("Europe/London");
  });

  it("only ever emits values the validator accepts", () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    openIt();
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    for (const el of options.slice(0, 40)) {
      fireEvent.mouseDown(el);
      openIt();
    }
    for (const call of onPick.mock.calls) {
      expect(isSelectableZone(call[0])).toBe(true);
    }
  });
});

describe("existing stored values", () => {
  it("displays a valid stored zone, including Asia/Calcutta", () => {
    render(<Harness initial="Asia/Calcutta" />);
    expect(screen.getByRole("combobox").textContent).toContain("Asia/Calcutta");
    expect(isSelectableZone("Asia/Calcutta")).toBe(true);
  });

  it("marks the selected zone in the list", () => {
    render(<Harness initial="Europe/London" />);
    openIt();
    const selected = within(screen.getByRole("listbox"))
      .getAllByRole("option").filter(o => o.getAttribute("aria-selected") === "true");
    expect(selected).toHaveLength(1);
    expect(selected[0].textContent).toContain("Europe/London");
  });

  it("shows an invalid stored value as-is rather than swapping it silently", () => {
    // "California, Sacramento" is the real production value. It must remain
    // visible — and flagged — so an admin editing an unrelated field cannot
    // change the workspace clock by accident.
    render(<Harness initial="California, Sacramento" invalid />);
    const field = screen.getByRole("combobox");
    expect(field.textContent).toContain("California, Sacramento");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(isSelectableZone("California, Sacramento")).toBe(false);
  });
});

describe("accessibility", () => {
  it("exposes combobox/listbox/option roles and expanded state", () => {
    render(<Harness />);
    const closed = screen.getByRole("combobox");
    expect(closed.getAttribute("aria-expanded")).toBe("false");
    expect(closed.getAttribute("aria-haspopup")).toBe("listbox");
    openIt();
    expect(screen.getByRole("combobox").getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("listbox")).toBeTruthy();
  });

  it("tracks the active option with aria-activedescendant", () => {
    render(<Harness />);
    openIt();
    const input = screen.getByRole("combobox");
    const first = input.getAttribute("aria-activedescendant");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.getAttribute("aria-activedescendant")).not.toBe(first);
  });

  it("labels the control for screen readers", () => {
    render(<Harness />);
    expect(screen.getByRole("combobox").getAttribute("aria-label")).toBeTruthy();
  });
});

describe("human-readable labels", () => {
  it("shows the zone name and offset beside the identifier", () => {
    render(<Harness />);
    openIt();
    const row = within(screen.getByRole("listbox")).getByText("Asia/Kolkata")
      .closest("[role=option]");
    expect(row.textContent).toContain("Asia/Kolkata");
    expect(row.textContent).toContain("India Standard Time");
    expect(row.textContent).toMatch(/GMT\+5:30/);
  });

  it("labels the closed field readably once a zone is chosen", () => {
    render(<Harness initial="Asia/Kolkata" />);
    const field = screen.getByRole("combobox").textContent;
    expect(field).toContain("Asia/Kolkata");
    expect(field).toContain("India Standard Time");
  });

  it("tells the admin that Asia/Calcutta is the same zone as Asia/Kolkata", () => {
    // Requirement: an administrator must not have to know which technical
    // identifier their browser exposes.
    render(<Harness />);
    openIt();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "Calcutta" } });
    const row = within(screen.getByRole("listbox")).getByText("Asia/Calcutta")
      .closest("[role=option]");
    expect(row.textContent).toContain("Asia/Kolkata");
  });

  it("saves the bare IANA identifier, never the label", () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    openIt();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "Kolkata" } });
    fireEvent.mouseDown(within(screen.getByRole("listbox")).getByText("Asia/Kolkata"));
    expect(onPick).toHaveBeenCalledWith("Asia/Kolkata");
    expect(screen.getByTestId("value").textContent).toBe("Asia/Kolkata");
  });

  it("finds and selects UTC", () => {
    const onPick = vi.fn();
    render(<Harness onPick={onPick} />);
    openIt();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "UTC" } });
    fireEvent.mouseDown(within(screen.getByRole("listbox")).getByText("UTC"));
    expect(onPick).toHaveBeenCalledWith("UTC");
  });

  it("shows an unusable stored value verbatim, not dressed up as a zone", () => {
    render(<Harness initial="California, Sacramento" invalid />);
    expect(screen.getByRole("combobox").textContent).toContain("California, Sacramento");
    expect(screen.getByRole("combobox").textContent).not.toMatch(/GMT[+-]/);
  });

  it("degrades to the identifier when a zone has no readable name", () => {
    expect(zoneLabel("Not/AZone").label).toBe("Not/AZone");
  });
});

describe("search text helper", () => {
  it("carries the identifier, the spaced city and any alias", () => {
    expect(zoneSearchText("Asia/Calcutta")).toContain("kolkata");
    expect(zoneSearchText("Asia/Kolkata")).toContain("calcutta");
    expect(zoneSearchText("America/Los_Angeles")).toContain("los angeles");
  });
});
