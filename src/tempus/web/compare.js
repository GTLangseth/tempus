// The compare selection: the series the user has picked to compare, as an ordered list of
// {ref: "source:id", title}. It persists in localStorage, and every + Compare toggle and the
// tray pinned to the bottom of the page re-render from it on each change.
import { esc } from "./util.js";

const MAX = 6;
const KEY = "tempus.compare";

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(saved) ? saved.filter((p) => typeof p?.ref === "string").slice(0, MAX) : [];
  } catch {
    return [];
  }
}

let picks = load();
const listeners = new Set();

function changed() {
  try { localStorage.setItem(KEY, JSON.stringify(picks)); } catch { /* storage may be blocked */ }
  listeners.forEach((fn) => fn());
}

export const selection = {
  list: () => picks,
  has: (ref) => picks.some((p) => p.ref === ref),
  // False when the selection is already full.
  add(ref, title) {
    if (this.has(ref)) return true;
    if (picks.length >= MAX) return false;
    picks = [...picks, { ref, title: title || ref }];
    changed();
    return true;
  },
  remove(ref) {
    picks = picks.filter((p) => p.ref !== ref);
    changed();
  },
  clear() {
    picks = [];
    changed();
  },
  onChange(fn) { listeners.add(fn); },
};

// Another tab changed the selection.
window.addEventListener("storage", (e) => {
  if (e.key !== KEY) return;
  picks = load();
  listeners.forEach((fn) => fn());
});

export const compareHash = (refs) => `#/compare/${refs.map(encodeURIComponent).join(",")}`;
const shorten = (s, n) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);
// Many titles share a long prefix ("Market Yield on U.S. Treasury Securities at …"), so a
// series reads as its id first, then as much of its title as fits.
const seriesId = (ref) => ref.slice(ref.indexOf(":") + 1);
export const refLabel = (ref, title, n) =>
  `<b class="ref-id">${esc(seriesId(ref))}</b>${title && title !== ref && n ? ` <span>${esc(shorten(title, n))}</span>` : ""}`;

// A + Compare toggle for one series. Clicks are handled by the document-wide listener below,
// so markup rendered as strings by any view works without wiring.
// Phones show only the glyph in list rows, so it sits apart from the word.
const toggleFace = (on) => (on ? `<i>✓</i><span> Comparing</span>` : `<i>+</i><span> Compare</span>`);

export function compareToggle(ref, title, extra = "") {
  const on = selection.has(ref);
  return `<button type="button" class="cmp-toggle ${extra}" data-ref="${esc(ref)}" data-title="${esc(title)}"
    aria-pressed="${on}" aria-label="Compare ${esc(title)}">${toggleFace(on)}</button>`;
}

function syncToggles() {
  document.querySelectorAll(".cmp-toggle").forEach((b) => {
    const on = selection.has(b.dataset.ref);
    b.setAttribute("aria-pressed", String(on));
    b.innerHTML = toggleFace(on);
  });
}

// The tray: hidden when the selection is empty and on the compare view itself. It reserves
// its height in --tray-space so pages can keep their bottoms clear of it.
let tray;
let note;

function renderTray() {
  const shown = picks.length > 0 && document.body.dataset.view !== "compare";
  tray.hidden = !shown;
  if (!shown) { document.body.style.setProperty("--tray-space", "0px"); return; }
  tray.querySelector(".tray-chips").innerHTML = picks.map((p) => `<span class="chip" title="${esc(p.title)}">
    <span>${refLabel(p.ref, p.title, 18)}</span><button type="button" data-ref="${esc(p.ref)}"
      aria-label="Stop comparing ${esc(p.title)}">×</button></span>`).join("");
  tray.querySelector(".count").textContent = `${picks.length} of ${MAX}`;
  tray.querySelector(".go").disabled = picks.length < 2;
  tray.querySelector(".go").title = picks.length < 2 ? "Pick at least 2 series" : "";
}

function say(message) {
  note.textContent = message;
  clearTimeout(say.timer);
  say.timer = setTimeout(() => { note.textContent = ""; }, 3500);
}

export function mountCompare() {
  tray = document.createElement("aside");
  tray.className = "tray sticker";
  tray.setAttribute("aria-label", "Series to compare");
  tray.innerHTML = `<div class="tray-chips"></div>
    <div class="tray-actions">
      <span class="label count"></span><span class="desc tray-note" role="status"></span>
      <button type="button" class="btn go">Compare</button>
      <button type="button" class="btn ghost clear">Clear</button>
    </div>`;
  document.body.appendChild(tray);
  note = tray.querySelector(".tray-note");
  tray.querySelector(".tray-chips").addEventListener("click", (e) => {
    const ref = e.target.closest("button")?.dataset.ref;
    if (ref) selection.remove(ref);
  });
  tray.querySelector(".go").addEventListener("click", () => {
    if (picks.length >= 2) location.hash = compareHash(picks.map((p) => p.ref));
  });
  tray.querySelector(".clear").addEventListener("click", () => selection.clear());
  new ResizeObserver(() => {
    if (!tray.hidden) document.body.style.setProperty("--tray-space", `${tray.offsetHeight + 32}px`);
  }).observe(tray);

  document.addEventListener("click", (e) => {
    const b = e.target.closest(".cmp-toggle");
    if (!b) return;
    e.preventDefault();
    if (selection.has(b.dataset.ref)) selection.remove(b.dataset.ref);
    else if (!selection.add(b.dataset.ref, b.dataset.title)) say(`Compare holds up to ${MAX}`);
  });
  selection.onChange(() => { syncToggles(); renderTray(); });
  renderTray();
}

// The router calls this on every route, since the tray hides on the compare view.
export const refreshTray = () => tray && renderTray();
