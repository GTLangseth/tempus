// The cards view: concept tree with the cards filed in it, and one card at a time.
// Hot-reloads over /api/events (wired in app.js). Layout follows docs/DESIGN.md.
import { fmt, renderPanel } from "../adapters/echarts.js";
import { keepCard } from "../api.js";
import { esc, FREQ } from "../util.js";

const LAG_SHORT = { D: "d", W: "wk", M: "mo", Q: "qtr", Y: "yr" };

// The card's single neon figure: the lag-correlation peak, else the first stat or series value.
export function headlineStats(payloads, theme) {
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
    const unit = s.units === "%" || s.units === "Percent" ? "%" : "";
    out.push({ neon: !lag && i === 0, dot: lag || i > 0 ? theme.color.series[i % 5] : null,
               value: `${fmt(last[1])}${unit}`, label: `${s.label} · ${last[0]}` });
  });
  return out;
}

// Lag panels don't carry a frequency; borrow it from the card's series for unit labels.
// Returns that frequency.
export function lendLagFreq(payloads) {
  const freq = payloads.flatMap((p) => p.series ?? []).find((s) => s.freq)?.freq;
  payloads.forEach((p) => { if (p.type === "lagcorr" && p.annotations) p.annotations.freq = freq; });
  return freq;
}

export function errorBlock(errors) {
  return errors.length ? `<div class="errors"><span class="label">${errors.length} error${errors.length > 1 ? "s" : ""}</span>\n${
    errors.map((e) => `${esc(e.path)}\n  ${esc(e.message)}${e.hint ? " " + esc(e.hint) : ""}`).join("\n\n")}</div>` : "";
}

// Keep a drafted card and open it. Refusals show above the view's `.actions` row; `busy`
// locks the view's controls meanwhile, and `stale` says the view moved on while it waited.
export async function keepAndOpen(root, card, busy, stale) {
  busy(true, "Keeping…");
  const kept = await keepCard(card);
  if (stale()) return;
  if (kept.id) { location.hash = `#/cards/${encodeURIComponent(kept.id)}`; return; }
  busy(false, kept.message);
  root.querySelector(".keep-errors")?.remove();
  if (kept.errors.length) root.querySelector(".actions").insertAdjacentHTML("beforebegin",
    `<div class="keep-errors">${errorBlock(kept.errors)}</div>`);
}

export function statRow(stats) {
  return stats.length ? `<div class="stats">${stats.map((s) => `<div class="stat"><b class="${s.neon ? "neon" : ""}">${
    s.dot ? `<span class="dot" style="background:${s.dot}"></span>` : ""}${esc(s.value)}</b><span class="label">${esc(s.label)}</span></div>`).join("")}</div>` : "";
}

export function chips(series, theme) {
  return series.map((s, i) => `<span class="chip"><span class="dot"
    style="background:${theme.color.series[i % theme.color.series.length]}"></span>${esc(s.label)}</span>`).join("");
}

// Draw a list of panel payloads into `holder`; returns the chart instances to dispose later.
export function drawPanels(holder, payloads, theme) {
  const charts = [];
  holder.style.display = "grid";
  holder.style.gap = "16px";
  for (const p of payloads) {
    const wrap = document.createElement("section");
    wrap.className = `panel${p.type === "stat" ? " stat-panel" : ""}`;
    const lead = p.annotations?.lead ? `<span class="label">${esc(p.annotations.lead)}</span>` : "";
    const legend = p.type === "timeseries" ? `<div class="foot">${chips(p.series, theme)}</div>` : "";
    const cls = p.type === "table" || p.type === "stat" ? "stat-body" : "chart";
    wrap.innerHTML = `<div class="panel-head"><h3>${esc(p.title ?? p.type)}</h3>${lead}</div>
      ${legend}<div class="${cls}"></div>`;
    holder.appendChild(wrap);
    charts.push(renderPanel(wrap.lastElementChild, p, theme));
  }
  return charts;
}

export class CardsView {
  constructor(root, theme) {
    this.root = root;
    this.theme = theme;
    this.charts = [];
    this.conceptNames = {};
  }

  async mount(route) {
    this.root.innerHTML = `<div class="cards-layout">
      <aside id="sidebar"><div class="label">concepts · cards</div><nav id="tree"></nav></aside>
      <main id="main"><p class="label">Pick a card.</p></main></div>`;
    await this.update(route);
  }

