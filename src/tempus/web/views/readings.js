// The readings view: full-text research reader interleaved with live tempus cards, claims, and lab notes.
// Hot-reloads over /api/events when either a reading or an embedded card changes.
import { fmt } from "../adapters/echarts.js";
import { drawPanels, errorBlock, headlineStats, lendLagFreq, statRow } from "./cards.js";
import { esc, FREQ } from "../util.js";

const CLAIM_META = {
  confirmed: { label: "TEMPUS · CONFIRMED", short: "CONFIRMED", cls: "is-confirmed" },
  extended: { label: "TEMPUS · EXTENDED", short: "EXTENDED", cls: "is-extended" },
  nuanced: { label: "TEMPUS · NUANCED", short: "NUANCED", cls: "is-nuanced" },
  unverified: { label: "TEMPUS · UNVERIFIED IN STORE", short: "UNVERIFIED IN STORE", cls: "is-unverified" },
};

const MODES = [
  { id: "annotated", label: "Annotated" },
  { id: "essay", label: "Essay" },
  { id: "digest", label: "Digest" },
];

const INGEST_KINDS = [
  { id: "pdf", label: "PDF Upload" },
  { id: "url", label: "Article URL" },
  { id: "text", label: "Paste Text" },
  { id: "markdown", label: "Import Markdown" },
];

const INGEST_STEPS = [
  { id: "queued", label: "Queued", detail: "Starting ingestion pipeline…" },
  { id: "extracting", label: "Extract prose", detail: "Reading source and cleaning full essay prose…" },
  { id: "resolving", label: "FRED search & sync", detail: "Matching ontology concepts and syncing FRED series…" },
  { id: "composing", label: "Compose chart cards", detail: "Authoring SQL-grounded chart cards and claim checks…" },
  { id: "saving", label: "Save to Postgres", detail: "Validating and saving reading & charts to Postgres…" },
  { id: "complete", label: "Ready", detail: "Done — opening your new reading…" },
];

const INGEST_STEP_MAP = Object.fromEntries(INGEST_STEPS.map((s) => [s.id, s]));

function formatElapsed(ms) {
  const sec = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(sec / 60);
  const s = String(sec % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

function safeUrl(raw) {
  return /^(https?:\/\/|#\/)/.test(raw) ? raw : null;
}

// Escape HTML first, then render inline markdown (code, ==mark==, **bold**, *em*, links, [^fn]).
function inlineMd(text, footnotesById = {}) {
  const codes = [];
  let s = esc(text).replace(/`([^`]+)`/g, (_, code) => {
    const token = `\u0000C${codes.length}\u0000`;
    codes.push(`<code>${code}</code>`);
    return token;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
    const safe = safeUrl(url);
    if (!safe) return `${label} (${url})`;
    const ext = safe.startsWith("http") ? ` target="_blank" rel="noopener"` : "";
    return `<a href="${safe}"${ext}>${label}</a>`;
  });
  s = s.replace(/==([^=]+)==/g, "<mark>$1</mark>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  s = s.replace(/\[\^([A-Za-z0-9_-]+)\]/g, (_, fid) => {
    const note = footnotesById[fid];
    const title = note ? ` title="${esc(note)}"` : "";
    return `<sup class="fn-sup"><button type="button" class="fn-ref" data-fn="${esc(fid)}" aria-expanded="false"${title}>[${esc(fid)}]</button></sup>`;
  });
  return s.replace(/\u0000C(\d+)\u0000/g, (_, i) => codes[Number(i)]);
}

function claimScorecard(counts) {
  return Object.entries(CLAIM_META)
    .filter(([k]) => (counts?.[k] ?? 0) > 0)
    .map(([k, m]) => `<span class="chip claim-pill ${m.cls}">${counts[k]} ${m.short}</span>`)
    .join("");
}

function renderProseBlock(b, footnotesById, opts = {}) {
  switch (b.type) {
    case "heading": {
      const idAttr = opts.secId ? ` id="${esc(opts.secId)}"` : "";
      return `<h${b.level} class="essay-only"${idAttr}>${inlineMd(b.text, footnotesById)}</h${b.level}>`;
    }
    case "paragraph": {
      const cls = opts.lead ? "lead-para essay-only" : "essay-only";
      return `<p class="${cls}">${inlineMd(b.text, footnotesById)}</p>`;
    }
    case "quote":
      return `<blockquote class="essay-only">${inlineMd(b.text, footnotesById)}</blockquote>`;
    case "list": {
      const tag = b.ordered ? "ol" : "ul";
      return `<${tag} class="essay-only">${b.items.map((item) => `<li>${inlineMd(item, footnotesById)}</li>`).join("")}</${tag}>`;
    }
    case "divider":
      return `<hr class="essay-only">`;
    default:
      return "";
  }
}

function renderClaimBlock(b, footnotesById) {
  const m = CLAIM_META[b.status] ?? CLAIM_META.unverified;
  const chips = (b.series_info ?? []).map((s) => {
    const val = s.latest_value != null ? ` · ${fmt(s.latest_value)}` : "";
    const href = `#/series/${encodeURIComponent(s.source)}/${encodeURIComponent(s.series_id)}`;
    return `<a class="chip" href="${href}" title="${esc(s.title ?? s.ref)}"><code>${esc(s.ref)}</code>${esc(val)}</a>`;
  }).join("");
  const cardLink = b.card
    ? `<a class="chip" href="#/cards/${encodeURIComponent(b.card)}">card: ${esc(b.card)} →</a>`
    : "";
  return `
    <aside class="claim-callout tempus-only ${m.cls}">
      <div class="claim-head">
        <span class="claim-badge ${m.cls}">${m.label}</span>
        ${cardLink}
      </div>
      <blockquote class="claim-quote">“${inlineMd(b.quote, footnotesById)}”</blockquote>
      <p class="claim-verdict">${inlineMd(b.verdict, footnotesById)}</p>
      ${chips ? `<div class="foot">${chips}</div>` : ""}
    </aside>`;
}

