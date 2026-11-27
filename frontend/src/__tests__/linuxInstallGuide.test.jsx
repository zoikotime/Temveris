/**
 * Linux install guide.
 *
 * The page exists because a download alone doesn't install a .deb — so the two
 * things it must always get right are the exact install command and the warning
 * not to double-click the file. Both are asserted here rather than trusted.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import LinuxInstallGuide from "../pages/LinuxInstallGuide";

const renderGuide = () =>
  render(<MemoryRouter><LinuxInstallGuide /></MemoryRouter>);

afterEach(() => { vi.unstubAllGlobals(); });

describe("LinuxInstallGuide", () => {
  it("offers the .deb from the published bucket", () => {
    renderGuide();
    const link = screen.getByRole("link", { name: /download for linux/i });
    expect(link).toHaveAttribute(
      "href",
      "https://updates.temveris.example/linux/Temveris-linux.deb",
    );
    expect(link).toHaveAttribute("download");
  });

  it("states the platform requirements and size", () => {
    renderGuide();
    expect(
      screen.getByText(/Ubuntu 22\.04\+ \/ Debian 12\+ · 64-bit · 71 MB/),
    ).toBeInTheDocument();
  });

  it("shows the exact install command", () => {
    renderGuide();
    // Verbatim: a typo here sends every Linux user to a broken install.
    expect(
      screen.getByText("sudo apt install ~/Downloads/Temveris-linux.deb"),
    ).toBeInTheDocument();
  });

  it("warns against double-clicking the .deb", () => {
    renderGuide();
    expect(screen.getByText(/Don't double-click the \.deb file/i)).toBeInTheDocument();
    expect(screen.getByText(/Archive Manager/i)).toBeInTheDocument();
  });

  it("explains how to launch the app after installing", () => {
    renderGuide();
    // Scoped to the launch step: a bare /temveris/i also matches the logo and the
    // page heading.
    const launch = screen.getByText(/Press the Super key/i);
    expect(launch).toBeInTheDocument();
    expect(launch.textContent.toLowerCase()).toContain("temveris");
    expect(launch.textContent).toMatch(/windows key/i);
  });

  it("offers the dependency-repair command for a failed install", () => {
    renderGuide();
    expect(screen.getByText("sudo apt --fix-broken install")).toBeInTheDocument();
  });

  it("walks all four steps in order", () => {
    renderGuide();
    ["Step 1.", "Step 2.", "Step 3.", "Step 4."].forEach((s) => {
      expect(screen.getByText(s)).toBeInTheDocument();
    });
  });

  it("copies the install command to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    renderGuide();
    fireEvent.click(screen.getAllByRole("button", { name: /copy command/i })[0]);

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        "sudo apt install ~/Downloads/Temveris-linux.deb",
      ),
    );
    // Confirms to the user that it worked.
    expect(await screen.findByRole("button", { name: /command copied/i })).toBeInTheDocument();
  });

  it("still reports success when the clipboard API is unavailable", async () => {
    // Plain http, or a browser withholding permission. The button must not
    // appear to do nothing.
    vi.stubGlobal("navigator", {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    document.execCommand = vi.fn().mockReturnValue(true);

    renderGuide();
    fireEvent.click(screen.getAllByRole("button", { name: /copy command/i })[0]);
    expect(await screen.findByRole("button", { name: /command copied/i })).toBeInTheDocument();
  });
});
