# Dynamic DB-Backed Readings, Cards & Automatic Article Ingestion

**Status:** Implemented 2026-11-25 (SPA-49)
**Linear:** SPA-49

## Goal

Turn `tempus` from a git-committed file viewer into a **private, Postgres-backed dynamic research library** with an **automatic ingestion pipeline**:

1. **TimescaleDB as source of truth for dynamic content:** Dynamically added readings, cards, tracked series, and ingestion jobs live in Postgres (`readings`, `cards`, `ingest_jobs`, `series.tracked`). New articles and charts do not touch git (`cards/`, `readings/`, or `catalog.toml`), while the static repo seed cards remain supported and isolated file-backed tests continue working untouched.
2. **Automatic multi-format ingestion:** Users can submit a **PDF**, **HTML/article URL**, **plain text**, or **pre-enriched Markdown** via the web UI (`#/readings/new`, `+ Add Article`) or API (`POST /api/ingest`) and watch live progress via SSE until the enriched reading opens automatically.
3. **Pluggable LLM + Jev + FRED enrichment:** A zero-new-dependency `httpx` LLM abstraction supports **Gemini** (`GEMINI_API_KEY`) and **Anthropic** (`ANTHROPIC_API_KEY`), paired with `navigate` + TypeSafe **Jev**, automatic **FRED** search and `sync_series`, and `JevDrafter` fallback so generated cards validate deterministically against stored series.
4. **Private deployment on Mac mini Home Base:** Works cleanly with `mise run share:on` / Tailscale (`docs/HOMEBASE.md`) and extends `transfer.py` (`tempus db pull` / `tempus db push`) to carry `cards` and `readings` alongside series and observations.

## Out of scope

- Multi-user authentication, public internet hosting, or SaaS multi-tenancy (Home Base + Tailscale + `TEMPUS_SHARE_PASSWORD` provides private single-owner access).
- An interactive in-app Markdown / block-authoring editor (deferred to the next slice; schema columns `status` and `origin` prepare Postgres for `'draft' | 'published'` and `'ingested' | 'authored' | 'imported'`).
- Adding third-party LLM SDKs (`google-genai`, `anthropic`, `pypdf`, `beautifulsoup4`) or browser-side PDF parsers; all API calls go over `httpx` and HTML extraction uses Python stdlib `html.parser`.

## Schema

`src/tempus/schema.sql` adds idempotent migrations via `tempus db init`:

- `series.tracked boolean NOT NULL DEFAULT false`: marks FRED series synced dynamically (e.g. during ingestion or `tempus sync`) so incremental sync keeps dynamically tracked series fresh without modifying `catalog.toml`.
- `cards` gains `yaml_text text` and `updated_at timestamptz NOT NULL DEFAULT now()`, and alters `file_hash` to `DEFAULT ''` so DB-native cards saved via `DbCardRepository.save()` or `tempus card import` do not require a disk file.
- `readings`: stores `id text PRIMARY KEY`, `title`, `subtitle`, `source`, `published text`, `topics text[]`, `concepts text[]`, `card_ids text[]`, `body_md text NOT NULL`, `source_url text`, `status text NOT NULL DEFAULT 'published'`, `origin text NOT NULL DEFAULT 'ingested'`, `created_at`, `updated_at`.
- `ingest_jobs` (indexed by `created_at DESC`): tracks asynchronous pipeline runs (`id uuid PRIMARY KEY`, `kind`, `title`, `source_url`, `status`, `step`, `error`, `reading_id`, `card_ids text[]`, `created_at`, `updated_at`).

## Repositories & Storage Mode

- `Settings` (`src/tempus/config.py`) adds `storage: Literal["db", "files"] = "db"`, `gemini_api_key`, `anthropic_api_key`, `ingest_provider: Literal["auto", "gemini", "anthropic"] = "auto"`, `gemini_model`, and `anthropic_model`.
- `Settings.uses_db_storage` defaults to `True` (`storage == "db"`), except when tests explicitly pass `cards_path` or `readings_path` without overriding `storage`, keeping file-backed unit tests isolated without flags.
- `DbCardRepository` (`src/tempus/cards/repository.py`) and `DbReadingRepository` (`src/tempus/readings.py`) implement the existing `CardRepository` and `ReadingRepository` protocols (`ids`, `get`, `text`, plus `save`, `delete`, and `status_info`).
- `sync.py` adds `mark_tracked` / `tracked_series_ids`; `cli.py` adds `tempus card import`, `tempus card delete`, `tempus reading import`, and `tempus reading delete`, and includes tracked series in `tempus sync`.
- `transfer.py` (`plan_push`, `pull`, `apply_push`) transfers `cards` and `readings` during full-DB sync (`keys is None` when the target schema has `readings` and `cards.yaml_text`), leaving keyed/partial test syncs unchanged.

