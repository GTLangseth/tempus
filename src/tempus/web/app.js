// The tempus app: concept tree + card view, hot-reloading over /api/events.
// Layout and tokens follow docs/DESIGN.md (Neon Notebook).
import { fmt, renderPanel } from "./adapters/echarts.js";

const theme = await (await fetch("/theme.json")).json();
const root = document.documentElement.style;
for (const [k, v] of Object.entries(theme.color)) if (typeof v === "string") root.setProperty(`--${k}`, v);
for (const [k, v] of Object.entries(theme.font)) root.setProperty(`--font-${k}`, v);
for (const [k, v] of Object.entries(theme.rounded)) root.setProperty(`--rounded-${k}`, `${v}px`);
root.setProperty("--grid-cell", `${theme.grid}px`);
await document.fonts.ready; // so charts measure text in the real fonts

const FREQ = { D: "daily", W: "weekly", M: "monthly", Q: "quarterly", Y: "annual" };
const LAG_SHORT = { D: "d", W: "wk", M: "mo", Q: "qtr", Y: "yr" };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const current = () => decodeURIComponent(location.hash.replace(/^#\/cards\//, "")) || null;
let charts = [];
let conceptNames = {}; // concept id → "Prices / Consumer Prices / Energy"

async function loadTree() {
  const [cards, concepts] = await Promise.all([
    fetch("/api/cards").then((r) => r.json()),
    fetch("/api/concepts").then((r) => r.json()).catch(() => ({ tree: [], unfiled_cards: [] })),
  ]);
  const byId = Object.fromEntries(cards.map((c) => [c.id, c]));
  const walk = (n, path) => {
    conceptNames[n.id] = [...path, n.name].join(" / ");
    n.children.forEach((c) => walk(c, [...path, n.name]));
  };
  conceptNames = {};
  concepts.tree.forEach((n) => walk(n, []));

  const link = (id) => {
    const c = byId[id];
    const cls = ["card", id === current() ? "active" : "", c?.errors ? "invalid" : ""].join(" ");
    return `<a class="${cls}" href="#/cards/${esc(id)}" title="${esc(c?.question)}"><span>${esc(c?.title ?? id)}</span></a>`;
  };
  const hasCards = (n) => n.cards.length > 0 || n.children.some(hasCards);
  const node = (n) => {
    const count = n.series + n.cards.length;
    return `<details ${hasCards(n) ? "open" : ""}>
      <summary>${esc(n.name)}<span class="count">${count || ""}</span></summary>
      ${n.cards.map(link).join("")}${n.children.map(node).join("")}</details>`;
  };
  const unfiled = concepts.unfiled_cards.length
    ? `<details open><summary>Unfiled</summary>${concepts.unfiled_cards.map(link).join("")}</details>` : "";
  document.getElementById("tree").innerHTML = concepts.tree.map(node).join("") + unfiled;
}

// The card's single neon figure: the lag-correlation peak, else the first stat or series value.
function headlineStats(payloads) {
  const out = [];
  const lag = payloads.find((p) => p.type === "lagcorr" && p.annotations?.peak);
  const series = payloads.filter((p) => p.type === "timeseries").flatMap((p) => p.series);
  if (lag) {
    const { lag: k, r } = lag.annotations.peak;
    out.push({ neon: true, value: `${k} ${LAG_SHORT[lag.annotations.freq] ?? "per"}`,
               label: `peak lag · r = ${r.toFixed(3)}` });
  }
  series.slice(0, lag ? 2 : 3).forEach((s, i) => {
    const last = [...s.points].reverse().find((pt) => pt[1] != null);
    if (!last) return;
    const unit = s.units === "%" ? "%" : "";
    out.push({ neon: !lag && i === 0, dot: lag || i > 0 ? theme.color.series[i % 5] : null,
               value: `${fmt(last[1])}${unit}`, label: `${s.label} · ${last[0]}` });
  });
  return out;
}

function chips(series) {
  return series.map((s, i) => `<span class="chip"><span class="dot"
    style="background:${theme.color.series[i % theme.color.series.length]}"></span>${esc(s.label)}</span>`).join("");
}

async function showCard(id) {
  charts.forEach((c) => c?.dispose());
  charts = [];
  const main = document.getElementById("main");
  if (!id) return;
  const res = await fetch(`/api/cards/${encodeURIComponent(id)}`);
  if (!res.ok) { main.innerHTML = `<p class="label">No card '${esc(id)}'.</p>`; return; }
  const body = await res.json();
  const card = body.card;
  document.title = `${card?.title ?? id} · tempus`;

  // Lag panels don't carry a frequency; borrow it from the card's series for unit labels.
  const freq = body.payloads.flatMap((p) => p.series ?? []).find((s) => s.freq)?.freq;
  body.payloads.forEach((p) => { if (p.type === "lagcorr" && p.annotations) p.annotations.freq = freq; });

  const errors = body.errors.filter((e) => e.level === "error");
  const warnings = body.errors.filter((e) => e.level === "warning");
  const eyebrow = [
    card?.concepts?.[0] ? (conceptNames[card.concepts[0]] ?? card.concepts[0]) : "unfiled",
    FREQ[freq],
  ].filter(Boolean).join(" · ");
  const stats = headlineStats(body.payloads);
  const sources = [...new Set(Object.values(card?.inputs ?? {}).map((i) => i.series?.split(":")[0]).filter(Boolean))];

  main.innerHTML = `
    <div><div class="label">card · ${esc(id)}</div><h1 class="page-title">${esc(card?.title ?? id)}</h1></div>
    <article class="sticker">
      <div class="label">${esc(eyebrow)}</div>
      ${card ? `<h2 class="question">${esc(card.question)}</h2>` : ""}
      ${errors.length ? `<div class="errors"><span class="label">${errors.length} error${errors.length > 1 ? "s" : ""}</span>\n${
        errors.map((e) => `${esc(e.path)}\n  ${esc(e.message)}${e.hint ? " " + esc(e.hint) : ""}`).join("\n\n")}</div>` : ""}
      ${stats.length ? `<div class="stats">${stats.map((s) => `<div class="stat"><b class="${s.neon ? "neon" : ""}">${
        s.dot ? `<span class="dot" style="background:${s.dot}"></span>` : ""}${esc(s.value)}</b><span class="label">${esc(s.label)}</span></div>`).join("")}</div>` : ""}
      <div id="panels"></div>
      ${card?.notes ? `<p class="notes">${esc(card.notes.trim())}</p>` : ""}
      ${warnings.map((w) => `<p class="warning"><b>NOTE</b> ${esc(w.message)}</p>`).join("")}
      <div class="foot">
        ${(card?.concepts ?? []).map((c) => `<span class="chip">${esc(conceptNames[c] ?? c)}</span>`).join("")}
        ${(card?.tags ?? []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
        ${sources.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}
      </div>
    </article>`;

  const panels = document.getElementById("panels");
  panels.style.display = "grid";
  panels.style.gap = "16px";
  for (const p of body.payloads) {
    const wrap = document.createElement("section");
    wrap.className = `panel${p.type === "stat" ? " stat-panel" : ""}`;
    const lead = p.annotations?.lead ? `<span class="label">${esc(p.annotations.lead)}</span>` : "";
    const legend = p.type === "timeseries" ? `<div class="foot">${chips(p.series)}</div>` : "";
    const holder = p.type === "table" || p.type === "stat" ? "stat-body" : "chart";
    wrap.innerHTML = `<div class="panel-head"><h3>${esc(p.title ?? p.type)}</h3>${lead}</div>
      ${legend}<div class="${holder}"></div>`;
    panels.appendChild(wrap);
    charts.push(renderPanel(wrap.lastElementChild, p, theme));
  }
}

async function refresh() { await loadTree(); await showCard(current()); }

window.addEventListener("hashchange", refresh);
const events = new EventSource("/api/events");
for (const kind of ["card-changed", "card-invalid", "card-removed"]) {
  events.addEventListener(kind, (e) => {
    const { card } = JSON.parse(e.data);
    loadTree();
    if (card === current()) showCard(card);
  });
}
refresh();
