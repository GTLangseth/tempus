# Article readings: digest external research with live tempus cards

- **Status:** Implemented 2026-11-25 (SPA-48)
- **Linear:** SPA-48
- **Horizon:** 2 · Cards → 3 · Converse (see `docs/SOUL.md`)
- **Related:** SPA-47 (Obsidian notes & theses); `docs/specs/2026-10-04-cards-slice-1-design.md` (`cards/*.yaml`); `docs/DESIGN.md` (Neon Notebook)

## Goal

Turn external research articles (macro letters, papers, PDFs, links) into git-tracked **Readings** (`readings/<id>.md`) that stay tethered to `tempus`'s live database:

1. Extract the article's core empirical claims and compare them honestly against stored series (`confirmed`, `extended`, `nuanced`, or `unverified`).
2. Recreate key charts and add second-order context as ordinary `cards/*.yaml` files that live in the concept tree and re-evaluate automatically whenever `tempus sync` brings in new observations.
3. Read the finished synthesis in `tempus serve` (`#/readings/<id>`) with inline live cards and SSE hot reload, and validate every reading in `tempus reading check` / `mise run lint`.

Success: `uv run tempus reading check` validates every reading, embedded card, and claim series reference; editing a reading or any card it embeds hot-reloads `#/readings/<id>`; and `.agents/skills/digest-article` turns a new PDF or article into a verified reading backed by synced FRED series.

## Out of scope

- **Standalone LLM-API ingestion CLI** (`tempus reading ingest` calling an external LLM SDK directly from Python). Digestion runs through an agent skill (`.agents/skills/digest-article`) using existing `tempus` CLI tools, with zero new runtime dependencies.
- **Storing copyrighted source PDFs in git.** Local source files may sit in `readings/sources/`, which is gitignored.
- **Formulas or SQL inside readings.** `Card` remains the single calculation unit in `tempus`; a reading only embeds saved `cards/*.yaml` and cites `source:series_id` references on claim callouts.
- **Database `readings` index table.** File scanning via `FileReadingRepository` covers dozens of readings cleanly; an index table can be added later behind the `ReadingRepository` seam without changing `readings/*.md`.

## Design

### 1. File format (`readings/<id>.md`)

Kebab-case filename (`[a-z0-9]+(-[a-z0-9]+)*`), YAML front matter (`---` ... `---`) validated strictly (`extra="forbid"`), and a Markdown body made of prose plus two fenced block types:

- **Front matter (`ReadingMeta`):**
  - `version: 1` (required for forward migration)
  - `title`, `authors` (`list[str]`, ≥ 1), `published` (`str`, e.g. `"2026-08"`), `summary`
  - Optional: `source`, `url`, `credit` (colophon line after endnotes), `concepts` (`list[str]`), `tags` (`list[str]`)
- **Prose blocks & footnotes:** `##` / `###` headings, paragraphs (leading essay paragraph styled with a drop cap), `>` blockquotes, bullet/ordered lists, `---` dividers, inline `[^n]` footnote markers + `[^n]: ...` definitions (checked for missing/unused definitions and expandable inline in the UI), and safe inline formatting (`` `code` ``, `==highlight==`, `**bold**`, `*italic*`, `[text](url)`).
- **```` ```card ```` fence (`CardEmbed`):** either a bare card ID (`coffee-pass-through`) or YAML `{card|id, caption?, start?, end?}`. Embeds a saved card from `cards/` via `run_card`, optionally clipping its window or adding a reading-specific caption.
- **```` ```note ```` fence (`NoteBlock`):** optional `title:` line followed by lab commentary paragraphs or `- ` bullets shown alongside the essay (`Annotated` / `Digest` modes).
- **```` ```claim ```` fence (`ClaimBlock`):** structured YAML `{quote, status, verdict, series?, card?}` where `status` is one of:
  - `confirmed`: stored data matches the article's empirical claim.
  - `extended`: newer observations in `tempus` move beyond the paper's sample and reinforce or update it.
  - `nuanced`: the data supports part of the claim, misses a cohort/institutional slice, or shows a subtler shape.
  - `unverified`: the dataset isn't in `tempus` yet (e.g. non-FRED survey or governance counts); checked statically without pretending DB verification.

Unknown fenced block tags (e.g. ```` ```cards ````) fail parsing with a `did_you_mean` hint rather than rendering silently as raw code. Borrowed inspiration: Observable Framework and Evidence markdown-plus-live-queries, keeping calculation inside `cards/*.yaml`.

### 2. Deep module & repository seam (`src/tempus/readings.py`)

`ReadingRepository` mirrors `CardRepository`:

