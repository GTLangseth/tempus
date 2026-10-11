// One series: an implicit single-series card (stat + history) built by the engine, plus its
// facts, where it sits in the tree and why, and its neighbors. Sticker-card layout.
import { fmt } from "../adapters/echarts.js";
import { compareToggle } from "../compare.js";
import { esc, fmtInt, FREQ } from "../util.js";
import { drawPanels } from "./cards.js";
import { seriesList } from "./explore.js";

const METHOD = {
  manual: "Pinned by hand in catalog.toml.",
  auto: "Filed by Jev, walking the tree one level at a time.",
  draft: "Draft placement written by Claude for a first look. <code>tempus ontology file</code> lets Jev decide it.",
};

export class SeriesView {
  constructor(root, theme) {
    this.root = root;
    this.theme = theme;
    this.charts = [];
  }

  async mount(route) { await this.update(route); }

  unmount() { this.charts.forEach((c) => c?.dispose()); }

  async update({ source, id }) {
    this.unmount();
    this.root.innerHTML = `<main class="feed"><p class="label">loading ${esc(source)}:${esc(id)}…</p></main>`;
    const res = await fetch(`/api/series/${encodeURIComponent(source)}/${encodeURIComponent(id)}`);
    const main = this.root.querySelector("main");
    if (!res.ok) {
      main.innerHTML = `<p class="label">No series ${esc(source)}:${esc(id)} in the database.</p>
        <p><a class="btn" href="#/explore">Back to the tree</a></p>`;
      return;
    }
    const page = await res.json();
    const s = page.series;
    const place = page.placement;
    const path = place?.path ?? [];
    document.title = `${s.id} · tempus`;
    const stat = page.payloads.find((p) => p.type === "stat")?.stat;
    const history = page.payloads.filter((p) => p.type !== "stat");
    const pct = s.units === "Percent" || s.units === "%";
    const eyebrow = [path.map((c) => c.name).join(" / ") || "unfiled", FREQ[s.freq] ?? s.frequency,
                     s.seasonal_adjustment].filter(Boolean).join(" · ");
    const change = stat?.change == null ? "" :
      ` · ${stat.change >= 0 ? "↑" : "↓"} ${fmt(Math.abs(stat.change))}${stat.change_kind === "yoy" ? "% yoy" : " vs prior"}`;
    const range = s.observation_start || s.observation_end
      ? `${s.observation_start ?? "?"} → ${s.observation_end ?? "?"}` : "—";

    main.innerHTML = `
      <nav class="crumbs label"><a href="#/explore">tree</a>${path.map((c) =>
        ` / <a href="#/explore/${esc(c.id)}">${esc(c.name)}</a>`).join("")} / <b>${esc(s.id)}</b></nav>
      <article class="sticker">
        <div class="label">${esc(eyebrow)}</div>
        <h1 class="question">${esc(s.title ?? s.id)}</h1>
        <div class="foot">${compareToggle(`${s.source}:${s.id}`, s.title ?? s.id, "btn ghost")}</div>
        ${stat ? `<div class="stats"><div class="stat"><b class="neon">${fmt(stat.value)}${pct ? "%" : ""}</b>
          <span class="label">${esc(s.units ?? "")} · ${esc(stat.date)}${esc(change)}</span></div></div>` : ""}
        ${page.synced ? "" : `
          <div class="unsynced">
            <span class="label"><mark>not synced yet</mark></span>
            <p>Only the metadata is here; this series came from the FRED snapshot.
              ${page.sync?.status === "failed" ? `Syncing on open failed: <code>${esc(page.sync.error)}</code>.` :
                page.read_only ? "This app is read-only, so it won't sync on open. Sync it on the home base:" :
                "Add a <code>FRED_API_KEY</code> to <code>.env</code> to sync on open, or run:"}</p>
            <div class="cmd"><code>${esc(page.hint)}</code><button class="btn ghost copy" type="button">Copy</button></div>
          </div>`}
        <div id="panels"></div>
        <dl class="facts">
          <div><dt class="label">id</dt><dd><code>${esc(s.source)}:${esc(s.id)}</code></dd></div>
          <div><dt class="label">units</dt><dd>${esc(s.units ?? "—")}</dd></div>
          <div><dt class="label">frequency</dt><dd>${esc(s.frequency ?? "—")}</dd></div>
          <div><dt class="label">adjustment</dt><dd>${esc(s.seasonal_adjustment ?? "—")}</dd></div>
          <div><dt class="label">coverage</dt><dd>${esc(range)}</dd></div>
          <div><dt class="label">points stored</dt><dd>${fmtInt(s.observations)}</dd></div>
          <div><dt class="label">popularity</dt><dd>${s.popularity ?? "—"}</dd></div>
          <div><dt class="label">synced</dt><dd>${s.last_synced_at ? esc(s.last_synced_at.slice(0, 16).replace("T", " ")) : "never"}</dd></div>
        </dl>
        ${s.notes ? `<details class="notes-box"><summary class="label">notes from the source</summary><p class="notes">${esc(s.notes)}</p></details>` : ""}
        <div class="foot">
          ${page.links.source ? `<a class="chip" href="${esc(page.links.source)}" target="_blank" rel="noopener">fred ↗</a>` : ""}
          ${path.length ? `<a class="chip" href="#/explore/${esc(path.at(-1).id)}">show in tree</a>` : ""}
        </div>
      </article>
      ${placementCard(place)}
      ${page.neighbors.length ? `<article class="sticker">
        <div class="label">also in ${esc(path.at(-1)?.name ?? "this concept")}</div>
        ${seriesList(page.neighbors, this.theme)}</article>` : ""}`;

    main.querySelector(".copy")?.addEventListener("click", async (e) => {
      try { await navigator.clipboard.writeText(page.hint); e.target.textContent = "Copied"; } catch { e.target.textContent = "Select and copy"; }
    });
    for (const e of page.errors.filter((e) => e.level === "error"))
      main.querySelector("#panels").insertAdjacentHTML("beforebegin", `<p class="warning"><b>ERROR</b> ${esc(e.message)}</p>`);
    this.charts = drawPanels(main.querySelector("#panels"), history, this.theme);
  }
}

function placementCard(place) {
  if (!place) {
    return `<article class="sticker"><div class="label">placement</div>
      <p class="desc">Not in the tree yet. Run <code>tempus ontology file</code> to let Jev place it.</p></article>`;
  }
  const low = place.trail?.find?.((t) => t.low)?.low;
  const steps = place.method === "auto" ? place.trail : [];
  return `<article class="sticker">
    <div class="label">placement · <span class="method">${esc(place.method)}</span>${
      place.confidence != null ? ` · confidence ${place.confidence.toFixed(2)}` : ""}</div>
    <p class="desc">${METHOD[place.method] ?? ""}${low ? ` Flagged as a judgment call: ${esc(low)}.` : ""}</p>
    ${steps.length ? `<ol class="routes">${steps.map((t) => `<li class="route"><span class="label">at ${esc(t.at ?? "the top")}</span>
      ${t.top.map(([k, p], i) => `<span class="step${i === 0 ? " chosen" : ""}"><span>${esc(k)}</span><i>${p.toFixed(2)}</i></span>`).join("")}</li>`).join("")}</ol>` : ""}
  </article>`;
}
