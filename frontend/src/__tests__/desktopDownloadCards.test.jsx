/**
 * Per-OS desktop download cards.
 *
 * Two properties matter. A card's label must match what the click actually does
 * — Windows downloads a file, Linux and macOS navigate to instructions, and
 * calling the latter "Download" would leave the user waiting for a file that
 * never arrives. And availability is derived from config alone, so the component
 * makes no network requests and cannot hang or offer a link that 404s.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import DesktopDownloadCards from "../components/auth/DesktopDownloadCards";

const renderCards = () =>
  render(<MemoryRouter><DesktopDownloadCards /></MemoryRouter>);

function stubUserAgent(ua, platform = "") {
  vi.stubGlobal("navigator", { userAgent: ua, platform });
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("DesktopDownloadCards", () => {
  it("shows all three operating systems", () => {
    renderCards();
    expect(screen.getByText("Windows")).toBeInTheDocument();
    expect(screen.getByText("macOS")).toBeInTheDocument();
    expect(screen.getByText("Linux")).toBeInTheDocument();
  });

  it("makes no network requests", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    renderCards();
    // Availability is a pure function of config — nothing to probe, so nothing
    // that can hang or fail.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("downloads Windows directly from the published installer", () => {
    renderCards();
    const win = screen.getByLabelText("Download Temveris for Windows");
    expect(win).toHaveAttribute("href", expect.stringContaining("Temveris-Setup.exe"));
    expect(win).toHaveAttribute("download");
  });

  it("downloads Windows in place rather than opening a tab", () => {
    renderCards();
    // A blank tab would flash open and immediately close once the binary
    // content type takes over.
    expect(screen.getByLabelText("Download Temveris for Windows"))
      .not.toHaveAttribute("target");
  });

  it("sends Linux to its install guide instead of downloading the .deb", () => {
    // Double-clicking a .deb opens Archive Manager on most Ubuntu desktops and
    // installs nothing.
    renderCards();
    const linux = screen.getByLabelText(
      "Temveris for Linux — download and install guide",
    );
    expect(linux).toHaveAttribute("href", "/install/linux");
    expect(linux).not.toHaveAttribute("download");
  });

  it("sends macOS to its install guide instead of downloading the .dmg", () => {
    // Gatekeeper commonly reports a downloaded .dmg as "damaged", which invites
    // the user to bin a working app.
    renderCards();
    const mac = screen.getByLabelText(
      "Temveris for macOS — download and install guide",
    );
    expect(mac).toHaveAttribute("href", "/install/mac");
    expect(mac).not.toHaveAttribute("download");
  });

  it("labels the guide cards for what the click actually does", () => {
    renderCards();
    expect(screen.getAllByText("Install guide")).toHaveLength(2);
    expect(screen.getByText("Download")).toBeInTheDocument();
  });

  it("offers every platform — none are Coming soon", () => {
    renderCards();
    expect(screen.queryByText("Coming soon")).toBeNull();
  });

  it("highlights a macOS visitor's own card", () => {
    stubUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "MacIntel");
    renderCards();
    expect(screen.getByText("Your device")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Temveris for macOS — download and install guide"),
    ).toHaveTextContent("Your device");
  });

  it("highlights a Linux visitor's own card", () => {
    stubUserAgent("Mozilla/5.0 (X11; Linux x86_64)", "Linux x86_64");
    renderCards();
    expect(
      screen.getByLabelText("Temveris for Linux — download and install guide"),
    ).toHaveTextContent("Your device");
  });

  it("highlights a Windows visitor's own card", () => {
    stubUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Win32");
    renderCards();
    expect(screen.getByLabelText("Download Temveris for Windows"))
      .toHaveTextContent("Your device");
  });

  it("claims no device when the platform is unrecognised", () => {
    stubUserAgent("SomeUnknownAgent/1.0", "");
    renderCards();
    expect(screen.queryByText("Your device")).toBeNull();
  });

  it("highlights exactly one card at most", () => {
    stubUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "MacIntel");
    renderCards();
    expect(screen.getAllByText("Your device")).toHaveLength(1);
  });
});
