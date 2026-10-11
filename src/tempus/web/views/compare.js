// The compare view (#/compare/<ref>,<ref>,…): several hand-picked series on one chart, a
// switch between ways of putting them on one scale, the correlation of their year-over-year
// changes, and Keep. "Compare several series" in
// docs/specs/2026-10-05-question-to-card-design.md. Layout follows docs/DESIGN.md.
import { failure, post } from "../api.js";
import { compareHash, refLabel } from "../compare.js";
import { esc } from "../util.js";
import { drawPanels, errorBlock, keepAndOpen } from "./cards.js";

const MODE_WORDS = { levels: "Levels", rebased: "Rebased to 100", yoy: "Year-over-year", zscore: "Standard deviations" };
const modeWords = (m) => MODE_WORDS[m] ?? m;
const month = (d) => (d ? d.slice(0, 7) : "?");

function signed(r) {
  const s = Math.abs(r).toFixed(2);
  return r > 0 ? `+${s}` : r < 0 ? `−${s}` : s;
}

// Tinted lightly enough that ink stays readable; the printed number carries the meaning.
function tint(r) {
  const hue = r >= 0 ? "--neon-violet" : "--neon-orange";
  return `background: color-mix(in srgb, var(${hue}) ${Math.round(Math.abs(r) * 40)}%, var(--paper))`;
}

