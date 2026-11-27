/**
 * /download — the desktop app download page.
 *
 * It exists because an admin signs in on the web and never sees the "Account
 * created" screen that offers the installer. Two things must hold: the page
 * offers a real installer for every platform (not a placeholder), and it makes
 * no network requests, so it cannot hang or gate the download behind a session.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import DownloadApp from "../pages/DownloadApp";

// Same test-only shim as authShell.test.jsx: under vitest this project's JSX
// compiles to the CLASSIC runtime, so every component in the tree needs `React`
// in scope. `AuthLayout` (rendered by this page) does not import it — it is
// fine in the app, where the build uses the automatic runtime. Deletable once
// vitest.config.js is aligned with vite.config.js.
globalThis.React = React;

const renderPage = () =>
  render(<MemoryRouter><DownloadApp /></MemoryRouter>);

afterEach(() => { vi.unstubAllGlobals(); });

describe("DownloadApp page", () => {
  it("offers the desktop app for every platform", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Download Temveris" })).toBeInTheDocument();
    expect(screen.getByLabelText("Download Temveris for Windows")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Temveris for macOS — download and install guide"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Temveris for Linux — download and install guide"),
    ).toBeInTheDocument();
  });

  it("links Windows straight at the published installer", () => {
    renderPage();
    expect(screen.getByLabelText("Download Temveris for Windows"))
      .toHaveAttribute("href", expect.stringContaining("Temveris-Setup.exe"));
  });

  it("makes no network requests", () => {
    // Nothing to fetch: the page is static, so it cannot hang or 404 on load.
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    renderPage();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("offers a way back for a visitor who arrived from inside the app", () => {
    renderPage();
    expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument();
  });
});
