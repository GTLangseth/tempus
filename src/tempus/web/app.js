// The tempus app: a hash router over five views. Explore (the default) is the 3D concept
// tree with natural-language search; series pages show one series; drafts turn a question
// into a card; compare lines up hand-picked series; cards hot-reload over /api/events.
// Layout and tokens follow docs/DESIGN.md (Neon Notebook).
import { mountCompare, refreshTray } from "./compare.js";
import { CardsView } from "./views/cards.js";
import { CompareView } from "./views/compare.js";
import { DraftView } from "./views/draft.js";
import { ExploreView } from "./views/explore.js";
import { SeriesView } from "./views/series.js";

const theme = await (await fetch("/theme.json")).json();
const root = document.documentElement.style;
for (const [k, v] of Object.entries(theme.color)) if (typeof v === "string") root.setProperty(`--${k}`, v);
for (const [k, v] of Object.entries(theme.font)) root.setProperty(`--font-${k}`, v);
for (const [k, v] of Object.entries(theme.rounded)) root.setProperty(`--rounded-${k}`, `${v}px`);
root.setProperty("--grid-cell", `${theme.grid}px`);
await document.fonts.ready; // so charts measure text in the real fonts

// Reading prod through the tunnel (TEMPUS_DB=prod): say so on every page.
const meta = await fetch("/api/meta").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
// The shared guest copy can't draft or keep cards, so it shows no Draft button.
if (meta.guest) document.body.dataset.guest = "";
if (meta.remote) {
  const badge = document.createElement("span");
  badge.className = "chip remote-badge";
  badge.textContent = "prod · read-only";
  badge.title = "The home base's database, through an SSH tunnel. Nothing here can change it.";
  document.querySelector("header .brand").after(badge);
}

const VIEWS = { explore: ExploreView, series: SeriesView, cards: CardsView, draft: DraftView, compare: CompareView };
const outlet = document.getElementById("view");
let active = null; // {kind, view}

export function parse(hash) {
  // Refs are encoded one by one and joined by commas, so split before decoding.
  const compare = hash.match(/^#\/?compare\/(.*)$/);
  if (compare) return { kind: "compare", refs: compare[1].split(",").filter(Boolean).map(decodeURIComponent) };
  const parts = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  switch (parts[0]) {
    case "series": return { kind: "series", source: parts[1], id: parts.slice(2).join("/") };
    case "cards": return { kind: "cards", card: parts[1] || null };
    case "draft": return { kind: "draft", question: parts.slice(1).join("/") };
    case "ask": return { kind: "explore", view: "ask", query: parts.slice(1).join("/") };
    case "explore": return { kind: "explore", concept: parts[1] || null };
    default: return { kind: "explore", concept: null };
  }
}

async function route() {
  const r = parse(location.hash);
  document.querySelectorAll("header nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.view === (["series", "draft", "compare"].includes(r.kind) ? "explore" : r.kind)));
  document.body.dataset.view = r.kind;
  refreshTray();
  if (active?.kind === r.kind) return active.view.update(r);
  active?.view.unmount();
  if (r.kind !== "series") document.title = "tempus";
  const view = new VIEWS[r.kind](outlet, theme);
  active = { kind: r.kind, view };
  await view.mount(r);
}

mountCompare();
window.addEventListener("hashchange", route);
const events = new EventSource("/api/events");
for (const kind of ["card-changed", "card-invalid", "card-removed"]) {
  events.addEventListener(kind, (e) => {
    if (active?.kind === "cards") active.view.onEvent(JSON.parse(e.data).card);
  });
}
route();
