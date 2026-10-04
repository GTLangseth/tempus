# Architecture

The high-level blueprint for tempus. Update it **in the same change** as anything that alters
the shape of the system: a new component, a new data source, a schema change, or a reversed
decision. Implementation detail belongs in code and the README, not here.

## Vision

A personal time-series lab. Collect public data (economic data first, from FRED) into one
TimescaleDB database. Then build beautiful, interactive interfaces for exploring that data
and learning from it. The longer-range vision lives in [`SOUL.md`](SOUL.md): cards,
an LLM-friendly card language, prediction markets, signals, and a playground for
learning forecasting.

## Principles

Tools are chosen to fit the job, judged against this vision: FOSS by default, the smallest
tool that does the job, standard interfaces so swaps stay cheap, proven conventions
(Grafana, Datadog, Vega-Lite, Ossie) borrowed instead of invented, and a revisit trigger
for every choice. Easy-to-undo choices are made fast and logged as provisional. Hard-to-undo
ones (one-way doors) are flagged and decided explicitly, and seams such as adapters,
versioned file formats and theme tokens keep as many choices as possible easy to undo. The full principles, the register of tools with the alternatives considered,
and the quarterly review procedure live in [`STACK.md`](STACK.md).

## Components

| Component | Status | Lives in | Role |
|---|---|---|---|
| Database | ✅ built | `docker-compose.yml`, `src/tempus/schema.sql` | TimescaleDB (`timescaledb-ha:pg18`) in Docker; the single store everything reads from |
| Sources | ✅ FRED | `src/tempus/sources/` | One adapter per provider behind the `Source` protocol (`fetch_meta`, `fetch_observations`) |
| Sync engine | ✅ built | `src/tempus/sync.py` | Reconciles a source's series into the database and logs each attempt to `sync_runs` |
| Catalog | ✅ built | `catalog.toml` | Declarative, git-tracked list of the series to keep synced |
| Snapshot import | ✅ built | `data/fred/series-snapshot.json`, `src/tempus/snapshot.py` | `tempus catalog import-snapshot`: metadata for ~2,000 popular FRED series into `series`, no observations, so the tree has something to browse before anything is synced |
| Ontology | ✅ built | `ontology/concepts.yaml`, `src/tempus/ontology.py` | Git-tracked concept tree of *what is measured* (174 concepts, 3–4 levels); mirrored into the `concepts` table |
| Draft placements | 🟡 temporary | `ontology/placements.draft.yaml` | Claude-written homes for the snapshot series, loaded as method `draft`; replaced by Jev filing (see Decisions) |
| Filing | ✅ built | `src/tempus/filing.py`, `src/tempus/jev.py` | Places series in the tree by walking it top-down with TypeSafe's Jev; pins in `catalog.toml` override; drafts are re-decided |
| Navigation | ✅ built | `src/tempus/navigate.py` | Phrase → concepts → series by beam search down the tree (Jev, or a labeled lexical fallback with no key): `/api/navigate`, `tempus resolve` |
| CLI | ✅ built | `src/tempus/cli.py` | `tempus` commands for data actions: sync, catalog, inspect, search, resolve |
| Repo tooling | ✅ built | `mise.toml` | Pins tools (Python, uv, Node) and runs repo tasks (setup, db, test, lint) |
| Home base | 🟡 runbook ready | `docs/HOMEBASE.md`, `scripts/homebase/` | Mac mini on Tailscale: real DB and keys, `tempus serve` under launchd published with `tailscale serve`, SSH + tmux for agents from the phone (`mise run homebase:setup`) |
| Scheduler | 🟡 minimal | `tempus sync --every 6h` | A blocking loop for now; see open questions |
| Cards | ✅ built | `cards/`, `src/tempus/cards/` | Card language v1 (inputs, formulas, panels), static checks, Polars engine, panel payloads |
| App | ✅ built | `src/tempus/app.py`, `src/tempus/explore.py`, `src/tempus/web/` | `tempus serve`: FastAPI engine API + no-build static front end (ECharts, three.js), hot reload |
| Web front end | ✅ explore, series and cards views | see App | Explore (default): the concept tree in 3D with natural-language search, or an outline on small screens. Series pages: an implicit single-series card. Cards: saved cards, hot-reloaded |
| Design system | ✅ adopted | `docs/DESIGN.md`, `docs/design/` | Visual tokens and rules for every surface (design.md format); `docs/design/` holds the reference candidates, a static preview and a playground for choosing options. `src/tempus/web/theme.json` mirrors its tokens |