- `FileReadingRepository(root: Path)` discovers `readings/*.md` matching `READING_ID` (skipping `README.md`), parses front matter and blocks into typed dataclasses, and collects `ReadingError` items (error or warning, with path and `did_you_mean` hints).
- `check_reading(reading_id, reading, cards_repo, *, conn=None, known_concepts=None)`:
  - **Static (`--offline`):** validates every embedded `card` and claim `card` exists in `cards_repo` and passes `check_card`, validates `meta.concepts` against `known_concepts` if supplied, and warns when a reading embeds zero cards.
  - **Database:** runs `cards.resolve.resolve` on each referenced card and verifies every `claim.series` exists in `series` (error if missing, warning if metadata-only when `status != "unverified"`).
- `run_reading(conn, readings_repo, cards_repo, reading_id, *, known_concepts=None)` evaluates all embedded cards via `run_card` and enriches every `claim.series` entry with title, units, latest date, and latest non-null value.

### 3. CLI, API & SSE hot reload

- **CLI (`src/tempus/cli.py`):** `tempus reading list | new | check [--offline] | show [--json]`. `mise run lint` adds `uv run tempus reading check --offline`.
- **Engine API (`src/tempus/app.py`):**
  - `GET /api/readings` (no DB needed): reading summaries, claim verdict counts, embedded card counts, static error counts.
  - `GET /api/readings/{reading_id}`: full evaluated reading payload from `run_reading`.
  - `GET /api/events`: watches both `cards/` and `readings/` with `watchfiles.awatch`, emitting `reading-changed`, `reading-invalid`, and `reading-removed` alongside `card-*` events (ignoring editor temp files).

### 4. Web UI (`src/tempus/web/views/readings.js`)

Follows `docs/DESIGN.md` (Neon Notebook):

- Top nav adds `Readings` (`#/readings`), with `#/readings/<id>` routed alongside `Explore` and `Cards`.
- Sidebar lists all readings (`#sidebar`), highlighting the active reading with the lime marker swipe.
- Index view (`#/readings`) shows a single-column feed of `.sticker` reading cards with source/date eyebrow, summary, claim scorecard chips, and concept tags.
- Reading detail (`#/readings/<id>`) opens with a masthead `.sticker` (title, authors, reading time, and an **`Annotated · Essay · Digest`** mode switch) plus a collapsible/togglable `tempus · lab brief` (summary, claim status counts, concept chips).
- **Three reading modes:** `Annotated` interleaves essay sheets, expandable inline footnotes, `claim` callouts, `note` blocks, and live `.sticker` cards; `Essay` hides `tempus`-only annotations for pure distraction-free reading of the article + endnotes; `Digest` hides essay prose for fast empirical scanning of claims, lab notes, and cards.
- **Navigation without hash mutation:** sidebar section TOC links, `[n]` inline footnote triggers, and `↩` endnote back-links scroll and toggle in-place via `<button>` handlers so `#/readings/<id>` is never overwritten.
- Hot reloads whenever either the reading Markdown or any of its embedded cards changes on disk.

### 5. Ingestion skill (`.agents/skills/digest-article`)

A repo-tracked model-invoked skill (symlinked into `.claude/skills/digest-article` while keeping `.agents/` third-party skills gitignored) guiding any agent session through four steps: inventory claims → resolve & `catalog add --sync` missing FRED series → build & check 3–6 v1 cards → write & `tempus reading check` the reading file.

## One-way doors & seams

| Choice | Door | Seam / reversal |
|---|---|---|
| `readings/<id>.md` front matter (`version: 1`) + `card` / `claim` fences | **One-way** once many readings accumulate | `version: 1` enables mechanical migration; `ReadingRepository` isolates parsing and storage; underlying `cards/*.yaml` remain standard cards |
| `readings` storage on filesystem (no DB table yet) | Two-way | `ReadingRepository` protocol matches `CardRepository`; a `readings` index table can be added without changing files or API routes |
| Agent skill instead of built-in Python LLM CLI | Two-way | Zero runtime dependencies; a standalone CLI can later target the same `readings/<id>.md` format |

## Docs to update when implemented

- `docs/ARCHITECTURE.md`: add Readings to Components, Data flow, and dated entry under **Decisions**.
- `docs/STACK.md`: add `Reading format & storage` under **Open decisions** and `Observable Framework / Evidence` under **Inspirations**.
- `README.md`: add **Readings** section with `tempus reading` CLI commands.
- `docs/specs/2026-11-25-article-readings-design.md`: set status to `Implemented 2026-11-25 (SPA-48)`.
