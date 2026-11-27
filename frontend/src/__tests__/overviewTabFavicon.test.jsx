import { describe, it, expect } from "vitest";
import { getTabFavicon } from "../components/admin/adminShared";

const FAV = "https://github.com/favicon.ico";

describe("getTabFavicon — Overview app chip icon source", () => {
  it("uses the site favicon when the foreground app is a browser", () => {
    expect(getTabFavicon("chrome.exe", { domain: "github.com", favicon_url: FAV })).toBe(FAV);
    expect(getTabFavicon("msedge.exe", { domain: "github.com", favicon_url: FAV })).toBe(FAV);
    expect(getTabFavicon("Brave Browser", { domain: "github.com", favicon_url: FAV })).toBe(FAV);
  });

  it("keeps the app icon for non-browser apps holding stale tab data", () => {
    // /tracking/heartbeat overwrites app_name without clearing the extension's
    // tab columns, so a VS Code row can still carry the last Chrome favicon.
    expect(getTabFavicon("Code.exe", { domain: "github.com", favicon_url: FAV })).toBeNull();
    expect(getTabFavicon("WINWORD.EXE", { domain: "github.com", favicon_url: FAV })).toBeNull();
    expect(getTabFavicon("Illustrator.exe", { domain: "github.com", favicon_url: FAV })).toBeNull();
  });

  it("returns null when there is nothing safe to render", () => {
    expect(getTabFavicon("chrome.exe", null)).toBeNull();
    expect(getTabFavicon("chrome.exe", { domain: "localhost", favicon_url: null })).toBeNull();
    expect(getTabFavicon("", { favicon_url: FAV })).toBeNull();
    // Non-loadable schemes are skipped rather than rendered broken.
    expect(getTabFavicon("chrome.exe", { favicon_url: "chrome-extension://abc/icon.png" })).toBeNull();
  });

  it("accepts inline data: favicons the extension forwards", () => {
    const inline = "data:image/png;base64,iVBORw0KGgo=";
    expect(getTabFavicon("chrome.exe", { favicon_url: inline })).toBe(inline);
  });
});
