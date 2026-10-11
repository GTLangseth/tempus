// The explore view: the concept tree in 3D (or an outline on small screens), with a search
// box that walks the tree (/api/navigate) and flies the camera along the route it finds.
import { ancestors, layout, subtree } from "../explore/layout.js";
import { esc, fmtInt, FREQ } from "../util.js";

const FREQ_KEYS = ["D", "W", "M", "Q", "Y"];
const SERIES_SHOWN = 40;
const EXAMPLES = ["crude oil", "mortgage rates", "jobs report", "home prices in san francisco", "yield curve", "bitcoin"];

let cache = null; // explore payload + layout, fetched once per page load

export async function exploreData(force = false) {
  if (!cache || force) {
    const body = await fetch("/api/explore").then((r) => r.json());
    const byId = new Map(body.concepts.map((c) => [c.id, c]));
    cache = { ...body, byId, lay: layout(body.concepts, body.series) };
  }
  return cache;
}

export function preferredMode() {
  try {
    const saved = localStorage.getItem("tempus.mode");
    if (saved) return saved;
  } catch { /* storage may be blocked */ }
  return window.innerWidth < 760 ? "outline" : "3d";
}

export class ExploreView {
  constructor(root, theme) {
    this.root = root;
    this.theme = theme;
    this.focus = null;
    this.result = null;
    this.mode = preferredMode();
  }