function matrixTable(corr, series, theme) {
  const at = corr.refs.map((ref) => series.findIndex((s) => s.series === ref));
  const titles = at.map((i, k) => series[i]?.title ?? corr.refs[k]);
  const color = (k) => theme.color.series[Math.max(at[k], 0) % theme.color.series.length];
  const head = (i, n) => `<span class="dot" style="background:${color(i)}"></span>${refLabel(corr.refs[i], titles[i], n)}`;
  const hasNull = corr.matrix.some((row) => row.some((v) => v == null));
  const span = corr.months ? `, ${corr.months} months (${month(corr.start)} – ${month(corr.end)})` : "";
  return `<section class="corr">
    <div class="panel-head"><h3>How they move together</h3><span class="label">correlation</span></div>
    <div class="corr-scroll"><table>
      <thead><tr><td></td>${corr.refs.map((ref, j) => `<th scope="col" title="${esc(titles[j])}">${head(j, 0)}</th>`).join("")}</tr></thead>
      <tbody>${corr.matrix.map((row, i) => `<tr><th scope="row" title="${esc(titles[i])}">${head(i, 26)}</th>${row.map((v, j) =>
        i === j ? `<td class="diag">1.00</td>`
        : v == null ? `<td class="none" title="too little overlap">—</td>`
        : `<td style="${tint(v)}">${signed(v)}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>
    <p class="label">Year-over-year changes, monthly${esc(span)}</p>
    ${hasNull ? `<p class="note">— too little overlap to measure.</p>` : ""}
  </section>`;
}

export class CompareView {
  constructor(root, theme) {
    this.root = root;
    this.theme = theme;
    this.charts = [];
    this.seq = 0; // bumps on every request; late responses are dropped
    this.mode = null;
  }

  async mount(route) { await this.update(route); }

  unmount() {
    this.seq++;
    this.disposeCharts();
  }

  disposeCharts() {
    this.charts.forEach((c) => c?.dispose());
    this.charts = [];
  }

  async update({ refs }) {
    this.refs = refs;
    document.title = "Compare · tempus";
    // Levels only make sense while units match, so a new set of series starts from the default.
    if (this.mode === "levels") this.mode = null;
    if (!this.result) {
      this.root.innerHTML = `<main class="feed compare"><div class="label">compare · ${refs.length} series</div>
        <article class="sticker" aria-busy="true"><p class="desc" role="status">Lining the series up…</p></article></main>`;
    }
    await this.load();
  }

  async load() {
    const seq = ++this.seq;
    this.busy(true, "Lining the series up…");
    const { status, body } = await post("/api/compare", { series: this.refs, mode: this.mode });
    if (seq !== this.seq) return;
    if (status !== 200 || !body) return this.renderFailure(status, body);
    this.mode = body.mode;
    this.render(body);
  }

  busy(on, message = "") {
    this.root.querySelectorAll(".modes button, .series-chips button").forEach((b) => { b.disabled = on; });
    const keep = this.root.querySelector(".keep");
    if (keep) keep.disabled = on || !this.result?.card;
    const status = this.root.querySelector(".status");
    if (status) status.textContent = message;
    if (!on) this.lockChips();
  }

  lockChips() {
    const last = this.result?.series.length <= 2;
    this.root.querySelectorAll(".series-chips button").forEach((b) => {
      b.disabled = last;
      b.title = last ? "Compare needs at least 2 series" : "";
    });
  }

  renderFailure(status, body) {
    this.result = null;
    this.disposeCharts();
    const detail = typeof body?.detail === "string" ? body.detail : null;
    // A validation 422 carries a list in `detail`; the compare checks send one sentence.
    const message = status !== 422 ? failure(status, "lining these series up")
      : detail ? `These series can't be compared: ${detail}.` : "Compare takes 2 to 6 different series that are in the catalog.";
    this.root.innerHTML = `<main class="feed compare">
      <div class="label">compare · ${this.refs.length} series</div>
      <article class="sticker">
        <div class="label">couldn't compare</div>
        <p class="desc">${esc(message)}</p>
        <div class="foot">${status === 422 ? "" : `<button class="btn retry" type="button">Try again</button>`}
          <a class="btn ghost" href="#/explore">Back to explore</a></div>
      </article></main>`;
    this.root.querySelector(".retry")?.addEventListener("click", () => this.load());
  }

  render(result) {
    this.result = result;
    this.disposeCharts();
    const { card, series, correlation } = result;
    const titles = series.map((s) => s.title ?? s.series);
    const errors = result.errors.filter((e) => e.level === "error");
    const warnings = result.errors.filter((e) => e.level === "warning");
    const color = (i) => this.theme.color.series[i % this.theme.color.series.length];

    this.root.innerHTML = `<main class="feed compare">
      <div><div class="label">compare · not kept yet</div>
        <h2 class="page-title">${esc(card?.title ?? "Compare")}</h2></div>
      <article class="sticker">
        <div class="label">${series.length} series · ${esc(modeWords(result.mode))}</div>
        <h1 class="question">${esc(card?.question ?? `How do these ${series.length} series move together?`)}</h1>
        <div class="foot series-chips">${series.map((s, i) => `<span class="chip" title="${esc(titles[i])}">
          <span class="dot" style="background:${color(i)}"></span><span>${refLabel(s.series, titles[i], 26)}</span>
          <button type="button" data-ref="${esc(s.series)}" aria-label="Remove ${esc(titles[i])}">×</button></span>`).join("")}</div>
        ${result.modes.length > 1 ? `<div class="switch modes" role="group" aria-label="Scale">${result.modes.map((m) =>
          `<button type="button" data-mode="${esc(m)}" aria-pressed="${m === result.mode}">${esc(modeWords(m))}</button>`).join("")}</div>` : ""}
        ${errorBlock(errors)}
        <div id="panels"></div>
        ${warnings.map((w) => `<p class="warning"><b>NOTE</b> ${esc(w.message)}</p>`).join("")}
        ${correlation?.refs?.length ? matrixTable(correlation, series, this.theme) : ""}
        ${result.yaml ? `<details class="yaml-box"><summary class="label">the card as YAML</summary><pre class="yaml">${esc(result.yaml)}</pre></details>` : ""}
        <div class="foot actions">
          <button class="btn keep" type="button"${card ? "" : " disabled"}>Keep</button>
          <a class="btn ghost" href="#/explore">Back to explore</a>
          <span class="desc status" role="status"></span>
        </div>
      </article></main>`;
    this.root.querySelectorAll(".modes button").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.mode === this.mode) return;
      this.mode = b.dataset.mode;
      this.load();
    }));
    this.root.querySelector(".series-chips").addEventListener("click", (e) => {
      const ref = e.target.closest("button")?.dataset.ref;
      if (ref && this.refs.length > 2) location.hash = compareHash(this.refs.filter((r) => r !== ref));
    });
    this.root.querySelector(".keep").addEventListener("click", () => this.keep());
    this.lockChips();
    this.charts = drawPanels(this.root.querySelector("#panels"), result.payloads, this.theme);
  }

  async keep() {
    const seq = this.seq;
    await keepAndOpen(this.root, this.result.card, (on, message) => this.busy(on, message), () => seq !== this.seq);
  }
}
