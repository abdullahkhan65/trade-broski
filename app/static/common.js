// Shared helpers for all pages.
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
};

const usd = (v, sign = false) =>
  v == null ? "–" : (sign && v > 0 ? "+" : "") + (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
const pct = (v, d = 1) => (v == null ? "–" : v.toFixed(d) + "%");
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const fmtPx = (v, d) => (v == null ? "–" : Number(v).toFixed(d));
const ago = (iso) => {
  if (!iso) return "–";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
};
const time = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg) {
  let t = document.querySelector(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), 3000);
}

function nav(active) {
  const links = [["/", "Terminal"], ["/floor", "Floor"], ["/news", "News & events"], ["/settings", "Settings"]];
  document.body.insertAdjacentHTML("afterbegin",
    `<nav><span class="brand">📈 AI Paper Trader</span>${links
      .map(([h, t]) => `<a href="${h}" class="${h === active ? "active" : ""}">${t}</a>`).join("")}
      <span class="right" id="clock"></span></nav>`);
  setInterval(() => {
    const d = new Date();
    document.getElementById("clock").textContent =
      d.toUTCString().slice(17, 25) + " UTC · " + d.toLocaleTimeString([], { timeZone: "Asia/Karachi", hour: "2-digit", minute: "2-digit" }) + " PKT";
  }, 1000);
}
