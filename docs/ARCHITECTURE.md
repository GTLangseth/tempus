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

Each series is identified by the pair `(source, series_id)`, so the same ID can exist at
two different providers without colliding.

## Decisions

Newest first. Each entry: what we chose, and why.

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