## Data flow

```
catalog.toml ──▶ tempus sync ──▶ Source adapter ──HTTP──▶ provider API (FRED, …)
                     │                       ▲
data/fred/series-snapshot.json               │ sync on first open of a series page (FRED_API_KEY)
  ──▶ tempus catalog import-snapshot (metadata only)
                     ▼
     TimescaleDB: series · observations (hypertable) · sync_runs
                     ▲    concepts · series_concepts (manual > auto > draft)
                     │
ontology/concepts.yaml ─────┐
ontology/placements.draft.yaml ──▶ tempus ontology load
                            └──▶ tempus ontology file ──▶ Jev (TypeSafe API)
                     │
                     ▼
cards/*.yaml ──▶ card engine (check · resolve · evaluate · render) ──▶ panel payloads ──▶ tempus serve ──▶ browser
                                                                        /api/explore · /api/navigate · /api/series
```

## Data model

- **`series`** has one row per `(source, series_id)`: metadata, plus `upstream_updated_at`
  (when the provider last revised the series) and `last_synced_at`. Rows from the snapshot
  import have both NULL and no observations until synced; the snapshot's own fields
  (popularity, coverage, short codes, its `last_updated`) sit in `extra`.
- **`observations`** maps `(source, series_id, ts)` to `value`. It's a hypertable on `ts`.
  `value` is NULL where the provider reports a missing point.
- **`sync_runs`** is an append-only log of sync attempts and their outcomes.
- **`concepts`** mirrors the YAML tree. Its `id` is the dotted path (`labor.unemployment`).
- **`series_concepts`** holds one placement per series: `manual` (a pin), `auto` (with the
  lowest probability along the path, plus a trail of each level's decision) or `draft` (from
  `ontology/placements.draft.yaml`; the trail records `low` when it was a judgment call).
  Precedence is manual > auto > draft. A NULL concept means nothing fit at the top level.
- **`cards`** is an index of `cards/*.yaml` (the files are the source of truth): id, title,
  question, tags, concepts, the parsed spec as `jsonb`, and a file hash. Rebuilt by
  `tempus card load`.

Each series is identified by the pair `(source, series_id)`, so the same ID can exist at
two different providers without colliding.

## Using Jev in agentic workflows

TypeSafe's Jev returns typed decisions with calibrated probabilities: a **choice** among up
to 255 options, a **score** on an ordered scale of 2–10 levels, or a yes/no probability.
It doesn't generate text, and it costs very little ($0.042 per million input tokens).
Tempus uses it wherever an agent must pick from a closed set and the decision should be
fast, calibrated and on record.

### Who does what

| Work | Tool | Examples |
|---|---|---|
| **Decide** among a closed set | Jev | where a series is filed, which series a phrase means, which card operation fits, is this card valid |
| **Generate** open-ended text or structure | Claude | branch names, drafting cards, explanations, pulling out a threshold or date |
| **Compute** anything numeric | Code / SQL | transforms, correlations, choosing a forecasting method, detecting regimes from data |

