// The draft view (#/draft/<question>): Jev drafts a card for a question, the user swaps any
// pick for a runner-up, then keeps it (cards/<id>.yaml) or discards it. Phase 2 addendum of
// docs/specs/2026-10-05-question-to-card-design.md. Layout follows docs/DESIGN.md.
import { esc, FREQ } from "../util.js";
import { drawPanels, errorBlock, headlineStats, lendLagFreq, statRow } from "./cards.js";

const SHAPE_WORDS = {
  level: "where it stands",
  change: "how fast it's changing",
  compare: "side by side",
  spread: "the gap between",
  ratio: "one in terms of the other",
  lead_lag: "does one lead the other",
  threshold: "above or below a line",
};
const PROGRESS = [
  "Asking Jev what kind of answer this is…",
  "Finding a series for each part of the answer…",
  "Building the card and running it…",
];
const READ_ONLY = "This shared copy is read-only.";

const shapeWords = (name) => SHAPE_WORDS[name] ?? name.replace(/_/g, " ");
// Roles read like "the first leg of the gap: one rate or price, …"; the lead phrase is enough.
const roleWords = (role) => role.split(/[,:]/)[0];
const p2 = (p) => (p ?? 0).toFixed(2);

// POST JSON; network failures come back as status 0 so callers handle one shape.
async function post(url, body) {
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body) });
    return { status: res.status, body: await res.json().catch(() => null) };
  } catch {
    return { status: 0, body: null };
  }
}

function failure(status, doing) {
  if (status === 403) return READ_ONLY;
  if (status === 0) return "Couldn't reach the app. Check it's still running, then try again.";
  return `Something went wrong ${doing}. Try again.`;
}

export class DraftView {
  constructor(root, theme) {
    this.root = root;
    this.theme = theme;
    this.charts = [];
    this.seq = 0; // bumps on every route; late responses for an old question are dropped
  }

  async mount(route) { await this.update(route); }

  unmount() {
    this.seq++;
    clearInterval(this.ticker);
    this.disposeCharts();
  }

  disposeCharts() {
    this.charts.forEach((c) => c?.dispose());
    this.charts = [];
  }

  async update({ question }) {
    const seq = ++this.seq;
    this.question = question.trim();
    document.title = "Draft a card · tempus";
    if (!this.question) { location.hash = "#/explore"; return; }
    this.disposeCharts();
    this.root.innerHTML = `<main class="feed draft">
      <div class="label">draft · not kept yet</div>
      <article class="sticker" aria-busy="true">
        <div class="label">drafting</div>
        <h1 class="question">${esc(this.question)}</h1>
        <p class="desc progress" role="status">${PROGRESS[0]}</p>
      </article></main>`;
    const line = this.root.querySelector(".progress");
    let step = 0;
    clearInterval(this.ticker);
    this.ticker = setInterval(() => { line.textContent = PROGRESS[Math.min(++step, PROGRESS.length - 1)]; }, 1800);

    const { status, body } = await post("/api/draft", { question: this.question });
    clearInterval(this.ticker);
    if (seq !== this.seq) return;
    if (status !== 200 || !body) return this.renderFailure(failure(status, "drafting this card"));
    this.chooser = body.chooser;
    // Only the first draft carries runners-up; keep them as each role's choices for swaps.
    this.options = body.picks.map((p) => [
      { series: p.series, title: p.title, probability: p.probability }, ...p.runners_up]);
    this.render(body);
  }

  renderFailure(message) {
    this.disposeCharts();
    this.root.innerHTML = `<main class="feed draft">
      <div class="label">draft · not kept yet</div>
      <article class="sticker">
        <div class="label">couldn't draft</div>
        <h1 class="question">${esc(this.question)}</h1>
        <p class="desc">${esc(message)}</p>
        <div class="foot">${message === READ_ONLY ? "" : `<button class="btn retry" type="button">Try again</button>`}
          <a class="btn ghost" href="#/explore">Back to explore</a></div>
      </article></main>`;
    this.root.querySelector(".retry")?.addEventListener("click", () => this.update({ question: this.question }));
  }