function renderNoteBlock(b, footnotesById) {
  const bullets = [];
  const parts = [];
  const flushBullets = () => {
    if (bullets.length) {
      parts.push(`<ul>${bullets.map((item) => `<li>${inlineMd(item, footnotesById)}</li>`).join("")}</ul>`);
      bullets.length = 0;
    }
  };
  for (const p of b.paragraphs ?? []) {
    if (p.startsWith("- ")) {
      bullets.push(p.slice(2));
    } else {
      flushBullets();
      parts.push(`<p>${inlineMd(p, footnotesById)}</p>`);
    }
  }
  flushBullets();
  return `
    <aside class="lab-note tempus-only">
      <div class="label">tempus · lab note</div>
      ${b.title ? `<h3>${inlineMd(b.title, footnotesById)}</h3>` : ""}
      ${parts.join("")}
    </aside>`;
}

export class ReadingsView {
  constructor(root, theme, meta = {}) {
    this.root = root;
    this.theme = theme;
    this.meta = meta;
    this.charts = [];
    this.conceptNames = {};
    this.embeddedCards = [];
    this.footnotesById = {};
    this.current = null;
    this.mode = "annotated";
    this.ingestKind = "pdf";
    this.activeJob = null;
    this.pollTimer = null;
    this.elapsedTimer = null;
    this.jobStartedAt = null;
    this._lastJobKey = null;
    this.submitting = false;
    this.ingestError = null;
    this.pendingDelete = false;
    this._onClick = this.handleClick.bind(this);
  }

  canWrite() {
    return !this.meta?.read_only && !this.meta?.guest;
  }

