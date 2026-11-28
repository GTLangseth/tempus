-- Idempotent: safe to run on every `tempus db init`.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- One row per tracked series, from any source.
CREATE TABLE IF NOT EXISTS series (
    source              text        NOT NULL,
    series_id           text        NOT NULL,
    title               text,
    units               text,
    frequency           text,
    seasonal_adjustment text,
    notes               text,
    extra               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    upstream_updated_at timestamptz,          -- when the source last revised the series
    last_synced_at      timestamptz,          -- when we last pulled it
    tracked             boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (source, series_id)
);
ALTER TABLE series ADD COLUMN IF NOT EXISTS tracked boolean NOT NULL DEFAULT false;

-- The data points. A hypertable partitioned on ts.
CREATE TABLE IF NOT EXISTS observations (
    source    text             NOT NULL,
    series_id text             NOT NULL,
    ts        timestamptz      NOT NULL,
    value     double precision,               -- NULL = source reported a missing value
    PRIMARY KEY (source, series_id, ts),
    FOREIGN KEY (source, series_id) REFERENCES series ON DELETE CASCADE
);

SELECT create_hypertable('observations', by_range('ts', INTERVAL '5 years'), if_not_exists => TRUE);

-- Audit log of every sync attempt.
CREATE TABLE IF NOT EXISTS sync_runs (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source        text        NOT NULL,
    series_id     text        NOT NULL,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text        NOT NULL DEFAULT 'running',  -- running | updated | unchanged | failed
    rows_upserted integer,
    rows_deleted  integer,
    error         text
);

CREATE INDEX IF NOT EXISTS sync_runs_started_at_idx ON sync_runs (started_at DESC);

-- The concept tree, mirrored from ontology/concepts.yaml by `tempus ontology load`.
CREATE TABLE IF NOT EXISTS concepts (
    id          text    PRIMARY KEY,                       -- dotted path, e.g. labor.unemployment
    parent_id   text    REFERENCES concepts ON DELETE CASCADE,
    name        text    NOT NULL,
    description text    NOT NULL,
    position    integer NOT NULL                           -- order among siblings
);

-- Where each series sits in the tree (one concept per series).
CREATE TABLE IF NOT EXISTS series_concepts (
    source     text        NOT NULL,
    series_id  text        NOT NULL,
    concept_id text        REFERENCES concepts ON DELETE CASCADE,  -- NULL = filed, but no fit at the top level
    method     text        NOT NULL,                       -- manual (pin) > auto (Jev) > draft
    confidence real,                                       -- lowest probability along the auto path
    trail      jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- each level's decision, for review
    filed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, series_id),
    FOREIGN KEY (source, series_id) REFERENCES series ON DELETE CASCADE
);

-- Migration 2026-10-04: allow 'draft' placements (ontology/placements.draft.yaml). Dropping
-- and re-adding the named constraint keeps this idempotent on databases created before it.
ALTER TABLE series_concepts DROP CONSTRAINT IF EXISTS series_concepts_method_check;
ALTER TABLE series_concepts ADD CONSTRAINT series_concepts_method_check
    CHECK (method IN ('manual', 'auto', 'draft'));

-- Cards & readings: Postgres-backed for dynamic articles, also syncable from repo files via `tempus card load` / `import`.
CREATE TABLE IF NOT EXISTS cards (
    id         text        PRIMARY KEY,                  -- kebab-case card id
    title      text        NOT NULL,
    question   text        NOT NULL,
    tags       text[]      NOT NULL DEFAULT '{}',
    concepts   text[]      NOT NULL DEFAULT '{}',
    spec       jsonb       NOT NULL,
    yaml_text  text,
    file_hash  text        NOT NULL DEFAULT '',
    loaded_at  timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE cards ADD COLUMN IF NOT EXISTS yaml_text text;
ALTER TABLE cards ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE cards ALTER COLUMN file_hash SET DEFAULT '';

-- Enriched article readings linked to cards.
CREATE TABLE IF NOT EXISTS readings (
    id          text        PRIMARY KEY,
    title       text        NOT NULL,
    subtitle    text,
    source      text,
    published   text,
    topics      text[]      NOT NULL DEFAULT '{}',
    concepts    text[]      NOT NULL DEFAULT '{}',
    card_ids    text[]      NOT NULL DEFAULT '{}',
    body_md     text        NOT NULL,
    source_url  text,
    status      text        NOT NULL DEFAULT 'published',
    origin      text        NOT NULL DEFAULT 'ingested',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Async article ingestion jobs triggered via the web UI or API.
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id          uuid        PRIMARY KEY,
    kind        text        NOT NULL,
    title       text,
    source_url  text,
    status      text        NOT NULL,
    step        text        NOT NULL DEFAULT 'queued',
    error       text,
    reading_id  text        REFERENCES readings(id) ON DELETE SET NULL,
    card_ids    text[]      NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_jobs_created_at_idx ON ingest_jobs (created_at DESC);
