# Cards, slice 1: language, engine and a minimal app

- **Status:** Draft, in review (approved when this PR merges)
- **Linear:** SPA-25 (card language v0). Resolves SPA-26 (where derived transforms live).
  Supersedes SPA-23 (Next.js scaffold).
- **Horizon:** 2 · Cards (see `docs/SOUL.md`)
- **Related:** `docs/STACK.md` (Open decisions, Inspirations), `docs/ARCHITECTURE.md`

## Goal

Write a card as YAML, check it, and see it rendered as real charts in a browser that reloads
on save. This slice builds the contract that every later surface depends on: the card
language, the engine that evaluates it, and the panel payload that renderers draw. The
surfaces include the MCP server (SPA-29), dashboards and signals.

**Done when:**
- `cards/crude-to-gasoline.yaml` and `cards/coffee-pass-through.yaml` both render in
  `tempus serve`. Each shows a YoY overlay and a lag-correlation panel, and the numbers
  reproduce the findings in `ARCHITECTURE.md` (crude → CPI gasoline r ≈ 0.88 at 1 month;
  arabica → CPI coffee r ≈ 0.82 at 7 months).
- Editing a card in an editor updates the browser within a second or two. A broken card
  shows its errors in place of the card.
- `tempus card check --offline` runs in `mise run lint`. `mise run test` covers the
  functions, the parser, validation and payload snapshots.

## Out of scope

Dashboards and layouts; the MCP server (SPA-29); forecasting functions; abstract
`{ metric: … }` references; Lightweight Charts; saving a card from the UI (cards are edited
as files); signals; vintages; pushing evaluation down to SQL; label-aware checks that reject unsafe
transforms (e.g. `yoy` on a rate), which arrive once series labels exist (SPA-18).

## Decisions this design rests on

Settled in conversation on 2026-10-04 and recorded in `docs/STACK.md` → Open decisions:

| Decision | Choice | Door |
|---|---|---|
| Card storage | YAML in git is the source of truth; a `cards` table is an index; everything goes through a `CardRepository` interface | One-way (mitigated by the interface) |
| References | `source:id` always allowed; `{ concept: … }` expands to a set; `{ metric: … }` later, as an addition | Two-way |
| Expression language | Named `inputs` plus `formulas` in the style of Grafana and Datadog; a restricted subset of Python expressions parsed with `ast` | One-way (mitigated by `version:`) |
| Evaluation | Python with Polars now. Individual functions can push down to SQL later without changing the language | Two-way |
| Panel grammar | A closed panel catalog using Vega-Lite's channel names; `type: vega` as the escape hatch (not built in v0) | One-way (mitigated by `version:`) |
| App | FastAPI `tempus serve`, plus a no-build static front end, with hot reload over server-sent events | Two-way |
| Renderer | ECharts only, behind a per-panel adapter, plus `theme.json` tokens | Two-way |

## Card format (version 1)

```yaml
# cards/crude-to-gasoline.yaml — the card ID is the filename (kebab-case, stable)
version: 1
title: Crude oil → gasoline CPI pass-through
question: Does a move in crude show up in gasoline CPI, and how fast?   # required
notes: |                                   # optional markdown
  First pass (2000+): YoY r ≈ 0.88 at a 1-month lag.
tags: [energy, pass-through]               # optional
concepts: [prices.energy]                  # optional: where it appears in the tree
window: { start: 2000-01-01 }              # optional: default range; end defaults to latest

inputs:
  crude:    fred:DCOILWTICO                # shorthand: source:series_id
  gasoline: { series: fred:CUSR0000SETB01, label: CPI gasoline }
  # energy: { concept: prices.energy }     # a set of series

formulas:
  crude_yoy:    yoy(resample(crude, "M", "mean"))
  gasoline_yoy: yoy(gasoline)
  passthrough:  lagcorr(crude_yoy, gasoline_yoy, nlags=12)

panels:                                    # one or more; the first is the thumbnail
  - type: timeseries
    title: YoY change
    y: [crude_yoy, gasoline_yoy]
  - type: lagcorr
    data: passthrough
```

**Rules**
- Top-level keys are fixed, and unknown keys are an error.
- Input and formula names share one namespace and must be valid identifiers.
- Panels may refer to inputs or formulas.
- Formulas may refer to each other. Cycles are an error.

## Expression language

