# tempus

Collect public time-series data (starting with [FRED](https://fred.stlouisfed.org/)) into
TimescaleDB (Tiger Data), then explore it.

```
catalog.toml ──▶ tempus sync ──▶ sources/ (fred, …) ──▶ TimescaleDB (docker) ──▶ tempus serve (web)
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

## Explore

Browse ~2,000 popular FRED series as a 3D tree and find them by describing them. No API
keys needed to start:

```sh
uv run tempus db init
uv run tempus catalog import-snapshot data/fred/series-snapshot.json   # metadata only
uv run tempus ontology load        # tree + draft placements (ontology/placements.draft.yaml)
uv run tempus serve                # http://127.0.0.1:8765
```

Drag to orbit, scroll to zoom, click a bead to fly to a concept and a seed to open its
series page; press `/` and describe what you want ("mortgage rates", "jobs report"). The
search walks the tree with Jev when `TYPESAFE_API_KEY` is set, and with a labeled lexical
fallback when it isn't. Snapshot series have no data until synced: with `FRED_API_KEY` set a
series page syncs on first open; otherwise run `tempus catalog add fred <ID> --sync`. The
draft placements were written by Claude; `tempus ontology file` lets Jev decide them.

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
tempus ontology file               # file unplaced and draft-placed series with Jev (needs TYPESAFE_API_KEY)
tempus ontology load               # validate + load concepts.yaml and draft placements (sync does this too)
tempus catalog import-snapshot data/fred/series-snapshot.json  # browse without syncing
tempus resolve "crude oil price"   # phrase → concepts → series, with probabilities
```

To pin a series to a concept by hand, add `concept = "rates.mortgage"` to its `catalog.toml`
entry. Pins override automatic filing.

## Cards

A card is a saved view: inputs (series), formulas that combine them, and panels that show
the result. Cards are YAML files in `cards/`, checked in lint and rendered by `tempus serve`.

```sh
tempus card new my-card            # template in cards/my-card.yaml
tempus card check                  # validate (add --offline to skip the DB)
tempus card show crude-to-gasoline # summary; --json prints the panel payloads
tempus card functions | panels     # the vocabulary
tempus serve                       # http://127.0.0.1:8765, reloads when a card is saved
```

See `cards/crude-to-gasoline.yaml` for the format, and
`docs/specs/2026-10-04-cards-slice-1-design.md` for the full design.

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
- `concepts` / `series_concepts`: the tree and one placement per series (manual > auto > draft)
- `cards`: index of `cards/*.yaml`, rebuilt by `tempus card load`

**How syncing works:** if the source's `last_updated` hasn't moved, the sync skips
the download. Otherwise it downloads the full series and reconciles it in one
transaction: changed points are upserted and points the source removed are
deleted. This picks up revisions to economic data, which happen often.

## Adding a source

Implement the `Source` protocol in `src/tempus/sources/base.py` (`fetch_meta`,
`fetch_observations`), then register it in `sources/__init__.py`.