If a rule or metadata already answers the question (e.g. FRED's `units` field), parse it
instead of asking a model. When a workflow needs both, Jev routes or constrains first and
Claude generates only inside the decided slot. That's faster and cheaper, and Claude can't
invent options outside the vocabulary.

### Making decisions reproducible

Jev is calibrated but not exactly repeatable. Five identical calls on the 30-year mortgage
rate (MORTGAGE30US) gave `rates` probabilities of 0.74–0.79 against `housing` at
0.21–0.26. The top choice was stable, but the probability drifted by about ±0.03.
Reproducibility therefore comes from how tempus uses Jev, not from the model:

1. **Pin the model version.** Use a fixed version (e.g. `jev-1.13.0`), not `jev-latest`.
   Upgrading is a deliberate change, followed by re-deciding and reviewing what moved.
2. **Record decisions, don't re-ask.** Each decision is stored with a hash of the exact
   input (state, question, options, model version). The same input returns the recorded
   answer without an API call. Placements in `series_concepts` already work this way.
3. **Commit only to clear decisions.** Thresholds (filing: probability ≥ 0.65 and a lead
   of ≥ 0.2 over the runner-up) decide whether to act. A result within about ±0.05 of a
   threshold is marked **needs review** instead of being accepted or rejected silently.
4. **Keep the evidence.** Every recorded decision keeps its ranked probabilities (the
   `trail`), so a person can see why and override with a pin.

### Where it's used

By the horizon in [`SOUL.md`](SOUL.md) where it first matters. Status: ✅ built ·
⬜ planned.

| Use | Primitive | Horizon | Status | Why Jev |
|---|---|---|---|---|
| **Filing:** place series in the concept tree | choice per level | Explore | ✅ | Calibrated thresholds; decisions recorded, with pins to override |
| **Ontology upkeep:** Claude proposes a branch; Jev checks it's needed and distinct from its siblings. When the tree changes, re-ask only the series placed near the changed branch | yes/no, choice | Explore | ⬜ | Completes the Jevbox pattern without trusting generated branches blindly |
| **Ranking for deep dives:** score FRED search results by "is this the headline series for this concept?" | score | Explore | ⬜ | Turns hundreds of search hits into a short list worth adding |
| **Series labels:** how each series is measured (level, % change, rate, index), real vs. nominal, geography | choice | Explore → Cards | ⬜ | Cards need these to transform safely, e.g. no YoY change of a series that's already a YoY change. Recorded once per series |
| **Resolving phrases to series IDs:** "crude" → `prices.commodities.energy` → `DCOILWTICO` | choice per level (beam search) | Explore → Converse | ✅ | Removes invented series IDs, the most likely failure when an LLM drafts a card. Every reference exists and has a probability. Powers the explore search and `tempus resolve`; decisions aren't recorded yet (SPA-16) |
| **Choosing the card operation:** question → compare, lead-lag, ratio, rebase, forecast, find precedents | choice | Cards → Converse | ⬜ | Constrains Claude to the card vocabulary; it fills in the slots for one operation |
| **Checking cards before they run:** does it answer the question? is the transform valid for these units? are the series comparable? | yes/no | Cards | ⬜ | Semantic validation next to schema validation; cheap enough for every draft |
| **Picking saved cards for a question:** rank the card library for a question | score | Converse | ⬜ | Jevbox's search, applied to cards instead of documents |
| **Prediction markets:** map each market to the indicator it bets on (e.g. "CPI YoY > 3% in March" → `CPIAUCSL`) | choice | Widen | ⬜ | Classifying thousands of markets into a closed set of targets; Claude extracts the threshold and date |
| **Matching series across sources:** is series A at one source the same concept as series B at another? | yes/no | Widen | ⬜ | Avoids duplicate concepts as sources multiply |
| **Text into time series:** score FOMC statements hawkish to dovish; label historical episodes for finding precedents | score, choice | Signal | ⬜ | Recorded, consistent scores become new series. See the caveat below |

### Not for

- **Numbers.** Correlations, transforms, choosing a forecasting method (statistical tests
  for trend and seasonality do this better) and detecting regimes all stay in code, where
  they're exactly reproducible and explainable ("understanding over automation").
- **Generation.** Names, drafts and explanations are Claude's job.
- **Historical text as backtest input without care.** A model scoring a 2007 statement may
  know what happened in 2008. Scores of historical text are useful descriptions, but they
  violate "honest with time" as backtest inputs unless that leak is controlled for.

## Decisions

Newest first. Each entry: what we chose, and why.

- **2026-10-04 · A Mac mini is the home base, reached over Tailscale.** The real database,
  API keys and long-running agents live on one always-on machine at home. Everything binds
  to localhost (the app, and now Postgres in `docker-compose.yml`). Tailscale publishes the
  app to the tailnet only (`tailscale serve`), SSH is the way in, and other devices reach the
  database through an SSH tunnel. Cloud routines keep doing code-only work. `tailscaled` runs
  as a system daemon, so it's up before login, and Colima replaces Docker Desktop there.
  Runbook: `docs/HOMEBASE.md`.
- **2026-10-04 · Draft placements: a temporary, user-approved exception to "Jev decides".**
  To make the explore view useful before any Jev filing has run, a Claude session placed
  all 1,965 snapshot series in the tree by hand-reviewed rules, in batches, with 29 judgment
  calls flagged `low`. They live in git as `ontology/placements.draft.yaml` (versioned,
  concept → `source:series_id` refs) and load as method `draft`. Drafts never outrank a pin
  or a Jev decision, `tempus ontology file` treats them as unfiled, and a draft dropped from
  the file is deleted on the next load. **How they get replaced:** run `tempus ontology
  file` (with `TYPESAFE_API_KEY`); each draft becomes an `auto` placement with real
  probabilities, or a pin where a person overrides. Once no `draft` rows remain, delete the
  file. Until then the UI labels every draft as such. `schema.sql` widens the `method`
  check constraint with an idempotent drop-and-re-add, so existing databases upgrade on
  `tempus db init`.
- **2026-10-04 · The ontology covers a FRED metadata snapshot, not only synced series**
  (supersedes "organizes only synced series" below). `tempus catalog import-snapshot` loads
  metadata for ~2,000 popular US series into `series` without observations; `catalog.toml`
  stays the list `sync` keeps fresh. Series pages sync one on first open when
  `FRED_API_KEY` is set. The tree grew to 174 concepts with three new top-level areas
  (Banking & Credit, Business Cycle & Outlook, Population & Society). Exchange rates split
  into advanced and emerging currencies: a grouping of *what is priced*, not a geography
  facet, but close to the line, so revisit if it starts to sprawl.
- **2026-10-04 · Explore is a 3D tree with a deterministic layout, in plain three.js.**
  Concepts sit on nested spherical caps by depth and each concept's series form a seed head
  beyond it ("dandelion"). No force simulation: the same tree always lands in the same place,
  so camera flights and screenshots are stable, and ~2,000 seeds render as instanced meshes.
  three.js is loaded from a pinned CDN through an import map (no build step), only by the 3D
  view; an outline view covers small screens and missing WebGL. See `STACK.md` for the
  alternatives (3d-force-graph, deck.gl, a 2D radial tree).
- **2026-10-04 · Natural-language navigation is a beam search down the tree.** At each level
  the chooser picks among the children plus "none of these"; up to four routes survive,
  ranked by the geometric mean of their decision probabilities (single-child steps don't
  count, a child must reach 0.2 and beat "none of these"). The series under each reached
  concept are then ranked per route. With `TYPESAFE_API_KEY` the chooser is Jev (still
  `jev-latest`; pinning is SPA-15); without one, or if Jev fails, a BM25 lexical chooser
  stands in and every response names which one answered. Decisions aren't recorded yet
  (SPA-16).
- **2026-10-04 · A series page is an implicit card.** `/api/series/{source}/{id}` builds a
  stat-plus-timeseries card in memory and runs it through the card engine (`run_spec`), so
  series pages and saved cards share payloads and adapters, and nothing is written to
  `cards/`.

- **2026-10-04 · The visual language is "Neon Notebook", kept in `docs/DESIGN.md`.** A calm
  cream graph-paper canvas with ink text and sticker-style cards, so that a few hot neon
  numbers and chart lines draw the eye. It combines two reference styles (a cream
  sketchbook and a lab-label monospace voice) and was tuned in a playground. The file uses
  Google's design.md format: tokens in YAML front matter, reasons in prose, linted by
  `mise run design:lint`. That lets agents and `theme.json` (the app's tokens) read one source.
  Because the hotter neons trade away contrast, only pink and violet may set type; cyan,
  orange and green are limited to labeled chart marks.
- **2026-10-04 · Cards are evaluated in a Python engine; the web app reads through it.**
  This reverses "the web app reads TimescaleDB directly": cards are the shared logic that
  decision said would trigger a revisit. `tempus serve` (FastAPI) returns renderer-neutral
  panel payloads; the browser never touches formulas or SQL. Spec:
  `docs/specs/2026-10-04-cards-slice-1-design.md`.
- **2026-10-04 · Derived data lives in card formulas** (resolves the open question).
  Transforms (YoY, resampling, rolling, lag correlation) are catalog functions evaluated
  with Polars. Functions may later push down to SQL without changing the language.

- **2026-10-04 · Jev decides, Claude generates, code computes.** In agentic workflows, Jev
  handles every choice from a closed set: filing, resolving phrases to series IDs, choosing
  card operations, series labels, card checks and market mapping. Claude generates only
  inside the slot Jev decided, and numbers stay in code. A repeat test showed about ±0.03
  drift in probabilities, so reproducibility comes from pinning the model version,
  recording decisions by input hash, and a review band around each threshold. See
  [Using Jev](#using-jev-in-agentic-workflows). This also resolves the open question on new
  branches: Claude proposes, Jev checks, a person reviews the YAML diff.

- **2026-10-04 · Stack register with quarterly review.** `docs/STACK.md` records each tool,
  its license, the alternatives we compared it with, and what would make us revisit it.
  A review runs quarterly or when a trigger fires (`mise run stack:review-due`). Choices
  should stay defensible against the vision, not just persist by inertia.
- **2026-10-04 · TimescaleDB is the serving layer, with the narrow schema.** Series that
  are unrelated and sparse at mixed frequencies suit `(source, series_id, ts) → value`.
  TimescaleDB earns its place over DuckDB through concurrent serving, cheap row-level
  reconcile, continuous aggregates and the Postgres ecosystem. It does not win on wide
  analytical scans, which belong in a warehouse. If tempus stays read-mostly and
  single-user, DuckDB is the revisit trigger. Comparison: `STACK.md` → Storage.

- **2026-10-04 · Commodities and the CPI components they feed are tracked as pairs.**
  The first deep dive covered both ends of each chain: commodity prices (energy,
  agriculture, metals) and the consumer price indexes they feed (energy, gasoline, food
  at home, coffee). Interplay between them is the point. We dropped a "Commodity Indexes"
  branch: "index" describes how a series is measured, not what it measures, so broad
  indexes stay at `prices.commodities` (a facet, consistent with the decision below).

- **2026-10-04 · The ontology organizes only synced series for now (superseded the same day
  by the snapshot decision above).** It started with the
  series in `catalog.toml`, then deepened by branch. Indexing FRED-wide metadata for
  discovery is deferred: it costs many API calls and filing money at that scale.
- **2026-10-04 · Jevbox-style filing with Jev.** The walk goes top-down, one choice per level
  among the children plus "none of these". It descends only when the top choice has
  probability ≥ 0.65 and leads the runner-up by ≥ 0.2; otherwise the series stays at the
  deepest concept it reached. Jev returns calibrated probabilities rather than text, which
  is what these thresholds need. It's also cheap ($0.042/M input tokens). Generative work
  (drafting branches) is done by hand or in a Claude session as edits to the YAML, and
  reviewed in git.
- **2026-10-04 · The tree is about what is measured; everything else is a facet.**
  Geography, frequency, seasonal adjustment and units stay as series attributes, not tree
  levels. This avoids FRED's own tree, which repeats the same concept for every state and
  county. The concept tree is drafted, not derived from FRED's categories.

- **2026-10-04 · Hand-written architecture doc; no generated wiki yet.** Intent and rationale
  can't be derived from code. Revisit a generated wiki (e.g. OpenWiki) once the codebase is
  large enough to warrant one.
- **2026-10-03 · The web app reads TimescaleDB directly (planned; reversed 2026-10-04, see
  above).** Next.js server code
  queries Postgres, with no Python API in between. That means one less service while the
  Python side does nothing beyond ingestion. Revisit if we need shared business logic
  (e.g. derived series computed in Python).
- **2026-10-03 · Two command surfaces with separate jobs.** `mise` tasks run the repo
  (install, services, tests, lint). The `tempus` CLI acts on data. Neither duplicates the other.
- **2026-10-03 · Full reconcile when upstream changes; skip when it hasn't.** Economic data
  is revised often, so appending only new points would leave stale values in place. Each
  sync checks the provider's `last_updated`. If it's unchanged, the sync skips. Otherwise it
  downloads the whole series, then upserts changed points and deletes removed points in one
  transaction. FRED series are small enough for this to be cheap.
- **2026-10-03 · `catalog.toml` holds the list of synced series, not the database.** The
  list is reviewable in git and survives a database reset.
- **2026-10-03 · Self-hosted TimescaleDB in Docker rather than Tiger Cloud.** Local, free,
  and easy to reset. The connection is a plain `DATABASE_URL`, so moving to Tiger Cloud
  later only changes config.
- **2026-10-03 · Python with uv, typer, psycopg 3 and httpx; mise for provisioning.** These
  are the user's preferred tools. psycopg 3 provides COPY, which loads data in bulk.

## Open questions

- **Semantic-layer source:** should tempus cache slices of upstream warehouse metrics
  (e.g. Snowflake, defined in an Apache Ossie semantic model) as series? The sketch: a
  `semantic` source whose catalog entry is a query spec (model, metric, time dimension,
  grain, dimension slice), a `dimensions jsonb` column on `series`, and a reconcile scoped
  to a recent window instead of the whole series. Open: how freshness is detected without
  a `last_updated` stamp, and how non-additive metrics are cached. See `STACK.md` → Two
  domains.

- **Retiring drafts:** after `tempus ontology file` has run, should drafts that Jev left at a
  shallower concept (below threshold) keep any influence, e.g. as a hint shown in review?
  Today Jev's answer simply replaces the draft.

- **Cross-links:** some series belong to two concepts (real consumer spending is both a
  GDP component and household spending; mortgage rates are both rates and housing). Add
  typed links between concepts (`component_of`, `deflated_by`, `leads`) or secondary
  placements once the tree is stable. The first pass-through check (YoY correlations,
  2000 onward) suggests *measured* links with a lag: crude oil → CPI gasoline (r 0.88 at
  1 month), arabica coffee → CPI coffee (0.82 at 7 months), the global food index → CPI
  food at home (0.72 at 9 months), and crude oil → core CPI (only 0.30). Open questions:
  should links be declared in YAML, computed, or both? And at series level or concept level?

- **Scheduling:** is a long-running `tempus sync --every` loop enough? The alternatives are
  cron or launchd, or an orchestrator (Dagster or Prefect) once there are several sources
  with dependencies between them.
- **Next data sources:** BLS, BEA, Treasury, World Bank, or scraped sites? Scraped sites
  may not fit the `Source` protocol's "metadata plus full series" shape.
- **Revision history:** we currently keep only the latest values. Do we want vintages
  (what the data said on date X), e.g. via FRED's ALFRED vintage data?
