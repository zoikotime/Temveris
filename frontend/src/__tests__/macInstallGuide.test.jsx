/**
 * macOS install guide.
 *
 * The page exists because Gatekeeper reports a downloaded .dmg as "damaged",
 * which tells the user to bin a working app. So the things pinned here are the
 * exact quarantine command and the explicit "don't bin it, don't reinstall"
 * reassurance — a typo or omission in either sends every Mac user to a dead end.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import MacInstallGuide from "../pages/MacInstallGuide";

const renderGuide = () =>
  render(<MemoryRouter><MacInstallGuide /></MemoryRouter>);

afterEach(() => { vi.unstubAllGlobals(); });

describe("MacInstallGuide", () => {
  it("offers the .dmg from the published bucket", () => {
    renderGuide();
    const link = screen.getByRole("link", { name: /download for macos/i });
    expect(link).toHaveAttribute(
      "href",
      "https://updates.temveris.example/mac/Temveris.dmg",
    );
    expect(link).toHaveAttribute("download");
  });

  it("states the platform requirements and size", () => {
    renderGuide();
    expect(
      screen.getByText(/macOS 12 or later · Apple silicon & Intel · 94 MB/),
    ).toBeInTheDocument();
  });

  it("walks all six steps", () => {
    renderGuide();
    ["Step 1.", "Step 2.", "Step 3.", "Step 4.", "Step 5.", "Step 6."].forEach((s) => {
      expect(screen.getByText(s)).toBeInTheDocument();
    });
  });

  it("explains mounting the dmg and moving the app to Applications", () => {
    renderGuide();
    expect(screen.getByText(/double-click Temveris\.dmg/i)).toBeInTheDocument();
    expect(screen.getByText(/drag the Temveris app onto the Applications folder/i))
      .toBeInTheDocument();
  });

  it("shows the exact quarantine-removal command", () => {
    renderGuide();
    // Verbatim: a typo here leaves the user with an app macOS refuses to open.
    expect(
      screen.getByText("xattr -dr com.apple.quarantine /Applications/Temveris.app"),
    ).toBeInTheDocument();
  });

  it("also covers the app still sitting in Downloads", () => {
    renderGuide();
    expect(
      screen.getByText("xattr -dr com.apple.quarantine ~/Downloads/Temveris.app"),
    ).toBeInTheDocument();
  });

  it("quotes the misleading macOS error and says the app is not really damaged", () => {
    renderGuide();
    expect(screen.getByText(/is damaged and can’t be opened/i)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is actually damaged/i)).toBeInTheDocument();
  });

  it("tells the user not to bin the app or reinstall", () => {
    // The macOS dialog's own advice is to move it to the Bin, which is wrong.
    renderGuide();
    expect(screen.getByText(/don’t move it to the\s+Bin and don’t reinstall/i))
      .toBeInTheDocument();
  });

  it("marks the quarantine step as conditional, not part of the happy path", () => {
    renderGuide();
    expect(screen.getByText(/Only if macOS says the app is damaged/i)).toBeInTheDocument();
    expect(screen.getByText(/Skip this step unless you see the message below/i))
      .toBeInTheDocument();
  });

  it("offers the open command and the Finder route", () => {
    renderGuide();
    expect(screen.getByText("open /Applications/Temveris.app")).toBeInTheDocument();
    expect(screen.getByText(/Open Finder → Applications/i)).toBeInTheDocument();
  });

  it("gives support a diagnostic for a still-blocked app", () => {
    renderGuide();
    expect(screen.getByText("spctl -a -vv /Applications/Temveris.app")).toBeInTheDocument();
    expect(screen.getByText(/Don’t reinstall repeatedly/i)).toBeInTheDocument();
  });

  it("copies a command to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    renderGuide();
    fireEvent.click(
      screen.getByRole("button", {
        name: /copy command: xattr -dr com\.apple\.quarantine \/Applications/i,
      }),
    );

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        "xattr -dr com.apple.quarantine /Applications/Temveris.app",
      ),
    );
  });

  it("still reports success when the clipboard API is unavailable", async () => {
    // Plain http, or a browser withholding permission. The button must not
    // appear to do nothing.
    vi.stubGlobal("navigator", {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    document.execCommand = vi.fn().mockReturnValue(true);

    renderGuide();
    fireEvent.click(
      screen.getByRole("button", { name: /copy command: spctl/i }),
    );
    expect(
      await screen.findByRole("button", { name: /command copied: spctl/i }),
    ).toBeInTheDocument();
  });
});
