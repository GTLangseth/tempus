# Stack

Why each tool is here, what we compared it with, and when we'd change it. This doc keeps
the stack defensible. `ARCHITECTURE.md` says what the system is. This doc says why these
tools build it. Decisions still go in `ARCHITECTURE.md` under **Decisions**; this doc
holds the reasoning and comparisons behind them.

**Last reviewed:** 2026-10-04 · **Cadence:** quarterly, or when a trigger below fires ·
Check with `mise run stack:review-due`

## Principles

1. **Judge against the vision.** Score every tool against what tempus is for: a personal
   time-series lab that collects public data and serves fast, interactive exploration.
   A future role as a cache for semantic-layer extracts is exploratory, so it's a tiebreaker,
   not a requirement. "Popular" or "what we used before" is not a reason.
2. **FOSS by default, and say so when we aren't.** Prefer OSI-licensed, self-hostable
   tools. A source-available or proprietary choice is allowed when nothing open is close.
   The register must label it as such and name the open fallback.
3. **Smallest tool that does the job.** Add a component only when a named need requires
   it. "We might need it" isn't a need. Remove tools that stop earning their place.
4. **Standard interfaces keep swaps cheap.** Depend on the Postgres wire protocol and SQL,
   OCI images, Parquet and TOML, not on a vendor's API. Switching a tool should mean
   changing config or one adapter, not a rewrite.
5. **Every choice has a revisit trigger.** Each register row says what would make us look
   again. If we can't name a trigger, we don't understand the choice.
6. **Borrow proven conventions; don't invent our own.** When a mature platform already
   solves a design problem, adopt its conventions and name the source. Examples: Grafana
   and Datadog query formulas for card expressions, Vega-Lite for chart specs, Prometheus
   labels for dimensions, Apache Ossie for the semantic layer. Invent only where nothing
   fits, and record why. Familiar conventions are easier for people to learn and for LLMs
   to write correctly.
7. **One home per definition.** Metric logic lives in one place. When tempus caches data
   from an upstream semantic layer, the definitions stay upstream.