## Pluggable LLM + Jev + FRED Auto-Sync Pipeline

### `src/tempus/llm.py`

- **URL extractor:** `fetch_url_article(url)` downloads HTML via `httpx`, extracts visible text cleanly using stdlib `html.parser` (stripping `script`, `style`, `nav`, `footer`), and infers title/source metadata.
- **Structured contracts:** `ExtractedArticle` (metadata + cleaned body Markdown + proposed macroeconomic enrichment questions) and `ComposedEnrichment` (validated card specs + insertion anchors / `<Card>` / `<Note>` / `[^n]` annotations).
- **Providers:** `GeminiIngestLLM` (supports native `inlineData` `application/pdf` upload as well as text/URL inputs, JSON schema output) and `AnthropicIngestLLM` (supports native `document` PDF `base64` blocks and JSON tool/structured output), selected automatically or explicitly via `select_ingest_llm(settings)`. Both raise `LLMError` on missing credentials or HTTP/parse failures.

### `src/tempus/ingest.py`

1. Create an `ingest_jobs` row (`queued` -> `extracting`) and emit SSE progress events.
2. Fast-path `format="markdown"`: validates frontmatter/cards via `parse_reading`/`check_reading`, saves referenced cards (when provided) and reading directly into Postgres without an LLM call.
3. For `pdf`, `url`, and `text`:
   - `extracting`: run `llm.extract_article(...)` (plus `fetch_url_article` for URLs) to produce clean prose Markdown, metadata, and enrichment queries.
   - `resolving`: map queries against local catalog via `navigate` (`LexicalChooser` + `Jev` when `TYPESAFE_API_KEY` is present); search FRED (`FredSource.search`) for missing concepts, run `sync_series`, `mark_tracked`, and file newly synced series.
   - `composing`: compute real SQL ground-truth snapshots from TimescaleDB for candidate series, then call `llm.compose_enrichment(...)` to author `Card` specs (`PANELS`/`CATALOG`) and inline annotations/notes; validate every card with `parse_card` + `check_card`, falling back to `JevDrafter` if an LLM card fails validation.
   - `saving`: persist cards via `DbCardRepository.save()`, interleave validated `<Card>`/`<Note>` blocks into the Markdown, save the reading via `DbReadingRepository.save()`, mark the `ingest_jobs` row `done`, and broadcast SSE `readings` + `cards` + `ingest-progress`.

## Web UI & API

- `src/tempus/app.py`:
  - Uses `Db*Repository` when `settings.uses_db_storage` is true (with an in-process `_EventHub` powering `/api/events` SSE in DB mode alongside `watchfiles` when local dirs exist).
  - Exposes `storage` and configured `ingest_provider` in `GET /api/ingest/config` (leaving `GET /api/meta` unchanged for backwards compatibility).
  - Adds `POST /api/ingest`, `GET /api/ingest`, `GET /api/ingest/{job_id}`, and `DELETE /api/readings/{reading_id}`, and emits SSE updates on `POST /api/cards`.
- `src/tempus/web/`:
  - Adds `+ Add Article` action button on the Readings shelf (`#/readings`) and in-route `#/readings/new` view with tabs for **PDF Upload**, **Article URL**, **Paste Text**, and **Import Markdown**.
  - Streams live `ingest-progress` SSE stages (with polling fallback) and automatically navigates to `#/readings/<id>` once complete, plus a confirmed `Delete` action on dynamic readings.

## Mac mini Home Base & Git Migration

- Dynamic readings and their cards are stored in Postgres rather than git, while `catalog.toml` stays at the repository baseline.
- `docs/HOMEBASE.md` documents `.env` keys (`GEMINI_API_KEY` / `ANTHROPIC_API_KEY`, `TEMPUS_STORAGE=db`) and Tailscale/Share workflow for remote private publishing and ingestion.

## One-Way Doors

- Moving dynamic readings and cards out of git means Postgres backups / `tempus db pull` hold dynamic state; `transfer.py` must copy both `cards` and `readings` so laptops and Home Base stay synchronized.
- Keeping `cards.yaml_text` nullable and `readings` separate lets static file repos, DB-backed repos, and unit tests coexist without destructive schema rewrites.

## Docs to Update

- `docs/specs/2026-11-25-dynamic-readings-and-ingest-design.md` (status on completion)
- `docs/ARCHITECTURE.md` (Components, Data flow, Decisions)
- `docs/STACK.md` (pluggable LLM + Postgres dynamic storage pattern)
- `docs/HOMEBASE.md` (private dynamic publishing & `.env` setup)
- `README.md` (CLI commands & `+ Add Article` workflow)