A formula is a single Python expression, parsed with `ast.parse(expr, mode="eval")` and
**never evaluated as Python**. Allowed node types are: `Name`, `Constant` (numbers and
strings), `BinOp` (`+ - * /`), `UnaryOp` (`-`), and `Call` where the function is a plain
name, with positional and keyword arguments. Anything else is rejected with a message
naming the construct: attributes, subscripts, comparisons, lambdas, comprehensions, and so
on.

**Value types:** `Series` (one time series, with its frequency, units and label),
`SeriesSet` (a named collection), `Table`, and scalar numbers and strings as arguments.
Functions applied to a `SeriesSet` run on each member, as PromQL does with vectors.
Arithmetic between a `Series` and a `SeriesSet` broadcasts.

**Frequency:** each `Series` carries a frequency (`D`, `W`, `M`, `Q`, `Y`, or `irregular`).
Binary operations require matching frequencies, and the error suggests the `resample` that
fixes it. Operands are aligned on timestamps with an inner join. Nothing is filled
implicitly.

## Function catalog (v0)

Each entry has a name, a typed signature, the result type, a one-line description, a
caveat, and a Polars implementation. There's room for an optional SQL implementation later.

| Function | Signature → result | Notes |
|---|---|---|
| `resample(x, freq, agg="mean")` | Series, `D/W/M/Q/Y`, `mean/last/sum/min/max` → Series | Period-start timestamps, matching how FRED labels periods |
| `shift(x, n)` | Series, int → Series | Positive n moves values later in time |
| `fill(x, method)` | Series, `ffill/linear` → Series | The only way gaps get filled |
| `diff(x, n=1)` | Series → Series | |
| `pct_change(x, n=1)` | Series → Series | Percent, ×100 |
| `yoy(x)` | Series → Series | `pct_change` over one year of periods; needs a regular frequency |
| `log(x)` | Series → Series | Error on values ≤ 0 |
| `rolling(x, n, agg="mean")` | Series → Series | `mean/sum/min/max/std` |
| `ewm(x, span)` | Series → Series | |
| `rebase(x, at, to=100)` | Series, date → Series | Uses the nearest observation at or after `at` |
| `zscore(x, window=None)` | Series → Series | Full-sample, or rolling when `window` is set |
| `mean/sum/min/max(set)` | SeriesSet → Series | Across members; frequencies must match |
| `corr(x, y, window)` | Series, Series → Series | Rolling Pearson correlation |
| `lagcorr(x, y, nlags=12)` | Series, Series → Table(lag, r, n) | Positive lag k means **x leads y by k periods**. Caveat: warns when either input looks trending in levels |

## Panel catalog (v0)

Each entry has a type, the result types it accepts, its options, and a doc covering
purpose, when to use it, when not to, and the theory behind it.

| Type | Accepts | Options | Purpose |
|---|---|---|---|
| `timeseries` | Series, or a SeriesSet via `y: [...]` | `title`, `y` | Trends over time; a shared axis by default. Dual axes are deliberately left out in v0: rebase or z-score instead (Cleveland & McGill) |
| `lagcorr` | Table from `lagcorr` | `title`, `data` | Bars of r by lag; peak highlighted; the header says "x leads y by k" |
| `stat` | Series | `title`, `y`, `change: yoy/period` | Latest value, its change, and a sparkline |
| `table` | Table, or Series | `title`, `data` | Raw values |

`scatter`, `heatmap` and `forecast` are deferred until a card needs them. Each one gets a
catalog entry when it's added.

## Engine (`src/tempus/cards/`)

| Module | Job |
|---|---|
| `spec.py` | Pydantic models; YAML loading; structural checks |
| `expr.py` | `ast` parsing with the allow-list → expression tree |
| `functions.py` | Function catalog and implementations |
| `panels.py` | Panel catalog |
| `check.py` | Static checks with no DB: names, arity, cycles, panel ↔ result types |
| `resolve.py` | References → series metadata through the DB; concept expansion through `series_concepts`; frequency checks |
| `evaluate.py` | One query fetches the window's observations; formulas are evaluated in dependency order |
| `render.py` | Panel payloads |
| `repository.py` | `CardRepository` (`list`, `get`, `save`); `FileCardRepository` over `cards/`, plus sync into the `cards` index |

**Pipeline:** load → parse → check (offline) → resolve (DB metadata) → evaluate (DB data)
→ render → panel payloads.

