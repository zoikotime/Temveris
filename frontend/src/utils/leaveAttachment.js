import { API_BASE } from "../config";
import { readToken } from "../api/authStorage";

// Open a leave request's supporting document.
//
// The document is NOT reachable by a plain <a href>. GET /leave/{id}/attachment
// requires a Bearer token and checks that the caller is the employee who filed
// the request or an admin/hr reviewer in the same organization — a check the
// old direct link to `attachment_path` skipped entirely, because that path was
// served by an unauthenticated static mount.
//
// A browser cannot attach an Authorization header to a link, so we fetch the
// bytes and show them from an object URL, the same pattern AuthImage uses for
// protected screenshots.
//
// The tab is opened SYNCHRONOUSLY, before the fetch. Browsers only allow
// window.open while a user gesture is still on the stack; opening it after
// `await` — which is the obvious way to write this — gets blocked as a popup
// in Chrome, Safari and Firefox alike. So we claim the tab during the click,
// then point it at the blob once it arrives.
export async function openLeaveAttachment(leaveId) {
  const win = window.open("", "_blank");
  if (!win) {
    throw new Error("Your browser blocked the document window. Please allow pop-ups for this site.");
  }
  // Something to look at while the bytes are in flight, and something other
  // than a blank tab if the fetch fails before we can close it.
  try {
    win.document.write("<title>Opening document…</title><p style=\"font:14px system-ui;padding:24px\">Opening document…</p>");
    win.document.close();
  } catch { /* cross-origin-ish edge in some embedders; not worth failing over */ }

  try {
    const token = readToken();
    const res = await fetch(`${API_BASE}/leave/${leaveId}/attachment`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json())?.detail || ""; } catch { /* non-JSON body */ }
      throw new Error(detail || `Could not open the document (HTTP ${res.status})`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    win.location.href = url;
    // Revoke once the tab has had time to read it. Revoking immediately races
    // the tab's own load and shows a blank page; never revoking leaks the blob
    // for the rest of the session.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (err) {
    // Don't strand the user on a tab that says "Opening document…" forever.
    try { win.close(); } catch { /* already gone */ }
    throw err;
  }
}
