/**
 * Temveris Tracker — content script
 * Counts user interactions (mouse clicks, key presses, scrolls) on the page
 * and forwards them to the background worker in batches every FLUSH_MS.
 * We never capture *what* was typed or clicked — only counts.
 */
(() => {
  const FLUSH_MS = 5000;
  const KEY_BURST_WINDOW_MS = 1000;
  const KEY_BURST_CAP = 12;
  let clicks = 0, keys = 0, scrolls = 0;
  let scrollPending = false;
  let keyBurstWindowTs = 0;
  let keyBurstCount = 0;

  const onClick    = () => { clicks++; };
  const onKey      = (event) => {
    if (/^(Control|Shift|Alt|Meta|CapsLock|NumLock|ScrollLock)$/i.test(event.key || "")) return;
    const now = Date.now();
    if (!keyBurstWindowTs || now - keyBurstWindowTs >= KEY_BURST_WINDOW_MS) {
      keyBurstWindowTs = now;
      keyBurstCount = 0;
    }
    if (keyBurstCount >= KEY_BURST_CAP) return;
    keyBurstCount++;
    keys++;
  };
  const onScroll   = () => {
    // Coalesce rapid scroll events — one "scroll" per idle frame.
    if (scrollPending) return;
    scrollPending = true;
    requestAnimationFrame(() => { scrolls++; scrollPending = false; });
  };

  window.addEventListener("click",  onClick,  { passive: true, capture: true });
  window.addEventListener("keydown", onKey,   { passive: true, capture: true });
  window.addEventListener("scroll", onScroll, { passive: true, capture: true });

  setInterval(() => {
    if (!clicks && !keys && !scrolls) return;
    try {
      chrome.runtime.sendMessage({
        type: "zt-interaction",
        clicks, keys, scrolls,
      });
    } catch (_) { /* background may be asleep — numbers carry to next flush */ }
    clicks = 0; keys = 0; scrolls = 0;
    keyBurstWindowTs = 0; keyBurstCount = 0;
  }, FLUSH_MS);
})();
