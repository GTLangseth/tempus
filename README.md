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

## Home base

To run tempus on an always-on Mac (database, keys, app reachable from your phone over
Tailscale, agents in `tmux`), follow [docs/HOMEBASE.md](docs/HOMEBASE.md):
`mise run homebase:setup`, then `mise run homebase:status`.

## CLI

```sh
tempus db init | status            # schema / health
tempus db where                    # dev, or prod read-only (TEMPUS_DB=prod)
tempus db pull                     # prod's data + dynamic cards/readings → dev
tempus db push [--yes]             # dev's newer syncs, Jev & dynamic readings → prod (dry run without --yes)
tempus db readonly-role            # on the home base: the read-only tempus_ro role
tempus fred search "mortgage rate" # find series
tempus catalog add fred MORTGAGE30US --sync
tempus catalog list | remove fred GDP
tempus sync                        # sync catalog.toml plus dynamically tracked DB series
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
tempus card draft "Does crude lead gasoline CPI?"   # draft a card: shape, a series per role, the YAML (--save)
tempus card draft --eval          # grade every question in evals/drafting.yaml (--runs 3, --json)
```

To pin a series to a concept by hand, add `concept = "rates.mortgage"` to its `catalog.toml`
entry. Pins override automatic filing.

## Cards

A card is a saved view: inputs (series), formulas that combine them, and panels that show
the result. Seed cards live in `cards/*.yaml`; dynamically created or ingested cards live in
Postgres (`TEMPUS_STORAGE=db`, default) so your library grows without touching git.

```sh
tempus card new my-card            # template in cards/my-card.yaml
tempus card import cards/*.yaml    # import card YAML into Postgres
tempus card delete my-card         # remove a DB-stored card
tempus card check                  # validate DB + seed cards (add --offline to skip the DB)
tempus card show crude-to-gasoline # summary; --json prints the panel payloads
tempus card functions | panels     # the vocabulary
tempus serve                       # http://127.0.0.1:8765
```

See `cards/crude-to-gasoline.yaml` for the format, and
`docs/specs/2026-10-04-cards-slice-1-design.md` for the full design.

## Readings & Auto-Ingest

A reading digests an external research article into prose, claim verdicts (`confirmed`,
`extended`, `nuanced`, `unverified`), companion notes, and embedded live cards rendered at
`#/readings`. New readings can be ingested automatically from **PDF upload**, **URL**, **text**,
or **Markdown** via `+ Add Article` (`#/readings/new`) using `GEMINI_API_KEY` or
`ANTHROPIC_API_KEY` + automatic FRED sync.

```sh
tempus reading new my-article          # starter template in readings/my-article.md
tempus reading import my-article.md    # import Markdown reading into Postgres
tempus reading delete my-article       # remove a DB-stored reading
tempus reading list                    # list readings, card counts, and claim counts
tempus reading check [--offline]       # validate front matter, card embeds, and series refs
tempus reading show my-article         # summary and claim verdicts (--json for payloads)
```

See [docs/specs/2026-11-25-dynamic-readings-and-ingest-design.md](docs/specs/2026-11-25-dynamic-readings-and-ingest-design.md)
and `.agents/skills/digest-article`.

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

- `series`: one row per `(source, series_id)` with metadata, `upstream_updated_at`, and `tracked`
- `observations`: hypertable on `ts`, `(source, series_id, ts) → value`
- `sync_runs`: audit log of every sync attempt
- `concepts` / `series_concepts`: the tree and one placement per series (manual > auto > draft)
- `cards`: DB-stored & seed-indexed chart specs (`spec`, `yaml_text`)
- `readings`: DB-stored Markdown digests (`body_md`, metadata, `card_ids`, `status`, `origin`)
- `ingest_jobs`: async PDF/URL/Text ingestion status and pipeline steps

**How syncing works:** if the source's `last_updated` hasn't moved, the sync skips
the download. Otherwise it downloads the full series and reconciles it in one
transaction: changed points are upserted and points the source removed are
deleted. This picks up revisions to economic data, which happen often.

## Adding a source

Implement the `Source` protocol in `src/tempus/sources/base.py` (`fetch_meta`,
`fetch_observations`), then register it in `sources/__init__.py`.

## License

Dual-licensed under the [MIT License](LICENSE-MIT) or the
[Apache License, Version 2.0](LICENSE-APACHE), at your option.