  render(draft) {
    this.draft = draft;
    this.disposeCharts();
    const { card, shape } = draft;
    const freq = lendLagFreq(draft.payloads);
    const errors = draft.errors.filter((e) => e.level === "error");
    const warnings = draft.errors.filter((e) => e.level === "warning");
    const fallback = this.chooser && this.chooser !== "jev";
    const others = shape.ranked.filter(([name]) => name !== shape.name);

    this.root.innerHTML = `<main class="feed draft">
      <div><div class="label">draft · not kept yet</div>
        <h2 class="page-title">${esc(card?.title ?? "No card yet")}</h2></div>
      <article class="sticker">
        <div class="label">drafted by ${fallback ? `<span class="flag">${esc(this.chooser)}</span>` : "jev"}${
          FREQ[freq] ? ` · ${FREQ[freq]}` : ""}</div>
        <h1 class="question">${esc(draft.question)}</h1>
        <div class="shape">
          <span class="label">kind of answer</span>
          <span class="shape-pick"><mark>${esc(shapeWords(shape.name))}</mark><i>${p2(shape.p)}</i></span>
          ${others.length ? `<span class="label shape-also">or ${others.map(([name, p]) =>
            `${esc(shapeWords(name))} ${p2(p)}`).join(" · ")}</span>` : ""}
        </div>
        <div class="picks">
          ${draft.picks.map((pick, i) => `<label class="pick">
            <span class="label" title="${esc(pick.role)}">${esc(roleWords(pick.role))}</span>
            <select>${this.options[i].map((o, k) => `<option value="${k}"${
              o.series === pick.series ? " selected" : ""}>${esc(o.title ?? o.series)} (${esc(o.series.split(":").pop())}) · ${p2(o.probability)}</option>`).join("")}</select>
          </label>`).join("")}
          ${draft.error ? `<p class="warning draft-error"><b>CAN'T BUILD</b> ${esc(draft.error)}. Pick another series above to fix it.</p>` : ""}
        </div>
        ${errorBlock(errors)}
        ${card ? statRow(headlineStats(draft.payloads, this.theme)) : ""}
        <div id="panels"></div>
        ${warnings.map((w) => `<p class="warning"><b>NOTE</b> ${esc(w.message)}</p>`).join("")}
        ${draft.yaml ? `<details class="yaml-box"><summary class="label">the card as YAML</summary><pre class="yaml">${esc(draft.yaml)}</pre></details>` : ""}
        <div class="foot actions">
          <button class="btn keep" type="button"${card ? "" : " disabled"}>Keep</button>
          <a class="btn ghost" href="#/explore">Discard</a>
          <span class="desc status" role="status"></span>
        </div>
      </article></main>`;
    this.status = this.root.querySelector(".status");
    this.root.querySelectorAll(".picks select").forEach((s) => s.addEventListener("change", () => this.swap()));
    this.root.querySelector(".keep").addEventListener("click", () => this.keep());
    this.charts = drawPanels(this.root.querySelector("#panels"), draft.payloads, this.theme);
  }

  busy(on, message = "") {
    this.root.querySelectorAll(".picks select").forEach((s) => { s.disabled = on; });
    this.root.querySelector(".keep").disabled = on || !this.draft.card;
    this.status.textContent = message;
  }

  async swap() {
    const seq = this.seq;
    const picks = [...this.root.querySelectorAll(".picks select")].map((s, i) => {
      const o = this.options[i][Number(s.value)];
      return { role: this.draft.picks[i].role, series: o.series, probability: o.probability };
    });
    this.busy(true, "Rebuilding the card…");
    const { shape, question } = this.draft;
    const { status, body } = await post("/api/draft/assemble",
      { question, shape: shape.name, shape_p: shape.p, picks });
    if (seq !== this.seq) return;
    if (status !== 200 || !body) {
      // Put the selects back on the picks the card on screen was built from.
      this.root.querySelectorAll(".picks select").forEach((sel, i) => {
        sel.value = String(this.options[i].findIndex((o) => o.series === this.draft.picks[i].series));
      });
      this.busy(false, failure(status, "rebuilding the card"));
      return;
    }
    this.render(body); // `chooser` stays the first draft's: it picked the shape and the options
  }

  async keep() {
    const seq = this.seq;
    this.busy(true, "Keeping…");
    const { status, body } = await post("/api/cards", { card: this.draft.card });
    if (seq !== this.seq) return;
    if (status === 201 && body?.id) { location.hash = `#/cards/${encodeURIComponent(body.id)}`; return; }
    this.busy(false, status === 400 ? "" : failure(status, "keeping the card"));
    if (status === 400) {
      // FastAPI puts the reasons in `detail`: a list of card errors, or one message.
      const detail = body?.errors ?? body?.detail;
      const errors = Array.isArray(detail) ? detail.map((e) => (typeof e === "string" ? { path: "card", message: e } : e))
        : [{ path: "card", message: String(detail ?? "The card didn't pass its checks.") }];
      this.root.querySelector(".keep-errors")?.remove();
      this.root.querySelector(".actions").insertAdjacentHTML("beforebegin",
        `<div class="keep-errors">${errorBlock(errors)}</div>`);
    }
  }
}
