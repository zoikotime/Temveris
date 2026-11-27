const $ = (id) => document.getElementById(id);

async function load() {
  const { apiBase = "", token = "" } = await chrome.storage.local.get(["apiBase", "token"]);
  $("apiBase").value = apiBase;
  $("token").value = token;
  refreshLive();
}

function setStatus(msg, kind) {
  const el = $("status");
  el.textContent = msg;
  el.className = "status " + (kind === "ok" ? "ok" : "err");
}

async function save() {
  const apiBase = $("apiBase").value.trim().replace(/\/+$/, "");
  const token   = $("token").value.trim();
  if (!apiBase || !token) {
    setStatus("Both fields are required.", "err");
    return;
  }
  // Round-trip a heartbeat call to verify credentials.
  try {
    const r = await fetch(`${apiBase}/browser/live`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!r.ok) {
      setStatus(`Auth failed (${r.status}). Check token.`, "err");
      return;
    }
  } catch (e) {
    setStatus("Could not reach API. Check URL.", "err");
    return;
  }
  await chrome.storage.local.set({ apiBase, token });
  setStatus("Connected — tracking will start on the next tab switch.", "ok");
  refreshLive();
}

async function refreshLive() {
  const { current } = (await chrome.storage.session?.get?.("current")) || {};
  const el = $("live");
  if (!current) { el.style.display = "none"; return; }
  el.style.display = "block";
  el.innerHTML =
    `<strong>${current.domain || "—"}</strong><br>` +
    `<span>${(current.tabTitle || "").slice(0, 80)}</span><br>` +
    `<small>started ${new Date(current.startedAt).toLocaleTimeString()}</small>`;
}

$("save").addEventListener("click", save);
load();