  async mount(route) {
    this.data = await exploreData();
    this.meta = new Map(this.data.series.map((s) => [`${s.source}:${s.id}`, s]));
    this.root.innerHTML = `
      <section class="explore">
        <div class="stage" aria-label="3D concept tree"></div>
        <div class="labels" aria-hidden="true"></div>
        <form class="ask sticker" role="search">
          <span class="label">ask</span>
          <input name="q" autocomplete="off" spellcheck="false"
                 placeholder="Describe a series: “crude oil”, “mortgage rates”, “jobs report”">
          <button class="btn" type="submit">Find</button>
        </form>
        <aside class="side sticker"></aside>
        <div class="corner">
          <div class="switch" role="group" aria-label="View">
            <button data-mode="3d">3D</button><button data-mode="outline">Outline</button>
          </div>
          <div class="legend"></div>
        </div>
        <div class="hint label">Drag to orbit · Scroll to zoom · Click a bead to focus · <kbd>/</kbd> to ask · <kbd>Esc</kbd> to zoom out</div>
        <div class="tip" hidden></div>
      </section>`;
    this.el = this.root.querySelector(".explore");
    this.stage = this.el.querySelector(".stage");
    this.panel = this.el.querySelector(".side");
    this.labelsEl = this.el.querySelector(".labels");
    this.tip = this.el.querySelector(".tip");
    this.input = this.el.querySelector("input");
    this.el.querySelector(".ask").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = this.input.value.trim();
      if (q) location.hash = `#/ask/${encodeURIComponent(q)}`;
    });
    this.el.querySelectorAll(".switch button").forEach((b) => b.addEventListener("click", () => this.setMode(b.dataset.mode)));
    this.keys = (e) => {
      if (e.key === "/" && document.activeElement !== this.input) { e.preventDefault(); this.input.focus(); }
      if (e.key === "Escape") {
        if (document.activeElement === this.input) this.input.blur();
        location.hash = this.focus && this.data.byId.get(this.focus)?.parent_id
          ? `#/explore/${this.data.byId.get(this.focus).parent_id}` : "#/explore";
      }
    };
    window.addEventListener("keydown", this.keys);
    this.renderLegend();
    await this.setMode(this.mode, { save: false });
    await this.update(route);
  }

  unmount() {
    window.removeEventListener("keydown", this.keys);
    this.scene?.dispose();
    this.scene = null;
  }

  async setMode(mode, { save = true } = {}) {
    const { webglAvailable } = await import("../explore/scene.js").catch(() => ({ webglAvailable: () => false }));
    if (mode === "3d" && !webglAvailable()) mode = "outline";
    this.mode = mode;
    if (save) try { localStorage.setItem("tempus.mode", mode); } catch { /* ignore */ }
    this.el.dataset.mode = mode;
    this.el.querySelectorAll(".switch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    this.scene?.dispose();
    this.scene = null;
    this.stage.innerHTML = "";
    this.labelsEl.innerHTML = "";
    if (mode === "3d") {
      const { TreeScene } = await import("../explore/scene.js");
      this.scene = new TreeScene(this.stage, this.data.lay, this.theme, {
        hover: (item, x, y) => this.hover(item, x, y),
        click: (item) => this.click(item),
        frame: () => this.placeLabels(),
      });
      this.scene.setShift(window.innerWidth > 760 ? 200 : 0);
      if (this.result) this.scene.setTrail(this.trailIds(), this.matchRefs());
      if (this.focus) { this.scene.setFocus(this.focus, { fly: false }); this.scene.flyTo(this.focus, 1); }
      this.refreshLabels();
    } else {
      this.renderOutline();
    }
  }

  // Routes: #/explore, #/explore/<concept>, #/ask/<query>
  async update(route) {
    if (route.view === "ask") {
      this.input.value = route.query;
      await this.ask(route.query);
      return;
    }
    this.result = null;
    this.scene?.setTrail([], []);
    this.setFocusConcept(route.concept && this.data.byId.has(route.concept) ? route.concept : null);
  }

  setFocusConcept(id) {
    this.focus = id;
    this.scene?.setFocus(id);
    this.refreshLabels();
    this.renderPanel();
    if (this.mode === "outline") this.renderOutline();
  }

  async ask(query) {
    this.panel.innerHTML = `<div class="label">walking the tree…</div><h2 class="panel-title">${esc(query)}</h2>`;
    const res = await fetch(`/api/navigate?q=${encodeURIComponent(query)}`);
    if (!res.ok) { this.panel.innerHTML = `<p class="label">Couldn't search: ${res.status}</p>`; return; }
    this.result = await res.json();
    const best = this.result.paths[0];
    this.focus = best ? best.concepts.at(-1) : null;
    if (this.scene) {
      this.scene.setTrail(this.trailIds(), this.matchRefs());
      this.scene.setFocus(this.focus, { fly: false });
      this.refreshLabels();
      this.renderPanel();
      await this.scene.flyAlong(best ? best.concepts : [], 700);
    } else {
      this.renderPanel();
      this.renderOutline();
    }
  }

  trailIds() {
    return [...new Set((this.result?.paths ?? []).flatMap((p) => p.concepts))];
  }

  matchRefs() {
    return (this.result?.series ?? []).slice(0, 12).map((s) => `${s.source}:${s.series_id}`);
  }

  // --- 3D interaction ------------------------------------------------------------------

  hover(item, x, y) {
    this.stage.style.cursor = item ? "pointer" : "";
    if (!item) { this.tip.hidden = true; return; }
    const rect = this.el.getBoundingClientRect();
    if (item.kind === "concept") {
      this.tip.innerHTML = `<b>${esc(item.node.name)}</b><span>${fmtInt(item.node.total)} series · ${esc(item.node.id)}</span>`;
    } else {
      const s = item.leaf.series;
      this.tip.innerHTML = `<b>${esc(s.title)}</b><span>${esc(s.id)} · ${FREQ[s.freq] ?? "irregular"}${s.synced ? " · synced" : ""}</span>`;
    }
    this.tip.hidden = false;
    this.tip.style.left = `${Math.min(x - rect.left + 14, rect.width - 340)}px`;
    this.tip.style.top = `${y - rect.top + 14}px`;
  }

  click(item) {
    if (!item) return;
    if (item.kind === "concept") location.hash = `#/explore/${item.node.id}`;
    else location.hash = `#/series/${item.leaf.series.source}/${encodeURIComponent(item.leaf.series.id)}`;
  }

  // Labels for the overview's areas, the focused concept with its ancestors and children,
  // and the search trail. Everything else is a hover away.
  refreshLabels() {
    if (!this.scene) return;
    const { nodes, children } = this.data.lay;
    const ids = new Set(children.get(null));
    if (this.focus) {
      ancestors(nodes, this.focus).forEach((id) => ids.add(id));
      (children.get(this.focus) ?? []).forEach((id) => ids.add(id));
    }
    this.trailIds().forEach((id) => ids.add(id));
    const trail = new Set(this.trailIds());
    this.labelsEl.innerHTML = "";
    this.labels = [...ids].map((id) => {
      const n = nodes.get(id);
      const el = document.createElement("a");
      el.href = `#/explore/${id}`;
      el.className = `tag${id === this.focus ? " focus" : ""}${trail.has(id) ? " trail" : ""}${n.depth === 1 ? " top" : ""}`;
      el.innerHTML = `<span>${esc(n.name)}</span><i>${fmtInt(n.total)}</i>`;
      this.labelsEl.appendChild(el);
      return { el, node: n };
    });
  }

  // Nearest labels win: each frame, labels are placed front to back and any that would
  // overlap one already placed is hidden, so the tree never reads as a pile of tags.
  placeLabels() {
    if (!this.labels) return;
    const placed = [];
    const items = this.labels.map((l) => {
      const n = l.node;
      return { ...l, p: this.scene.project([n.pos[0], n.pos[1] + n.size + 3, n.pos[2]]),
               d: this.scene.distance(n.pos), pri: l.el.classList.contains("focus") ? 0 : l.el.classList.contains("trail") ? 1 : 2 };
    }).sort((a, b) => a.pri - b.pri || a.d - b.d);
    for (const { el, p } of items) {
      if (!p.visible) { el.style.visibility = "hidden"; continue; }
      el.w ??= el.offsetWidth;
      el.h ??= el.offsetHeight;
      const box = { l: p.x - el.w / 2, r: p.x + el.w / 2, t: p.y - el.h, b: p.y };
      const hit = placed.some((o) => box.l < o.r + 4 && box.r > o.l - 4 && box.t < o.b + 2 && box.b > o.t - 2);
      el.style.visibility = hit ? "hidden" : "visible";
      el.style.transform = `translate(${Math.round(box.l)}px, ${Math.round(box.t)}px)`;
      if (!hit) placed.push(box);
    }
  }

  // --- panel ---------------------------------------------------------------------------

  renderPanel() {
    if (this.result) return this.renderResult();
    if (this.focus) return this.renderConcept(this.focus);
    return this.renderOverview();
  }

  renderOverview() {
    const { concepts, series, methods, lay } = this.data;
    const top = lay.children.get(null).map((id) => this.data.byId.get(id));
    const max = Math.max(...top.map((c) => c.total), 1);
    const methodChips = Object.entries(methods).map(([m, n]) => `<span class="chip">${fmtInt(n)} ${esc(m)}</span>`).join("");
    const unfiled = series.filter((s) => !s.concept).length;
    this.panel.innerHTML = `
      <div class="label">the tree · what is measured</div>
      <h2 class="panel-title">${fmtInt(series.length)} series in ${fmtInt(concepts.length)} concepts</h2>
      <div class="foot">${methodChips}${unfiled ? `<span class="chip">${fmtInt(unfiled)} unfiled</span>` : ""}</div>
      ${methods.draft ? `<p class="note">Placements marked <b>draft</b> were written by Claude for a first look. Run <code>tempus ontology file</code> to let Jev decide them.</p>` : ""}
      <ol class="rows">${top.map((c) => `
        <li><a href="#/explore/${esc(c.id)}"><span>${esc(c.name)}</span>
          <i class="bar" style="--w:${(c.total / max) * 100}%"></i><b>${fmtInt(c.total)}</b></a></li>`).join("")}
      </ol>
      <div class="label try">try: ${EXAMPLES.map((q) => `<a href="#/ask/${encodeURIComponent(q)}">${esc(q)}</a>`).join(" · ")}</div>`;
  }

  renderConcept(id) {
    const c = this.data.byId.get(id);
    const { lay, series } = this.data;
    const crumbs = ancestors(lay.nodes, id).slice(0, -1).map((a) => this.data.byId.get(a));
    const kids = (lay.children.get(id) ?? []).map((k) => this.data.byId.get(k));
    const inside = subtree(lay.children, id);
    const direct = series.filter((s) => s.concept === id);
    const shown = (direct.length ? direct : series.filter((s) => inside.has(s.concept)));
    const max = Math.max(...kids.map((k) => k.total), 1);
    this.panel.innerHTML = `
      <nav class="crumbs label"><a href="#/explore">tree</a>${crumbs.map((a) => ` / <a href="#/explore/${esc(a.id)}">${esc(a.name)}</a>`).join("")}</nav>
      <h2 class="panel-title"><mark>${esc(c.name)}</mark></h2>
      <p class="desc">${esc(c.description)}</p>
      <div class="foot"><span class="chip">${fmtInt(c.total)} series</span><span class="chip">${esc(c.id)}</span></div>
      ${kids.length ? `<div class="label">inside</div><ol class="rows">${kids.map((k) => `
        <li><a href="#/explore/${esc(k.id)}"><span>${esc(k.name)}</span>
          <i class="bar" style="--w:${(k.total / max) * 100}%"></i><b>${fmtInt(k.total)}</b></a></li>`).join("")}</ol>` : ""}
      ${shown.length ? `<div class="label">${direct.length ? "series here" : "most popular below"} · ${fmtInt(shown.length)}</div>
        ${seriesList(shown.slice(0, SERIES_SHOWN), this.theme)}
        ${shown.length > SERIES_SHOWN ? `<button class="more btn ghost">Show all ${fmtInt(shown.length)}</button>` : ""}` : ""}`;
    this.panel.querySelector(".more")?.addEventListener("click", (e) => {
      e.target.insertAdjacentHTML("beforebegin", seriesList(shown.slice(SERIES_SHOWN), this.theme));
      e.target.remove();
    });
  }

  renderResult() {
    const r = this.result;
    const fallback = r.chooser !== "jev";
    const routes = r.paths.map((p, i) => `
      <li class="route${i === 0 ? " best" : ""}">
        <a href="#/explore/${esc(p.concepts.at(-1))}">${p.steps.map((s, k) => `
          <span class="step"><span>${esc(p.names[k])}</span><i>${s.p.toFixed(2)}</i></span>`).join('<span class="sep">›</span>')}</a>
        <span class="label">score ${p.score.toFixed(2)}${p.stopped === "none" ? " · stopped: none of the options below fit" : ""}</span>
      </li>`).join("");
    this.panel.innerHTML = `
      <div class="label">asked · ${fallback ? `<span class="flag">${esc(r.chooser)}</span>` : "jev"}</div>
      <h2 class="panel-title">${esc(r.query)}</h2>
      <div class="foot"><a class="btn" href="#/draft/${encodeURIComponent(r.query)}">Draft a card</a>
        <span class="desc">a chart that answers it, ready to keep</span></div>
      ${r.paths.length ? "" : `<p class="note">Nothing in the tree fits that. Try other words, or browse.</p>`}
      ${r.ambiguous ? `<p class="note"><b>Two readings.</b> The top routes are close; both are lit.</p>` : ""}
      ${routes ? `<div class="label">route${r.paths.length > 1 ? "s" : ""} · probability per step</div><ol class="routes">${routes}</ol>` : ""}
      ${r.series.length ? `<div class="label">matching series</div>${seriesList(r.series.map((s) => ({
        ...this.meta.get(`${s.source}:${s.series_id}`), ...s, id: s.series_id })), this.theme, true)}` : ""}
      <a class="btn ghost" href="#/explore">Clear</a>`;
  }

  renderLegend() {
    const c = this.theme.color;
    const color = { D: c["neon-cyan"], W: c["neon-violet"], M: c.primary, Q: c["neon-orange"], Y: c["neon-green"] };
    this.el.querySelector(".legend").innerHTML = FREQ_KEYS.map((k) =>
      `<span class="chip"><span class="dot" style="background:${color[k]}"></span>${FREQ[k]}</span>`).join("") +
      `<span class="chip"><span class="dot ring"></span>ringed = synced</span>`;
  }

  // --- outline (2D) mode ---------------------------------------------------------------

  renderOutline() {
    const { lay, byId, series } = this.data;
    const open = new Set([...(this.focus ? ancestors(lay.nodes, this.focus) : []), ...this.trailIds()]);
    const trail = new Set(this.trailIds());
    const bySeries = new Map();
    for (const s of series) {
      if (!bySeries.has(s.concept)) bySeries.set(s.concept, []);
      bySeries.get(s.concept).push(s);
    }
    const node = (id) => {
      const c = byId.get(id);
      const kids = lay.children.get(id) ?? [];
      const own = bySeries.get(id) ?? [];
      return `<details ${open.has(id) ? "open" : ""} data-id="${esc(id)}">
        <summary class="${trail.has(id) ? "trail" : ""}${id === this.focus ? " focus" : ""}">
          <span>${esc(c.name)}</span><i>${fmtInt(c.total)}</i></summary>
        ${kids.map(node).join("")}
        ${own.length ? `<div class="own" data-id="${esc(id)}"></div>` : ""}
      </details>`;
    };
    this.stage.innerHTML = `<div class="outline">${lay.children.get(null).map(node).join("")}</div>`;
    const fill = (d) => {
      const own = d.querySelector(":scope > .own");
      if (own && !own.childElementCount) own.innerHTML = seriesList(bySeries.get(own.dataset.id) ?? [], this.theme);
    };
    this.stage.querySelectorAll("details").forEach((d) => {
      if (d.open) fill(d);
      d.addEventListener("toggle", () => d.open && fill(d));
    });
    this.stage.querySelector("summary.focus")?.scrollIntoView({ block: "center" });
  }
}

export function seriesList(list, theme, withP = false) {
  const c = theme.color;
  const color = { D: c["neon-cyan"], W: c["neon-violet"], M: c.primary, Q: c["neon-orange"], Y: c["neon-green"] };
  return `<ul class="series-list">${list.map((s) => `
    <li><a href="#/series/${esc(s.source)}/${encodeURIComponent(s.id)}" title="${esc(s.title)}">
      <span class="dot${s.synced ? " ring" : ""}" style="background:${color[s.freq] ?? c.pencil}"></span>
      <span class="t">${esc(s.title)}</span>
      <code>${esc(s.id)}</code>${withP ? `<i class="p" style="--w:${Math.round((s.p ?? 0) * 100)}%">${(s.p ?? 0).toFixed(2)}</i>` : ""}
    </a></li>`).join("")}</ul>`;
}