**Errors** are collected, never raised one at a time. Every stage returns
`list[CardError]` with `card`, `path` (for example `formulas.spread` or `panels[1]`),
`level` (`error` or `warning`), `message`, and an optional `hint` ("did you mean
'gasoline'?", "resample(crude, \"M\") first"). Warnings, such as the trending-levels
caveat or an empty window, don't stop rendering.

## Panel payload (the seam)

The renderer-neutral JSON that the CLI, the API and later MCP return. It is versioned
separately from cards.

```json
{
  "payload_version": 1,
  "card": "crude-to-gasoline",
  "panel": 0,
  "type": "timeseries",
  "title": "YoY change",
  "encoding": { "x": "ts", "y": "value", "color": "series" },
  "series": [
    { "name": "crude_yoy", "label": "Crude oil: WTI, YoY %", "units": "%", "freq": "M",
      "points": [["2000-01-01", 172.1], ["2000-02-01", null]] }
  ],
  "table": null,
  "annotations": { "peak": null },
  "warnings": []
}
```

## App

`tempus serve [--port]` starts FastAPI with uvicorn.

| Endpoint | Returns |
|---|---|
| `GET /api/concepts` | The concept tree with series counts and card counts |
| `GET /api/cards` | Card summaries (id, title, question, concepts, tags, error count) |
| `GET /api/cards/{id}` | `{ card, payloads, errors }`, with an optional `?start=&end=` |
| `GET /api/events` | A server-sent event stream: `card-changed` or `card-invalid` when a file under `cards/` is saved (`watchfiles`) |
| `GET /` | The static app |

**Front end:** `src/tempus/web/` holds `index.html`, `app.js` (ES modules, no build step),
`adapters/echarts.js` (payload → ECharts option, one function per panel type),
`theme.json` (tokens read by both the CSS and the adapter), and `style.css`. ECharts loads
from a pinned CDN URL. Layout: the concept tree, with the cards under each concept, on the
left; the selected card on the right, showing its question, notes and panels. Errors render
in place of the card.

## CLI

```
tempus card list | new ID | check [ID] [--offline] | show ID [--start --end] [--json]
tempus card load | functions [NAME] | panels [TYPE]
tempus serve
```

`show` without `--json` prints a numeric summary: the latest value and change for each
series, and the peak lag for `lagcorr`.

## Schema change

```sql
CREATE TABLE IF NOT EXISTS cards (
  id         text PRIMARY KEY,
  title      text NOT NULL,
  question   text NOT NULL,
  tags       text[] NOT NULL DEFAULT '{}',
  concepts   text[] NOT NULL DEFAULT '{}',
  spec       jsonb NOT NULL,
  file_hash  text NOT NULL,
  loaded_at  timestamptz NOT NULL DEFAULT now()
);
```

This is an index only. `tempus card load` rebuilds it from `cards/`, and `ARCHITECTURE.md`
gets updated in the same change.

## Dependencies

Polars, FastAPI and uvicorn are added, each with a register row in `docs/STACK.md`.
`watchfiles` comes in through uvicorn. PyYAML is already a dependency. ECharts is loaded
from a CDN; no npm.

## Testing

- **Functions:** small hand-built frames covering gaps, irregular frequency, and the lag
  sign (a known shift must peak at the right lag).
- **Parser:** the allow-list accepts the valid forms and rejects attributes, subscripts,
  lambdas, comprehensions and comparisons.
- **Validation:** broken cards in `tests/cards/bad/*.yaml`, each with its expected error
  path and message.
- **Integration (DB):** both example cards are evaluated, and the payloads are compared
  with snapshots. The r values are asserted within a tolerance of the recorded findings.
- **API:** FastAPI's `TestClient` for `/api/cards` and `/api/cards/{id}`, including the
  error case.
- **Lint:** `tempus card check --offline` over `cards/`.
- **No browser tests in v0.**

## Docs to update when this lands

- `ARCHITECTURE.md`: Components (Cards, App), the data flow, the `cards` table, and
  Decisions. Reverses the "web reads Postgres directly" decision and resolves the derived-
  data open question.
- `STACK.md`: register rows for Polars, FastAPI and uvicorn, and the web front end row.
  Graduate the settled entries out of Open decisions.
- `README.md`: the `tempus card` commands, `tempus serve`, and the card format in brief.