8. **Decide fast when a choice is easy to undo; stop and flag it when it isn't.** This
   borrows Amazon's one-way and two-way doors. A **two-way door** is cheap to reverse: pick
   a sensible default, mark it *provisional* under [Open decisions](#open-decisions), and
   keep moving. A **one-way door** is expensive to reverse: a choice that spreads into
   stored data, file formats, public interfaces, or code in many places. Before walking
   through one, an agent must stop and say so. Name what would have to change to reverse
   it, offer a way to make it reversible, and wait for an explicit decision.
9. **Build seams so choices stay two-way.** Put an abstraction between tempus and every
   swappable part: the `Source` protocol for providers, a renderer adapter for each panel
   type, the engine API between the web app and Python, a `DATABASE_URL` for the store.
   Put a `version` on every file format (cards, catalog, ontology) so it can be migrated.
   Keep visual decisions (colors, type, spacing, chart styling) in shared theme tokens
   that every renderer reads, so swapping a library doesn't change the look. Add a seam
   when the second option is real or the switch is likely, not speculatively (principle 3).

## Register

License key: **OSI** = open source · **SA** = source-available · **P** = proprietary.

| Layer | Choice | License | Why | Alternatives considered | Revisit when |
|---|---|---|---|---|---|
| Time-series store | TimescaleDB (`timescaledb-ha:pg18`) | Core: OSI (Apache-2.0). Compression and continuous aggregates: SA (Timescale License) | Postgres serving layer with time-series features; see [Storage](#storage) | DuckDB, plain Postgres, ClickHouse, QuestDB, Snowflake | Tempus stays read-mostly and single-user (→ DuckDB); TSL terms change; wide scan-heavy analytics dominate (→ ClickHouse) |
| Container runtime | Docker (OCI images, Compose) | Engine: OSI. Docker Desktop: P | Standard and ubiquitous; the app only needs a `DATABASE_URL` | Podman, Colima, OrbStack (P), nerdctl | Docker Desktop licensing or resource use becomes a problem (→ Colima or Podman) |
| Language | Python 3.12 | OSI | User's preferred language; strong data ecosystem | — | A front end needs shared logic that Python can't serve |
| Packaging | uv | OSI | Fast, lockfile-based, manages Python versions | pip-tools, Poetry | — |
| Provisioning and tasks | mise | OSI | One file pins tools, env and tasks | asdf + make, just | — |
| CLI | typer + rich | OSI | Type-hinted commands; good terminal output | click, argparse | — |
| HTTP | httpx | OSI | Sync and async, modern API | requests, aiohttp | — |
| DB driver | psycopg 3 | OSI (LGPL-3.0) | COPY for bulk loads; the reference Postgres driver | asyncpg | — |
| Config | pydantic-settings | OSI | Typed settings from `.env` | python-dotenv | — |
| Catalog format | TOML via tomlkit | OSI | Reviewable in git; tomlkit keeps comments when editing | YAML | — |
| Decision model (filing, resolving phrases to series IDs, series labels, card checks) | TypeSafe Jev (hosted API), pinned model version | P | Returns calibrated probabilities for decisions from a closed set, which the thresholds need; cheap. Usage rules: `ARCHITECTURE.md` → Using Jev | Local open-weight LLM with logprobs (e.g. via llama.cpp or vLLM), embedding similarity | An open model gives comparably calibrated choices; cost or availability becomes an issue |
| Scheduler | `tempus sync --every` loop | n/a | Smallest thing that works for one source | cron or launchd, Dagster, Prefect | Several sources with dependencies, or a need for retries and alerting |
| Web front end (planned) | No-build static app (HTML + ES modules) served by the engine's FastAPI server, with hot reload | OSI | Lightest loop for quick iteration: no Node toolchain or bundler. Borrows the validate + serve loop from dbt Charts | Next.js, SvelteKit, dbt Charts, Observable Framework, Evidence | The UI needs routing, state management or components beyond plain modules (→ SvelteKit or Next.js) |

## Open decisions

Choices that are still open or only provisional. Each one says how hard it is to reverse
and which seam insulates it. Graduate an entry to the register, and to `ARCHITECTURE.md`
under **Decisions**, once it's settled. A one-way door needs an explicit decision before
any work depends on it (principle 8).

Status key: **Open** = undecided · **Provisional** = default in use, easy to change ·
**Decided** = settled but still listed while its seam is being built.

| Decision | Status | Current lean | Door | Seam that insulates it | Settle when |
|---|---|---|---|---|---|
| Card expression language | Decided | Named inputs plus formula expressions, in the style of Grafana and Datadog. A restricted subset of Python expressions, parsed with the standard library's `ast` and an allow-list (no Lark; it would only have served invented syntax) | **One-way** once many cards exist | `version:` on every card file, plus a migrator; the expression parser is the only code that reads the syntax | — |
| Card references | Decided | Explicit series always allowed; concept expansion in v0; abstract metrics (Ossie-style) later, as an addition | Two-way, because references are additive | Reference resolver in the engine | When a second source exists for the same metric |
| Panel grammar | Provisional | A closed panel catalog using Vega-Lite's encoding vocabulary (`x`, `y`, `color`, `mark`), with `type: vega` as the escape hatch | **One-way** once many cards exist | `version:` on cards; a catalog entry documents each type | Before the first ~10 cards are saved |
| Chart renderers | Provisional | ECharts only for v0 (zoom and crosshair tooltips are built in); Vega-Lite for the escape hatch; add Lightweight Charts only if market panels need it | Two-way | One renderer adapter per panel type, plus shared theme tokens (`theme.json`) | After the first web slice. AG Charts Enterprise is the alternative if a single paid family is preferred |
| Table renderers | Provisional | AG Grid Community for flat tables; TanStack Table for trees and grouping | Two-way | Table adapter, plus theme tokens | First web slice. AG Grid Enterprise would add tree data, at a cost |
| Card storage | Decided | YAML files in git (`cards/*.yaml`) are the source of truth, loaded into a `cards` table as an index, like the ontology. Open-source contributors can bootstrap from git without database access. A customer-facing deployment can later make Postgres the source of truth | **One-way** for workflows and tooling | Every surface (CLI, MCP, web) reads and saves through one card repository interface in the engine, so going database-first changes only that interface's implementation | — |
| Engine API | Provisional | FastAPI inside the tempus package (`tempus serve`): concepts, cards, panel payloads, and a server-sent event stream for hot reload. Reverses the "web reads Postgres directly" decision | Two-way while the API surface is small | The OpenAPI schema FastAPI generates | When slice 1 is built |
| Web framework | Provisional | No-build static app served by the engine (see register) | Two-way while the web app only calls the engine API | The engine API and the panel payload | When the UI outgrows plain modules |
| Playground notebooks | Provisional | marimo | Two-way | Notebooks import the engine; no logic lives only in notebooks | When the playground slice starts |
| Point-in-time data (vintages) | Open | — (e.g. FRED's ALFRED) | **One-way**: adds a time dimension to `observations`, and backfilling later is costly | — | Before any signal backtesting |
| Prediction-market sources | Open | — (Kalshi, Polymarket) | Two-way | `Source` protocol; contract expiry may need metadata fields | When the widen slice starts |
| Code-intelligence MCP for agents | Open | Not adopted. Candidate: [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) (OSI, MIT; local tree-sitter code graph in SQLite with call graphs, cross-service links and change impact; single binary, no network). At ~1.5k lines of Python today, `grep` plus the docs cover navigation; the extra tool descriptions in every session aren't worth it yet. If adopted, don't use its `manage_adr` tool: decisions stay in `ARCHITECTURE.md` | Two-way: config only, and no code depends on it | MCP config in `.claude/` only; never commit its graph snapshot | After the first web slice (engine API + static app) lands and the code passes ~10k lines across Python, JS and SQL; or when tracing usages across layers (e.g. "what reads `series_concepts`?") or gauging how far a refactor reaches becomes a recurring cost |

## Inspirations

Proven projects and ideas we borrow from (principle 6), and those we evaluated. Each entry
says what we take, what we leave, and where it shows up in tempus. Add an entry whenever a
design leans on an outside pattern, so the reasoning stays traceable. The **status** column
records whether we only borrow ideas, actually depend on the project, or are still
evaluating it.

| Source | License | Status | What we take | What we leave, and why | Where in tempus |
|---|---|---|---|---|---|
| [dbt Charts](https://docs.getdbt.com/guides/dbt-charts) | Apache-2.0 | Ideas; evaluated as a renderer | A typed YAML surface over a grammar (raw Vega-Lite rejected); a fixed chart registry where an unknown type fails validation; "did you mean" errors; channel names (`x`, `y`, `color`, `size`, `shape`); `rows`/`cols` layout for boards; the validate + serve-with-hot-reload loop | The tool itself: it's coupled to dbt (profiles, adapter), renders only through Vega-Lite (weak time-series interaction), and documents no custom chart types. Revisit if it gains a plugin API and non-dbt sources | Panel catalog, `tempus card check`, `tempus serve`, dashboards |
| [Vega-Lite](https://vega.github.io/vega-lite/) | BSD-3 | Ideas, plus escape hatch | Encoding vocabulary (`x`, `y`, `color`, `mark`); grammar-of-graphics thinking | Full specs as the default: verbose, and the results look inconsistent | Panel grammar, `type: vega` |
| Grafana | AGPL-3.0 | Ideas | Panel types as a closed set; named queries combined by expressions | The product: we're building a lab, not a monitoring stack | Panel catalog, card `inputs` and `formulas` |
| Datadog query formulas | Proprietary (product) | Ideas | Formulas over named queries; the term `formulas`; `fill()`; frequency handling made explicit | — | Card `formulas`, function names |
| PromQL / Prometheus | Apache-2.0 | Ideas | Functions apply to each member of a set (vector semantics); aggregation across a set; labels as dimensions | Range-vector syntax `[5m]` and label selectors (for now; they would trigger a real grammar such as Lark) | Concept sets in cards, the medium schema pattern |
| pandas | BSD-3 | Ideas (names) | Function names: `resample`, `shift`, `pct_change`, `rolling`, `ewm`, `diff` | pandas as the engine (Polars is faster and stricter) | Function catalog |
| statsmodels | BSD-3 | Ideas, plus a likely dependency | `ccf(…, nlags=)` naming; later, the forecasting methods (ETS, ARIMA, STL) | — | `lagcorr`, playground |
| [Apache Ossie](https://github.com/apache/ossie/blob/main/core-spec/spec.md) | Apache-2.0 | Ideas (future) | The split between datasets, fields and metrics; `is_time` for time dimensions; vendor extensions for what the spec omits | Not needed until abstract metrics or warehouse sources exist | Future `{ metric: … }` references and the semantic source |
| TimescaleDB schema guidance | Apache-2.0 docs | Ideas | Narrow, medium and wide table patterns | — | `observations`, the [Storage](#storage) section |
| Amazon's one-way and two-way doors | — | Ideas | Decide fast on reversible choices; slow down on irreversible ones | — | Principle 8, Open decisions |
| Cleveland & McGill (graphical perception) | — | Ideas | Position beats length, which beats angle and area; caution with dual axes | — | Panel catalog entries |
| [Entire](https://docs.entire.io/) | CLI: MIT | Evaluated, not adopted | Commits paired with the agent session behind them (checkpoints), for tracing code back to why | Adopting now: a platform to run for a solo project whose rationale already lives in curated docs. Revisit when other contributors or several parallel agents work in the repo | Decision recall |
| claude-mem | Apache-2.0 | Evaluated, removed | — | Memory is per-machine and not shareable; compression costs LLM calls; its hooks timed out and slowed every command in practice. The curated docs plus native Claude memory cover recall | Decision recall |
| Jevbox | — | Ideas | Top-down filing, one choice per level, with a "none of these" option | — | Ontology filing |

## Storage

### Schema patterns

TimescaleDB doesn't prescribe a schema; a hypertable is a Postgres table partitioned on
time. There are three common patterns:

- **Narrow (what tempus uses):** `(source, series_id, ts) → value`, with metadata in
  `series`. Best for many unrelated series that update independently and have sparse,
  mixed frequencies, which is exactly what FRED is.
- **Medium:** narrow rows plus labels, e.g. `series.dimensions jsonb` such as
  `{"region": "EMEA"}`. This is the Prometheus-style model, and the natural extension if
  tempus caches slices of high-dimensional data. Materialize only the slices you query,
  never the full cross-product of dimensions.
- **Wide:** `(ts, entity, dim1…dimN, measure1…measureN)`, the shape of a Snowflake fact
  table. Use a separate hypertable compressed with `segmentby` on the dimension columns,
  and only when you need ad hoc slicing across many dimensions.

### TimescaleDB vs. DuckDB vs. Snowflake

TimescaleDB is Postgres as an always-on server, plus time-series machinery. Its advantage is
strongest for the narrow pattern and smallest for the wide one.

| Capability | TimescaleDB | DuckDB | Snowflake |
|---|---|---|---|
| Fast single-series reads (ms) | B-tree on `(source, series_id, ts)` | Fine at small scale with min/max zone maps; no real indexes | 100 ms to seconds, plus warehouse wake-up and a 60s minimum bill |
| Many concurrent readers and writers | Yes | One writer per file, in-process | Yes, but each query costs compute |
| Row-level upserts and transactional reconcile | Cheap | Works, but built for bulk loads | `MERGE` rewrites micro-partitions |
| Incrementally refreshed rollups | Continuous aggregates, including real-time ones | None | Dynamic tables or materialized views, paid in credits |
| Hot and cold storage | Row storage for recent chunks, compressed columnar for older ones | All columnar | All columnar |
| Retention | Dropping a whole old time partition is a cheap metadata operation | `DELETE` | `DELETE` plus Time Travel storage |
| Time-series SQL | `time_bucket`, gap-filling, `locf`, `interpolate`, `first`/`last` | `time_bucket`, `ASOF JOIN` | `TIME_SLICE`, `ASOF JOIN` |
| Ecosystem | Anything that speaks Postgres: Grafana, PostgREST, ORMs | Embedded library or a file | BI tools |

**By pattern:**
- Narrow: TimescaleDB fits best (index range scans, cheap reconcile).
- Medium: same benefits, plus JSONB and GIN label matching; a modest edge.
- Wide: DuckDB and Snowflake match or beat TimescaleDB on big scans. TimescaleDB wins only
  if you also need continuous aggregates, streaming appends with concurrent reads, or an
  app backend.

**The honest test.** At today's size (about 19,000 rows), DuckDB would do everything with
no server. TimescaleDB earns its place once tempus is a **serving layer**: the web front
end reading concurrently, regular incremental syncs, continuous aggregates, retention, and
Postgres-native tools. If tempus stays a read-mostly personal explorer, DuckDB over Parquet
is the simpler choice, and the register's revisit trigger says to switch.

### Two domains: analytical warehouse vs. time-series serving

| | Warehouse + semantic layer (e.g. Snowflake) | tempus (TimescaleDB) |
|---|---|---|
| Role | System of record; owns metric definitions | Materialized cache that serves data |
| Question shape | Any metric across any dimensions; exploratory | Known metric and slice over time; asked repeatedly |
| Schema | Wide fact tables | Narrow or medium series |

A question that needs a new combination of dimensions goes upstream. A question asked every
day, or one that mixes internal metrics with public series, goes to tempus. Cached series
must record their provenance (model, version, metric, query-spec hash) so values can be
traced back and re-synced when a definition changes. Non-additive metrics (distinct counts,
ratios) can't be rolled up from finer cached grains. Cache each grain you query, or store
the additive parts.

## Review

Run a review quarterly, or as soon as a trigger in the register fires. The goal is to keep
each choice defensible against the vision and the current FOSS landscape. Keeping a tool
is a valid outcome, as long as it's justified fresh.

### Procedure

1. **Re-read the vision** in `ARCHITECTURE.md`. Note any drift since the last review: new
   uses, new scale, new users.
2. **Check the triggers.** For each register row, has its revisit trigger fired?
   Walk **Open decisions**: settle anything whose "settle when" has arrived, and confirm
   no one-way door was passed without an explicit decision.
3. **Scan the landscape** (and refresh **Inspirations**) for each layer that has more than trivial impact: storage,
   scheduler, filing, front end. Look for new or rising FOSS projects, license changes
   (relicensing is the main risk to principle 2), and project health (releases,
   maintainers, open issues).
4. **Score the challengers** against the incumbent on five criteria, each from 1 to 5:
   fit to the vision, maturity and community health, license, operating cost, and the cost
   to migrate. A challenger needs a clear margin, not a tie, to justify the migration
   cost.
5. **Record the outcome.** Add a dated entry to the review log below. Update register rows
   whose facts changed. Any keep-or-switch call that alters the system goes in
   `ARCHITECTURE.md` under **Decisions**. Update **Last reviewed** at the top of this doc.

A Claude session can run this end to end. Ask it to "run the stack review in
`docs/STACK.md`".

### Review log

Newest first.

- **2026-10-04 · Baseline.** Created the register and the storage comparison. Flagged
  two departures from principle 2. First, TimescaleDB's compression and continuous
  aggregates are source-available (Timescale License): free to self-host, but not OSI.
  The open fallbacks are plain Postgres with native partitioning, or DuckDB. Second, Jev
  is a proprietary hosted API, with a local open-weight model as the fallback. Both are
  kept for now.
