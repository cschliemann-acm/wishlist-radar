// Live relative times ("5 min ago", "in 40 min"), with the exact local time on hover.
function relative(ts) {
  const diff = ts * 1000 - Date.now();
  const abs = Math.abs(diff) / 1000;
  const future = diff > 0;
  const units = [["day", 86400], ["hour", 3600], ["min", 60]];
  for (const [unit, sec] of units) {
    if (abs >= sec) {
      const n = Math.floor(abs / sec);
      const label = `${n} ${unit}${n !== 1 && unit !== "min" ? "s" : ""}`;
      return future ? `in ${label}` : `${label} ago`;
    }
  }
  return future ? "in under a min" : "just now";
}

function refreshTimes() {
  document.querySelectorAll("time[data-ts]").forEach((el) => {
    const ts = parseFloat(el.dataset.ts);
    el.textContent = relative(ts);
    el.title = new Date(ts * 1000).toLocaleString();
  });
}
refreshTimes();
setInterval(refreshTimes, 30000);

// While a search is running on an item page, poll and reload when it finishes.
const head = document.querySelector("[data-item-id]");
if (head && head.dataset.checking === "1") {
  const id = head.dataset.itemId;
  const poll = setInterval(async () => {
    try {
      const r = await fetch(`/api/items/${id}/status`);
      const s = await r.json();
      if (!s.checking) { clearInterval(poll); location.reload(); }
    } catch (_) {}
  }, 2500);
}

// Show progress on slow forms (adding from a post takes a while).
document.querySelectorAll("form[data-busy]").forEach((form) => {
  form.addEventListener("submit", () => {
    const btn = form.querySelector("button");
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="pulse"></span> Working…'; }
    document.querySelectorAll("form[data-busy] button").forEach((b) => (b.disabled = true));
  });
});

// Refresh the wishlist every minute so "searching…" and counts stay current.
if (location.pathname === "/" && !document.querySelector(".inbox")) {
  setInterval(() => { if (document.visibilityState === "visible") location.reload(); }, 60000);
}