  stopPolling() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.stopElapsedTimer();
  }

  startElapsedTimer(startedIso = null) {
    const parsed = startedIso ? Date.parse(startedIso) : NaN;
    if (!this.jobStartedAt) {
      this.jobStartedAt = Number.isFinite(parsed) ? parsed : Date.now();
    }
    if (this.elapsedTimer) return;
    this.elapsedTimer = setInterval(() => {
      const el = this.root.querySelector(".ingest-elapsed");
      if (el && this.jobStartedAt) {
        el.textContent = formatElapsed(Date.now() - this.jobStartedAt);
      }
    }, 1000);
  }

  stopElapsedTimer() {
    if (this.elapsedTimer) {
      clearInterval(this.elapsedTimer);
      this.elapsedTimer = null;
    }
  }

  startPolling(jobId) {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.startElapsedTimer(this.activeJob?.created_at);
    this.pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/ingest/${encodeURIComponent(jobId)}`);
        if (!res.ok) return;
        const job = await res.json();
        await this.handleJobUpdate(job);
      } catch {
        // SSE or next interval will recover cleanly
      }
    }, 1500);
  }

  async handleJobUpdate(job) {
    if (!job || (this.activeJob && this.activeJob.id && job.id !== this.activeJob.id)) return;
    this.activeJob = job;
    this.updateJobPanel();
    this.syncSubmitButton();
    if (job.status === "done" && job.reading_id) {
      this.stopPolling();
      this.jobStartedAt = null;
      await this.loadSidebar();
      location.hash = `#/readings/${encodeURIComponent(job.reading_id)}`;
    } else if (job.status === "failed") {
      this.stopPolling();
      this.jobStartedAt = null;
    }
  }

  async onIngestEvent(ev) {
    if (!ev?.job) return;
    if (this.current === "new" && (!this.activeJob || ev.job.id === this.activeJob.id)) {
      await this.handleJobUpdate(ev.job);
    }
  }

  async mount(route) {
    const addBtn = this.canWrite()
      ? `<a class="btn-add-article" href="#/readings/new">+ Add Article</a>`
      : "";
    this.root.innerHTML = `<div class="cards-layout">
      <aside id="sidebar">
        <div class="sidebar-head">
          <div class="label">readings · library</div>
          ${addBtn}
        </div>
        <nav id="reading-list"></nav>
        <div id="reading-toc" class="sidebar-toc" hidden></div>
      </aside>
      <main id="main" data-reading-mode="${this.mode}"><p class="label">Loading readings…</p></main>
    </div>`;
    this.root.addEventListener("click", this._onClick);
    await this.update(route);
  }

  unmount() {
    this.stopPolling();
    this.root.removeEventListener("click", this._onClick);
    this.disposeCharts();
  }

  disposeCharts() {
    this.charts.forEach((c) => c?.dispose());
    this.charts = [];
  }

  setMode(mode) {
    if (!MODES.some((m) => m.id === mode)) return;
    this.mode = mode;
    const main = this.root.querySelector("#main");
    if (main) main.dataset.readingMode = mode;
    this.root.querySelectorAll(".reading-mode-switch button").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.mode === mode));
    });
    if (mode !== "essay") {
      requestAnimationFrame(() => this.charts.forEach((c) => c?.resize()));
    }
  }

  async handleClick(e) {
    const ingestTab = e.target.closest(".ingest-kind-switch button[data-ingest-kind]");
    if (ingestTab) {
      this.ingestKind = ingestTab.dataset.ingestKind;
      this.ingestError = null;
      await this.renderIngestSheet();
      return;
    }

    const delBtn = e.target.closest(".btn-delete-reading[data-delete-id]");
    if (delBtn) {
      const rid = delBtn.dataset.deleteId;
      if (!this.pendingDelete) {
        this.pendingDelete = true;
        delBtn.textContent = "Confirm delete?";
        delBtn.classList.add("is-armed");
        return;
      }
      delBtn.disabled = true;
      delBtn.textContent = "Deleting…";
      const res = await fetch(`/api/readings/${encodeURIComponent(rid)}`, { method: "DELETE" });
      this.pendingDelete = false;
      if (res.ok) {
        await this.loadSidebar();
        location.hash = "#/readings";
      } else {
        const err = await res.json().catch(() => ({}));
        delBtn.disabled = false;
        delBtn.textContent = err.detail || "Delete failed";
        delBtn.classList.remove("is-armed");
      }
      return;
    }

    const modeBtn = e.target.closest(".reading-mode-switch button[data-mode]");
    if (modeBtn) {
      this.setMode(modeBtn.dataset.mode);
      return;
    }

    const tocBtn = e.target.closest(".toc-link[data-scroll-target]");
    if (tocBtn) {
      if (this.mode === "digest") this.setMode("annotated");
      const target = document.getElementById(tocBtn.dataset.scrollTarget);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const backBtn = e.target.closest(".fn-back[data-fn-target]");
    if (backBtn) {
      const ref = this.root.querySelector(`.fn-ref[data-fn="${CSS.escape(backBtn.dataset.fnTarget)}"]`);
      if (ref) {
        ref.scrollIntoView({ behavior: "smooth", block: "center" });
        ref.focus({ preventScroll: true });
      }
      return;
    }

    const fnBtn = e.target.closest(".fn-ref[data-fn]");
    if (fnBtn) {
      const fid = fnBtn.dataset.fn;
      const host = fnBtn.closest("p, blockquote, li, .claim-callout");
      if (!host) return;
      const expanded = fnBtn.getAttribute("aria-expanded") === "true";
      const next = host.nextElementSibling;
      if (next?.classList.contains("fn-inline") && next.dataset.fnBox === fid) {
        next.remove();
        fnBtn.setAttribute("aria-expanded", "false");
        return;
      }
      if (next?.classList.contains("fn-inline")) next.remove();
      host.querySelectorAll('.fn-ref[aria-expanded="true"]').forEach((b) => {
        b.setAttribute("aria-expanded", "false");
      });
      if (expanded) {
        fnBtn.setAttribute("aria-expanded", "false");
        return;
      }
      const text = this.footnotesById[fid];
      if (!text) return;
      const box = document.createElement("aside");
      box.className = "fn-inline essay-only";
      box.dataset.fnBox = fid;
      box.innerHTML = `<div class="fn-inline-head"><span class="label">Footnote ${esc(fid)}</span><button type="button" class="fn-close" aria-label="Close footnote">×</button></div><p>${inlineMd(text)}</p>`;
      box.querySelector(".fn-close").addEventListener("click", () => {
        box.remove();
        fnBtn.setAttribute("aria-expanded", "false");
      });
      host.after(box);
      fnBtn.setAttribute("aria-expanded", "true");
    }
  }

  async update(route) {
    this.pendingDelete = false;
    this.current = route.reading ?? null;
    if (this.current !== "new") this.stopPolling();
    const list = await this.loadSidebar();
    await this.showReading(this.current, list);
  }

  async onEvent(ev) {
    if (ev.kind?.startsWith("reading-")) {
      const list = await this.loadSidebar();
      if (this.current && this.current !== "new" && ev.reading === this.current) {
        await this.showReading(this.current, list);
      } else if (!this.current) {
        await this.showReading(null, list);
      }
      return;
    }
    if (ev.kind?.startsWith("card-") && this.current && this.embeddedCards.includes(ev.card)) {
      await this.showReading(this.current);
    }
  }

  async loadSidebar() {
    const [readings, concepts] = await Promise.all([
      fetch("/api/readings").then((r) => r.json()),
      fetch("/api/concepts").then((r) => r.json()).catch(() => ({ tree: [] })),
    ]);
    this.conceptNames = {};
    const walk = (n, path) => {
      this.conceptNames[n.id] = [...path, n.name].join(" / ");
      n.children.forEach((c) => walk(c, [...path, n.name]));
    };
    concepts.tree.forEach((n) => walk(n, []));

    const nav = this.root.querySelector("#reading-list");
    nav.innerHTML = readings.length
      ? readings.map((r) => {
          const cls = ["card", "reading-item", r.id === this.current ? "active" : "", r.errors ? "invalid" : ""].join(" ");
          const sub = [r.published, r.reading_minutes ? `~${r.reading_minutes} min` : null].filter(Boolean).join(" · ");
          return `<a class="${cls}" href="#/readings/${encodeURIComponent(r.id)}" title="${esc(r.summary ?? r.title)}">
            <span>${esc(r.title)}</span>
            ${sub ? `<i class="label">${esc(sub)}</i>` : ""}
          </a>`;
        }).join("")
      : `<p class="label">No readings yet. <code>tempus reading new my-article</code></p>`;
    return readings;
  }

  renderToc(sections) {
    const toc = this.root.querySelector("#reading-toc");
    if (!toc) return;
    if (!sections.length) {
      toc.hidden = true;
      toc.innerHTML = "";
      return;
    }
    toc.hidden = false;
    toc.innerHTML = `<div class="label">sections</div>` + sections
      .map((s) => `<button type="button" class="toc-link" data-scroll-target="${esc(s.id)}">${esc(s.text)}</button>`)
      .join("");
  }

  async showReading(id, cachedList = null) {
    this.disposeCharts();
    this.embeddedCards = [];
    this.footnotesById = {};
    const main = this.root.querySelector("#main");
    main.dataset.readingMode = this.mode;

    if (id === "new") {
      this.renderToc([]);
      await this.renderIngestSheet();
      return;
    }

    if (!id) {
      this.renderToc([]);
      document.title = "Readings · tempus";
      const list = cachedList ?? await fetch("/api/readings").then((r) => r.json());
      const addAction = this.canWrite()
        ? `<a class="btn-ingest-primary" href="#/readings/new">+ Add Article</a>`
        : "";
      if (!list.length) {
        main.innerHTML = `<article class="sticker"><div class="label">readings</div>
          <h1 class="question">No readings yet.</h1>
          <p class="notes">Add a PDF, article URL, text, or Markdown digest into Postgres${addAction ? " below" : ""}.</p>
          ${addAction ? `<div class="foot">${addAction}</div>` : ""}</article>`;
        return;
      }
      main.innerHTML = `<div class="library-head">
          <div>
            <div class="label">readings · ${list.length} item${list.length > 1 ? "s" : ""}</div>
            <h1 class="page-title">Library</h1>
          </div>
          ${addAction}
        </div>` + list.map((r) => {
          const eyebrow = [r.source, r.published, r.reading_minutes ? `~${r.reading_minutes} min read` : null].filter(Boolean).join(" · ") || "reading";
          return `<article class="sticker">
            <div class="label">${esc(eyebrow)}</div>
            <h2 class="question"><a class="reading-title-link" href="#/readings/${encodeURIComponent(r.id)}">${esc(r.title)}</a></h2>
            ${r.authors?.length ? `<div class="label">${esc(r.authors.join(", "))}</div>` : ""}
            ${r.summary ? `<p class="notes">${esc(r.summary)}</p>` : ""}
            <div class="foot">
              ${claimScorecard(r.claim_counts)}
              <span class="chip">${r.cards} card${r.cards === 1 ? "" : "s"}</span>
              ${(r.concepts ?? []).map((c) => `<a class="chip" href="#/explore/${esc(c)}">${esc(this.conceptNames[c] ?? c)}</a>`).join("")}
            </div>
          </article>`;
        }).join("");
      return;
    }

    const res = await fetch(`/api/readings/${encodeURIComponent(id)}`);
    if (!res.ok) {
      this.renderToc([]);
      main.innerHTML = `<p class="label">No reading '${esc(id)}'.</p>`;
      return;
    }
    const body = await res.json();
    const meta = body.meta;
    this.embeddedCards = body.cards ?? [];
    this.footnotesById = Object.fromEntries((body.footnotes ?? []).map((f) => [f.id, f.text]));
    document.title = `${meta?.title ?? id} · tempus`;

    const errors = (body.errors ?? []).filter((e) => e.level === "error");
    const warnings = (body.errors ?? []).filter((e) => e.level === "warning");
    const eyebrow = [
      meta?.source,
      meta?.published,
      body.reading_minutes ? `~${body.reading_minutes} min read` : null,
    ].filter(Boolean).join(" · ") || `reading · ${id}`;
    const urlChip = meta?.url && safeUrl(meta.url)
      ? `<a class="chip" href="${esc(safeUrl(meta.url))}" target="_blank" rel="noopener">Source ↗</a>`
      : "";
    const modeButtons = MODES.map((m) =>
      `<button type="button" data-mode="${m.id}" aria-pressed="${this.mode === m.id}">${m.label}</button>`
    ).join("");
    const deleteBtn = this.canWrite() && body.origin && body.origin !== "file"
      ? `<button type="button" class="btn-delete-reading" data-delete-id="${esc(id)}">Delete</button>`
      : "";

    main.innerHTML = `
      <article class="sticker essay-sheet">
        <div class="essay-header">
          <div class="reading-head-bar">
            <div class="label">${esc(eyebrow)}</div>
            <div class="reading-actions">
              <div class="switch reading-mode-switch" role="group" aria-label="Reading mode">${modeButtons}</div>
              ${deleteBtn}
            </div>
          </div>
          <h1 class="question">${esc(meta?.title ?? id)}</h1>
          ${meta?.authors?.length ? `<div class="label">by ${esc(meta.authors.join(", "))}</div>` : ""}
          ${errorBlock(errors)}
          ${warnings.map((w) => `<p class="warning"><b>NOTE</b> ${esc(w.message)}</p>`).join("")}
          <div class="lab-brief tempus-only">
            <div class="label">tempus · lab brief</div>
            ${meta?.summary ? `<p class="reading-summary">${inlineMd(meta.summary, this.footnotesById)}</p>` : ""}
            <div class="foot">
              ${claimScorecard(body.claim_counts)}
              <span class="chip">${this.embeddedCards.length} live card${this.embeddedCards.length === 1 ? "" : "s"}</span>
              ${urlChip}
              ${(meta?.concepts ?? []).map((c) => `<a class="chip" href="#/explore/${esc(c)}">${esc(this.conceptNames[c] ?? c)}</a>`).join("")}
              ${(meta?.tags ?? []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
            </div>
          </div>
        </div>
        <div id="reading-stream" class="reading-body"></div>
      </article>`;

    const sheet = main.querySelector(".essay-sheet");
    const stream = main.querySelector("#reading-stream");
    const sections = [];
    let seenLeadPara = false;

    for (const block of body.blocks ?? []) {
      if (block.type === "card") {
        stream.appendChild(this.renderEmbeddedCard(block));
      } else if (block.type === "claim") {
        const wrap = document.createElement("div");
        wrap.innerHTML = renderClaimBlock(block, this.footnotesById).trim();
        stream.appendChild(wrap.firstElementChild);
      } else if (block.type === "note") {
        const wrap = document.createElement("div");
        wrap.innerHTML = renderNoteBlock(block, this.footnotesById).trim();
        stream.appendChild(wrap.firstElementChild);
      } else {
        const opts = {};
        if (block.type === "heading" && block.level === 2) {
          const secId = `sec-${sections.length}`;
          sections.push({ id: secId, text: block.text });
          opts.secId = secId;
        } else if (block.type === "paragraph" && !seenLeadPara) {
          opts.lead = true;
          seenLeadPara = true;
        }
        const wrap = document.createElement("div");
        wrap.innerHTML = renderProseBlock(block, this.footnotesById, opts).trim();
        if (wrap.firstElementChild) stream.appendChild(wrap.firstElementChild);
      }
    }
    this.renderToc(sections);

    if ((body.footnotes ?? []).length || meta?.credit) {
      const notesFooter = document.createElement("footer");
      notesFooter.className = "essay-footnotes essay-only";
      const items = (body.footnotes ?? []).map((f) =>
        `<li id="fn-${esc(f.id)}" value="${esc(f.id)}">
          <span>${inlineMd(f.text)}</span>
          <button type="button" class="fn-back" data-fn-target="${esc(f.id)}" title="Back to text">↩</button>
        </li>`
      ).join("");
      notesFooter.innerHTML = `
        ${items ? `<div class="label">footnotes</div><ol class="fn-list">${items}</ol>` : ""}
        ${meta?.credit ? `<p class="essay-credit label">${esc(meta.credit)}</p>` : ""}`;
      sheet.appendChild(notesFooter);
    }
  }

  renderEmbeddedCard(block) {
    const figure = document.createElement("figure");
    figure.className = "embedded-card tempus-only";
    const result = block.result;
    if (!result) {
      figure.innerHTML = `<div class="label">card · ${esc(block.card)}</div>
        <div class="errors"><span class="label">missing</span>\nCould not load card '${esc(block.card)}'.</div>`;
      return figure;
    }
    const card = result.card;
    const freq = lendLagFreq(result.payloads ?? []);
    const errors = (result.errors ?? []).filter((e) => e.level === "error");
    const warnings = (result.errors ?? []).filter((e) => e.level === "warning");
    const eyebrow = [
      card?.concepts?.[0] ? (this.conceptNames[card.concepts[0]] ?? card.concepts[0]) : "card",
      FREQ[freq],
      block.start || block.end ? `${block.start ?? "…"} → ${block.end ?? "latest"}` : null,
    ].filter(Boolean).join(" · ");
    const stats = headlineStats(result.payloads ?? [], this.theme);
    const sources = [...new Set(Object.values(card?.inputs ?? {}).map((i) => i.series?.split(":")[0]).filter(Boolean))];

    figure.innerHTML = `
      <div class="panel-head reading-card-head">
        <div class="label">${esc(eyebrow)}</div>
        <a class="open-card-link" href="#/cards/${encodeURIComponent(block.card)}">Open card →</a>
      </div>
      ${card ? `<h2 class="question">${esc(card.question)}</h2>` : ""}
      ${block.caption ? `<p class="reading-caption">${inlineMd(block.caption, this.footnotesById)}</p>` : ""}
      ${errorBlock(errors)}
      ${statRow(stats)}
      <div class="panels"></div>
      ${card?.notes ? `<p class="notes">${esc(card.notes.trim())}</p>` : ""}
      ${warnings.map((w) => `<p class="warning"><b>NOTE</b> ${esc(w.message)}</p>`).join("")}
      <div class="foot">
        ${(card?.concepts ?? []).map((c) => `<a class="chip" href="#/explore/${esc(c)}">${esc(this.conceptNames[c] ?? c)}</a>`).join("")}
        ${(card?.tags ?? []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
        ${sources.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}
      </div>`;

    this.charts.push(...drawPanels(figure.querySelector(".panels"), result.payloads ?? [], this.theme));
    return figure;
  }

  async renderIngestSheet() {
    document.title = "Add Article · tempus";
    const main = this.root.querySelector("#main");
    if (!this.canWrite()) {
      main.innerHTML = `<article class="sticker ingest-sheet">
        <div class="label">readings · read-only</div>
        <h1 class="question">Adding articles is disabled in read-only or shared guest mode.</h1>
        <div class="foot"><a class="open-card-link" href="#/readings">← Back to library</a></div>
      </article>`;
      return;
    }

    const [jobs, cfg] = await Promise.all([
      fetch("/api/ingest").then((r) => (r.ok ? r.json() : [])).catch(() => []),
      fetch("/api/ingest/config").then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
    ]);
    if (!this.activeJob && jobs[0]?.status === "running") {
      this.activeJob = jobs[0];
      this.startPolling(jobs[0].id);
    }

    const provider = cfg.ingest_provider ?? this.meta?.ingest_provider;
    const providerNotice = provider
      ? `<div class="ingest-banner is-ready"><span class="chip">LLM: ${esc(provider)}</span><span>Automatic PDF / URL / Text digestion extracts clean essay prose, searches &amp; syncs FRED series, and composes chart cards into Postgres.</span></div>`
      : `<div class="ingest-banner is-setup"><strong>LLM key not set yet</strong> — add <code>GEMINI_API_KEY</code> or <code>ANTHROPIC_API_KEY</code> to <code>.env</code> and restart <code>tempus serve</code> for automatic PDF/URL digestion. <strong>Import Markdown</strong> works immediately without a key.</div>`;

    const kindTabs = INGEST_KINDS.map((k) =>
      `<button type="button" data-ingest-kind="${k.id}" aria-pressed="${this.ingestKind === k.id}">${esc(k.label)}</button>`
    ).join("");

    let fields = "";
    if (this.ingestKind === "pdf") {
      fields = `
        <label class="ingest-field">
          <span class="label">Research PDF (up to 25 MB)</span>
          <input id="ingest-file" type="file" accept=".pdf,application/pdf" required>
        </label>
        <label class="ingest-field">
          <span class="label">Optional title override</span>
          <input id="ingest-title" type="text" placeholder="Leave blank to detect from paper">
        </label>`;
    } else if (this.ingestKind === "url") {
      fields = `
        <label class="ingest-field">
          <span class="label">Article URL</span>
          <input id="ingest-url" type="url" placeholder="https://example.com/research-note" required>
        </label>
        <label class="ingest-field">
          <span class="label">Optional title override</span>
          <input id="ingest-title" type="text" placeholder="Leave blank to detect from page">
        </label>`;
    } else if (this.ingestKind === "text") {
      fields = `
        <label class="ingest-field">
          <span class="label">Article title (optional)</span>
          <input id="ingest-title" type="text" placeholder="Headline or working title">
        </label>
        <label class="ingest-field">
          <span class="label">Paste article prose</span>
          <textarea id="ingest-text" rows="10" placeholder="Paste raw article paragraphs or notes here…" required></textarea>
        </label>`;
    } else {
      fields = `
        <label class="ingest-field">
          <span class="label">Optional reading slug (kebab-case)</span>
          <input id="ingest-slug" type="text" placeholder="my-macro-digest">
        </label>
        <label class="ingest-field">
          <span class="label">Load local .md file (optional)</span>
          <input id="ingest-md-file" type="file" accept=".md,.markdown,text/markdown">
        </label>
        <label class="ingest-field">
          <span class="label">Complete Tempus Reading Markdown (with YAML frontmatter)</span>
          <textarea id="ingest-text" rows="11" placeholder="---\nversion: 1\ntitle: ...\n---" required></textarea>
        </label>`;
    }

    const needsKey = !provider && this.ingestKind !== "markdown";
    const recentHtml = jobs.length
      ? `<section class="ingest-history">
          <div class="label">recent ingest jobs</div>
          <ul class="ingest-job-list">${jobs.slice(0, 6).map((j) => {
            const label = j.title || j.source_url || j.kind;
            const link = j.reading_id
              ? `<a class="open-card-link" href="#/readings/${encodeURIComponent(j.reading_id)}">Open →</a>`
              : `<span class="label">${esc(j.error || j.step)}</span>`;
            return `<li><span class="chip">${esc(j.kind)}</span><span class="ingest-job-title">${esc(label)}</span><span class="chip">${esc(j.status)}</span>${link}</li>`;
          }).join("")}</ul>
        </section>`
      : "";

    main.innerHTML = `
      <article class="sticker ingest-sheet">
        <div class="reading-head-bar">
          <div class="label">readings · dynamic ingestion</div>
          <a class="open-card-link" href="#/readings">← Library</a>
        </div>
        <h1 class="question">Add an article to Tempus</h1>
        <p class="notes">Articles, extracted claims, and generated chart cards are saved directly to Postgres (nothing checked into git).</p>
        ${providerNotice}
        <div id="ingest-status-wrap" aria-live="polite" role="status"></div>
        <div class="switch ingest-kind-switch" role="group" aria-label="Source format">${kindTabs}</div>
        <form id="ingest-form" class="ingest-form">
          ${fields}
          ${this.ingestError ? `<div class="errors">${esc(this.ingestError)}</div>` : ""}
          <div class="ingest-actions">
            <button id="ingest-submit" type="submit" class="go" ${needsKey || this.submitting ? "disabled" : ""}>
              ${this.submitButtonHtml()}
            </button>
          </div>
        </form>
        ${recentHtml}
      </article>`;

    this._lastJobKey = null;
    this.updateJobPanel();
    this.syncSubmitButton();

    const mdInput = main.querySelector("#ingest-md-file");
    if (mdInput) {
      mdInput.addEventListener("change", async () => {
        const f = mdInput.files?.[0];
        if (!f) return;
        const textArea = main.querySelector("#ingest-text");
        if (textArea) textArea.value = await f.text();
        const slugInput = main.querySelector("#ingest-slug");
        if (slugInput && !slugInput.value) {
          slugInput.value = f.name.replace(/\.(md|markdown)$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
        }
      });
    }

    const form = main.querySelector("#ingest-form");
    form?.addEventListener("submit", (ev) => this.submitIngest(ev));
  }

  submitButtonHtml() {
    if (this.submitting) {
      return `<progress class="loading-spinner is-sm is-inverted" aria-label="Uploading"></progress><span>Starting…</span>`;
    }
    if (this.activeJob?.status === "running") {
      return `<progress class="loading-spinner is-sm is-inverted" aria-label="Digesting"></progress><span>Digesting article…</span>`;
    }
    return this.ingestKind === "markdown"
      ? "Validate &amp; Save Reading →"
      : "Digest &amp; Add Article →";
  }

  syncSubmitButton() {
    const btn = this.root.querySelector("#ingest-submit");
    if (!btn) return;
    const provider = this.root.querySelector(".ingest-banner.is-ready");
    const needsKey = !provider && this.ingestKind !== "markdown";
    const working = this.submitting || this.activeJob?.status === "running";
    btn.disabled = Boolean(needsKey || working);
    btn.innerHTML = this.submitButtonHtml();
  }

  updateJobPanel() {
    const wrap = this.root.querySelector("#ingest-status-wrap");
    if (!wrap) return;
    if (!this.activeJob) {
      this._lastJobKey = null;
      wrap.innerHTML = "";
      return;
    }
    const job = this.activeJob;
    const jobKey = `${job.id ?? ""}|${job.status}|${job.step}|${job.title ?? ""}|${job.error ?? ""}`;
    if (jobKey === this._lastJobKey) return;
    this._lastJobKey = jobKey;

    const stepOrder = INGEST_STEPS.map((s) => s.id);
    const curIdx = stepOrder.indexOf(job.step);
    const stepMeta = INGEST_STEP_MAP[job.step] ?? INGEST_STEPS[0];
    const isWorking = job.status === "running";
    if (isWorking) this.startElapsedTimer(job.created_at);
    else this.stopElapsedTimer();

    const elapsedStr = this.jobStartedAt ? formatElapsed(Date.now() - this.jobStartedAt) : "0:00";
    const progressVal = job.status === "done" ? 5 : Math.max(0, Math.min(5, curIdx < 0 ? 0 : curIdx));

    const headerLeft = isWorking
      ? `<div class="ingest-working-main">
           <progress class="loading-spinner" aria-label="Digesting article"></progress>
           <div>
             <div class="ingest-working-title">${esc(stepMeta.label)} · ${esc(job.title || job.kind)}</div>
             <p class="ingest-step-detail">${esc(stepMeta.detail)}</p>
           </div>
         </div>`
      : `<div class="label">pipeline · ${esc(job.title || job.kind)} (${esc(job.status)})</div>`;

    const headerRight = isWorking
      ? `<span class="ingest-elapsed">${esc(elapsedStr)}</span>`
      : (job.reading_id
          ? `<a class="open-card-link" href="#/readings/${encodeURIComponent(job.reading_id)}">Open reading →</a>`
          : "");

    const stepsHtml = INGEST_STEPS.map((s, idx) => {
      let state = "pending";
      if (job.status === "done") state = "done";
      else if (job.status === "failed" && idx === Math.max(0, curIdx)) state = "failed";
      else if (curIdx > idx) state = "done";
      else if (curIdx === idx) state = "active";

      const icon = state === "active"
        ? `<progress class="loading-spinner is-sm is-inverted" aria-label="In progress"></progress>`
        : state === "done"
          ? `<span aria-hidden="true">✓</span>`
          : "";
      return `<li class="is-${state}">${icon}<span>${esc(s.label)}</span></li>`;
    }).join("");

    wrap.innerHTML = `
      <div class="ingest-progress">
        <div class="ingest-working">${headerLeft}${headerRight}</div>
        <progress class="ingest-bar" max="5" value="${progressVal}" aria-label="Pipeline step progress"></progress>
        <ol class="ingest-steps">${stepsHtml}</ol>
        ${job.error ? `<div class="errors">${esc(job.error)}</div>` : ""}
      </div>`;
  }

  async submitIngest(ev) {
    ev.preventDefault();
    if (this.submitting || this.activeJob?.status === "running") return;
    this.ingestError = null;
    const payload = { kind: this.ingestKind };
    const titleEl = this.root.querySelector("#ingest-title");
    if (titleEl?.value.trim()) payload.title = titleEl.value.trim();

    let file = null;
    if (this.ingestKind === "pdf") {
      file = this.root.querySelector("#ingest-file")?.files?.[0] ?? null;
      if (!file) {
        this.ingestError = "Choose a PDF file first.";
        await this.renderIngestSheet();
        return;
      }
      payload.filename = file.name;
    } else if (this.ingestKind === "url") {
      payload.url = this.root.querySelector("#ingest-url")?.value.trim() || "";
      if (!payload.url) {
        this.ingestError = "Enter an http:// or https:// article URL.";
        await this.renderIngestSheet();
        return;
      }
    } else {
      payload.text = this.root.querySelector("#ingest-text")?.value || "";
      if (this.ingestKind === "markdown") {
        const slug = this.root.querySelector("#ingest-slug")?.value.trim();
        if (slug) payload.slug = slug;
      }
    }

    this.submitting = true;
    this.jobStartedAt = Date.now();
    this.activeJob = {
      id: null,
      kind: this.ingestKind,
      title: payload.title || payload.filename || payload.url || "New article",
      status: "running",
      step: "queued",
      error: null,
      reading_id: null,
    };
    this.syncSubmitButton();
    this.updateJobPanel();

    try {
      if (file) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        payload.pdf_base64 = bytesToBase64(bytes);
      }
      const res = await fetch("/api/ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        this.ingestError = data.detail || `Request failed (${res.status})`;
        this.submitting = false;
        this.activeJob = null;
        this.jobStartedAt = null;
        this.stopPolling();
        await this.renderIngestSheet();
        return;
      }
      this.submitting = false;
      this.activeJob = data;
      this._lastJobKey = null;
      this.updateJobPanel();
      this.syncSubmitButton();
      if (data.status === "done" && data.reading_id) {
        this.stopPolling();
        this.jobStartedAt = null;
        await this.loadSidebar();
        location.hash = `#/readings/${encodeURIComponent(data.reading_id)}`;
      } else {
        this.startPolling(data.id);
      }
    } catch (err) {
      this.ingestError = err?.message || "Network error starting ingest";
      this.submitting = false;
      this.activeJob = null;
      this.jobStartedAt = null;
      this.stopPolling();
      await this.renderIngestSheet();
    }
  }
}