  unmount() { this.charts.forEach((c) => c?.dispose()); }

  async update(route) {
    this.current = route.card ?? null;
    await this.loadTree();
    await this.showCard(this.current);
  }

  async onEvent(card) {
    await this.loadTree();
    if (card === this.current) await this.showCard(card);
  }

  async loadTree() {
    const [cards, concepts] = await Promise.all([
      fetch("/api/cards").then((r) => r.json()),
      fetch("/api/concepts").then((r) => r.json()).catch(() => ({ tree: [], unfiled_cards: [] })),
    ]);
    const byId = Object.fromEntries(cards.map((c) => [c.id, c]));
    this.conceptNames = {};
    const walk = (n, path) => {
      this.conceptNames[n.id] = [...path, n.name].join(" / ");
      n.children.forEach((c) => walk(c, [...path, n.name]));
    };
    concepts.tree.forEach((n) => walk(n, []));

    const link = (id) => {
      const c = byId[id];
      const cls = ["card", id === this.current ? "active" : "", c?.errors ? "invalid" : ""].join(" ");
      return `<a class="${cls}" href="#/cards/${esc(id)}" title="${esc(c?.question)}"><span>${esc(c?.title ?? id)}</span></a>`;
    };
    // Only branches that hold cards; series live in the explore view.
    const hasCards = (n) => n.cards.length > 0 || n.children.some(hasCards);
    const node = (n) => `<details open>
      <summary>${esc(n.name)}<span class="count">${n.cards.length || ""}</span></summary>
      ${n.cards.map(link).join("")}${n.children.filter(hasCards).map(node).join("")}</details>`;
    const unfiled = concepts.unfiled_cards.length
      ? `<details open><summary>Unfiled</summary>${concepts.unfiled_cards.map(link).join("")}</details>` : "";
    const tree = concepts.tree.filter(hasCards).map(node).join("") + unfiled;
    this.root.querySelector("#tree").innerHTML = tree || `<p class="label">No cards yet. <code>tempus card new my-card</code></p>`;
  }

  async showCard(id) {
    this.charts.forEach((c) => c?.dispose());
    this.charts = [];
    const main = this.root.querySelector("#main");
    if (!id) { main.innerHTML = `<p class="label">Pick a card.</p>`; return; }
    const res = await fetch(`/api/cards/${encodeURIComponent(id)}`);
    if (!res.ok) { main.innerHTML = `<p class="label">No card '${esc(id)}'.</p>`; return; }
    const body = await res.json();
    const card = body.card;
    document.title = `${card?.title ?? id} · tempus`;

    const freq = lendLagFreq(body.payloads);
    const errors = body.errors.filter((e) => e.level === "error");
    const warnings = body.errors.filter((e) => e.level === "warning");
    const eyebrow = [
      card?.concepts?.[0] ? (this.conceptNames[card.concepts[0]] ?? card.concepts[0]) : "unfiled",
      FREQ[freq],
    ].filter(Boolean).join(" · ");
    const stats = headlineStats(body.payloads, this.theme);
    const sources = [...new Set(Object.values(card?.inputs ?? {}).map((i) => i.series?.split(":")[0]).filter(Boolean))];

    main.innerHTML = `
      <div><div class="label">card · ${esc(id)}</div><h1 class="page-title">${esc(card?.title ?? id)}</h1></div>
      <article class="sticker">
        <div class="label">${esc(eyebrow)}</div>
        ${card ? `<h2 class="question">${esc(card.question)}</h2>` : ""}
        ${errorBlock(errors)}
        ${statRow(stats)}
        <div id="panels"></div>
        ${card?.notes ? `<p class="notes">${esc(card.notes.trim())}</p>` : ""}
        ${warnings.map((w) => `<p class="warning"><b>NOTE</b> ${esc(w.message)}</p>`).join("")}
        <div class="foot">
          ${(card?.concepts ?? []).map((c) => `<a class="chip" href="#/explore/${esc(c)}">${esc(this.conceptNames[c] ?? c)}</a>`).join("")}
          ${(card?.tags ?? []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
          ${sources.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}
        </div>
      </article>`;
    this.charts = drawPanels(main.querySelector("#panels"), body.payloads, this.theme);
  }
}
