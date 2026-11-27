/**
 * The shared "Download App" header CTA.
 *
 * It is the only route to the desktop installers for anyone already signed in,
 * so the one thing that must never break is where it goes. The other case worth
 * pinning is the desktop shell: offering to download the app from inside the
 * app is a dead end, so it renders nothing there.
 */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import DownloadAppButton from "../components/DownloadAppButton";

// Same test-only shim as authShell.test.jsx: under vitest this project's JSX
// compiles to the CLASSIC runtime, so components need `React` in scope.
globalThis.React = React;

const renderButton = (props = {}) =>
  render(
    <MemoryRouter initialEntries={["/admin"]}>
      <Routes>
        <Route path="/admin" element={<DownloadAppButton {...props} />} />
        <Route path="/download" element={<p>download page</p>} />
      </Routes>
    </MemoryRouter>,
  );

afterEach(() => { vi.unstubAllGlobals(); });

describe("DownloadAppButton", () => {
  it("routes to the shared download page", () => {
    // Not to a single installer: the right file depends on the user's OS, and
    // for macOS/Linux the right destination is a guide rather than a download.
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /download app/i }));
    expect(screen.getByText("download page")).toBeInTheDocument();
  });

  it("uses the caller's translated label", () => {
    renderButton({ label: "Descargar App" });
    expect(screen.getByRole("button", { name: /descargar app/i })).toBeInTheDocument();
  });

  it("renders nothing inside the Electron desktop shell", () => {
    // The app is already installed and running there.
    vi.stubGlobal("isDesktopApp", true);
    const { container } = renderButton();
    expect(container).toBeEmptyDOMElement();
  });

  it("takes the compact shape when the header around it runs smaller controls", () => {
    // The employee dashboard's header is 60px with 12px/8px-radius buttons; the
    // default pill would sit in that cluster as an obvious outsider.
    renderButton({ size: "sm" });
    expect(screen.getByRole("button", { name: /download app/i }))
      .toHaveClass("zt-dl-btn", "zt-dl-btn--sm");
  });

  it("is a button, not a submit control", () => {
    // Both dashboards' headers sit inside larger interactive shells; a default
    // submit type would make it fire any enclosing form.
    renderButton();
    expect(screen.getByRole("button", { name: /download app/i }))
      .toHaveAttribute("type", "button");
  });
});
