# Architecture

The high-level blueprint for tempus. Update it **in the same change** as anything that alters
the shape of the system: a new component, a new data source, a schema change, or a reversed
decision. Implementation detail belongs in code and the README, not here.

## Vision

A personal time-series lab. Collect public data (economic data first, from FRED) into one
TimescaleDB database. Then build beautiful, interactive interfaces for exploring that data
and learning from it.

## Components

| Component | Status | Lives in | Role |
|---|---|---|---|
| Database | ✅ built | `docker-compose.yml`, `src/tempus/schema.sql` | TimescaleDB (`timescaledb-ha:pg18`) in Docker; the single store everything reads from |
| Sources | ✅ FRED | `src/tempus/sources/` | One adapter per provider behind the `Source` protocol (`fetch_meta`, `fetch_observations`) |
| Sync engine | ✅ built | `src/tempus/sync.py` | Reconciles a source's series into the database and logs each attempt to `sync_runs` |
| Catalog | ✅ built | `catalog.toml` | Declarative, git-tracked list of the series to keep synced |
| Ontology | ✅ built | `ontology/concepts.yaml`, `src/tempus/ontology.py` | Git-tracked concept tree of *what is measured*; mirrored into the `concepts` table |
| Filing | ✅ built | `src/tempus/filing.py`, `src/tempus/jev.py` | Places series in the tree by walking it top-down with TypeSafe's Jev; pins in `catalog.toml` override |
| CLI | ✅ built | `src/tempus/cli.py` | `tempus` commands for data actions: sync, catalog, inspect, search |
| Repo tooling | ✅ built | `mise.toml` | Pins tools (Python, uv, Node) and runs repo tasks (setup, db, test, lint) |
| Scheduler | 🟡 minimal | `tempus sync --every 6h` | A blocking loop for now; see open questions |
| Web front end | ⬜ planned | `web/` | Next.js app for exploring and visualizing series |

## Data flow

```
catalog.toml ──▶ tempus sync ──▶ Source adapter ──HTTP──▶ provider API (FRED, …)
                     │
                     ▼
     TimescaleDB: series · observations (hypertable) · sync_runs
                     ▲    concepts · series_concepts
                     │
ontology/concepts.yaml ──▶ tempus ontology file ──▶ Jev (TypeSafe API)
                     │
                     ▼
     web/ (Next.js, planned) ── reads directly with SQL
```

## Data model

- **`series`** has one row per `(source, series_id)`: metadata, plus `upstream_updated_at`
  (when the provider last revised the series) and `last_synced_at`.
- **`observations`** maps `(source, series_id, ts)` to `value`. It's a hypertable on `ts`.
  `value` is NULL where the provider reports a missing point.
- **`sync_runs`** is an append-only log of sync attempts and their outcomes.
- **`concepts`** mirrors the YAML tree. Its `id` is the dotted path (`labor.unemployment`).
- **`series_concepts`** holds one placement per series: `manual` (a pin) or `auto` (with the
  lowest probability along the path, plus a trail of each level's decision). A NULL concept
  means nothing fit at the top level.

Each series is identified by the pair `(source, series_id)`, so the same ID can exist at
two different providers without colliding.

## Decisions

Newest first. Each entry: what we chose, and why.

- **2026-10-04 · Commodities and the CPI components they feed are tracked as pairs.**
  The first deep dive covered both ends of each chain: commodity prices (energy,
  agriculture, metals) and the consumer price indexes they feed (energy, gasoline, food
  at home, coffee). Interplay between them is the point. We dropped a "Commodity Indexes"
  branch: "index" describes how a series is measured, not what it measures, so broad
  indexes stay at `prices.commodities` (a facet, consistent with the decision below).

- **2026-10-04 · The ontology organizes only synced series for now.** It started with the
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
- **2026-10-03 · The web app reads TimescaleDB directly (planned).** Next.js server code
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

- **Cross-links:** some series belong to two concepts (real consumer spending is both a
  GDP component and household spending; mortgage rates are both rates and housing). Add
  typed links between concepts (`component_of`, `deflated_by`, `leads`) or secondary
  placements once the tree is stable. The first pass-through check (YoY correlations,
  2000 onward) suggests *measured* links with a lag: crude oil → CPI gasoline (r 0.88 at
  1 month), arabica coffee → CPI coffee (0.82 at 7 months), the global food index → CPI
  food at home (0.72 at 9 months), and crude oil → core CPI (only 0.30). Open questions:
  should links be declared in YAML, computed, or both? And at series level or concept level?
- **New branches:** when a series fits nowhere, should a model propose a branch for a
  person to review (Jevbox's approach), or is a person editing the YAML enough?

- **Scheduling:** is a long-running `tempus sync --every` loop enough? The alternatives are
  cron or launchd, or an orchestrator (Dagster or Prefect) once there are several sources
  with dependencies between them.
- **Next data sources:** BLS, BEA, Treasury, World Bank, or scraped sites? Scraped sites
  may not fit the `Source` protocol's "metadata plus full series" shape.
- **Derived data:** where do transforms live (YoY change, rolling averages, resampling)?
  The options are SQL views or continuous aggregates in TimescaleDB, Python at sync time,
  or the front end.
- **Revision history:** we currently keep only the latest values. Do we want vintages
  (what the data said on date X), e.g. via FRED's ALFRED vintage data?
