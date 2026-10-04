// The tempus app: concept tree + card view, hot-reloading over /api/events.
import { renderPanel } from "./adapters/echarts.js";

const theme = await (await fetch("/theme.json")).json();
const root = document.documentElement.style;
for (const [k, v] of Object.entries(theme.color)) if (typeof v === "string") root.setProperty(`--${k}`, v);
root.setProperty("--family", theme.font.family);
root.setProperty("--mono", theme.font.mono);
root.setProperty("--size", `${theme.font.size}px`);
root.setProperty("--space", `${theme.space}px`);
root.setProperty("--radius", `${theme.radius}px`);

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
let charts = [];
const current = () => decodeURIComponent(location.hash.replace(/^#\/cards\//, "")) || null;

async function loadTree() {
  const [cards, concepts] = await Promise.all([
    fetch("/api/cards").then((r) => r.json()),
    fetch("/api/concepts").then((r) => r.json()).catch(() => ({ tree: [], unfiled_cards: [] })),
  ]);
  const byId = Object.fromEntries(cards.map((c) => [c.id, c]));
  const link = (id) => {
    const c = byId[id];
    const cls = ["card", id === current() ? "active" : "", c?.errors ? "invalid" : ""].join(" ");
    return `<a class="${cls}" href="#/cards/${esc(id)}" title="${esc(c?.question)}">${esc(c?.title ?? id)}</a>`;
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

async function showCard(id) {
  charts.forEach((c) => c?.dispose());
  charts = [];
  const main = document.getElementById("main");
  if (!id) return;
  const res = await fetch(`/api/cards/${encodeURIComponent(id)}`);
  if (!res.ok) { main.innerHTML = `<p class="muted">No card '${esc(id)}'.</p>`; return; }
  const body = await res.json();
  const card = body.card;
  const errors = body.errors.filter((e) => e.level === "error");
  const warnings = body.errors.filter((e) => e.level === "warning");
  const fmtErr = (e) => `${esc(e.path)}\n  ${esc(e.message)}${e.hint ? " " + esc(e.hint) : ""}`;
  main.innerHTML = `
    <h2>${esc(card?.title ?? id)}</h2>
    ${card ? `<p class="question">${esc(card.question)}</p>` : ""}
    ${card?.notes ? `<div class="notes">${esc(card.notes)}</div>` : ""}
    ${errors.length ? `<div class="errors">${errors.map(fmtErr).join("\n\n")}</div>` : ""}
    ${warnings.map((w) => `<p class="warning">⚠ ${esc(w.path)}: ${esc(w.message)}</p>`).join("")}
    <div id="panels"></div>`;
  const panels = document.getElementById("panels");
  for (const p of body.payloads) {
    const wrap = document.createElement("section");
    wrap.className = "panel";
    const sub = p.annotations?.lead ? `<div class="sub">${esc(p.annotations.lead)} (r = ${p.annotations.peak.r.toFixed(3)})</div>` : "";
    wrap.innerHTML = `<h3>${esc(p.title ?? p.type)}</h3>${sub}<div class="${p.type === "table" || p.type === "stat" ? "" : "chart"}"></div>`;
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
