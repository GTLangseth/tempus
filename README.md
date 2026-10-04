# tempus

Collect public time-series data (starting with [FRED](https://fred.stlouisfed.org/)) into
TimescaleDB (Tiger Data), then explore it.

```
catalog.toml ──▶ tempus sync ──▶ sources/ (fred, …) ──▶ TimescaleDB (docker) ──▶ web/ (soon)
```

Blueprint, decisions, and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Setup

Requires [mise](https://mise.jdx.dev) and Docker.

```sh
mise install          # python, uv, node
mise run setup        # .env, uv sync, start db, apply schema
# put your free FRED key in .env → https://fred.stlouisfed.org/docs/api/api_key.html
mise run sync
```

## CLI

```sh
tempus db init | status            # schema / health
tempus fred search "mortgage rate" # find series
tempus catalog add fred MORTGAGE30US --sync
tempus catalog list | remove fred GDP
tempus sync                        # sync everything in catalog.toml
tempus sync -s UNRATE --force      # one series, ignore freshness check
tempus sync --every 6h             # keep running on an interval
tempus series list                 # what's stored, coverage, freshness
tempus series show UNRATE          # sparkline + recent points
tempus runs                        # sync history

tempus ontology tree               # concept tree with the series filed in it
tempus ontology file               # file unplaced series with Jev (needs TYPESAFE_API_KEY)
tempus ontology load               # validate + load ontology/concepts.yaml (sync does this too)
```

To pin a series to a concept by hand, add `concept = "rates.mortgage"` to its `catalog.toml`
entry. Pins override automatic filing.

## mise tasks

| task | does |
|---|---|
| `setup` | first-time bootstrap |
| `db:up` / `db:down` / `db:reset` | container lifecycle (`reset` wipes data) |
| `db:psql` / `db:logs` | shell / logs |
| `sync` | `tempus sync` |
| `test` / `lint` / `fmt` | dev loop |
| `stack:review-due` | is the quarterly stack review ([docs/STACK.md](docs/STACK.md)) due? |

## Data model

- `series`: one row per `(source, series_id)` with metadata and `upstream_updated_at`
- `observations`: hypertable on `ts`, `(source, series_id, ts) → value`
- `sync_runs`: audit log of every sync attempt

**How syncing works:** if the source's `last_updated` hasn't moved, the sync skips
the download. Otherwise it downloads the full series and reconciles it in one
transaction: changed points are upserted and points the source removed are
deleted. This picks up revisions to economic data, which happen often.

## Adding a source

Implement the `Source` protocol in `src/tempus/sources/base.py` (`fetch_meta`,
`fetch_observations`), then register it in `sources/__init__.py`.
