# Dev and prod databases: pull, push and read-only prod over SSH

- **Status:** Implemented 2026-10-11 (SPA-46, PR #13)
- **Linear:** SPA-46
- **Horizon:** 1 · Explore (see `docs/SOUL.md`); development tooling for every later horizon
- **Related:** `docs/HOMEBASE.md` (SPA-39); `src/tempus/sync.py` (skip-if-unchanged);
  `src/tempus/app.py` (`guest` mode, PR #10);
  `docs/specs/2026-10-04-jev-decision-store-design.md` (`jev_decisions`, JSONL export)

## Goal

Develop and test on the MacBook (or in cloud sessions) against a **dev** database you can
change freely, while the Mac mini's database stays **prod**. Three things make that cheap:

1. **Pull** prod's data down, so dev starts from real data without re-running backfills.
2. **Push** what dev synced or filed back up, so prod never pays for the same backfill twice
   (FRED's rate limit, Jev's per-call cost, any future paid source).
3. **Read prod from dev** without copying anything, for UI and card work, with no way to write.

Success: `tempus db pull` then `tempus sync` on the MacBook fetches only what changed since
prod's copy; a backfill done in dev reaches prod with `tempus db push --yes` and no second
fetch; `TEMPUS_DB=prod tempus serve` shows prod's data through local code and can't change it.

## Out of scope

- **Cloud sessions.** They can't reach the tailnet. They keep using the snapshot and, once
  SPA-16 lands, Jev replay fixtures. Versioned extract files (Parquet or similar) would serve
  them; that is a file format and a one-way door, deferred until it is needed (see Open
  questions).
- **Syncing git-derived tables.** `concepts` and `cards` are rebuilt from `ontology/` and
  `cards/` on each side. Draft placements come from `ontology/placements.draft.yaml`.
- **`sync_runs` history.** Each side keeps its own log.
- **Propagating deletes, schema changes or ontology edits** through push. Those ship as code
  (merge, pull on the mini, `db init`) or as deliberate commands run on the mini.
- **Replication, scheduled pulls, more than one prod.**

## Design

### 1. Shape: the tunnel, settings and roles

All of it lives in a new `src/tempus/remote.py`; the commands go in `cli.py` beside
`db init` / `db status`.

**Tunnel.** `Tunnel(host, remote_port)` is a context manager that starts
`ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -L 127.0.0.1:<free port>:127.0.0.1:<remote port> <host>`,
waits up to 10 seconds for the local port to accept connections (failing early if `ssh`
exits), and terminates it on exit, including on Ctrl-C. For long-running processes
(`tempus serve`) a watchdog thread restarts `ssh` on the same local port if it dies (the
laptop slept), with backoff, so connection URLs stay valid. SSH, the `homebase` alias and its
keys are the only transport; no port opens on either machine.

**Settings** (all optional; without them, dev behaves exactly as today):

| Setting | Default | Purpose |
|---|---|---|
| `TEMPUS_HOMEBASE_HOST` | `homebase` | SSH host or alias (shared with `mise run homebase:zed`) |
| `TEMPUS_PROD_DB_PORT` | `5432` | Postgres port on the mini (its `TEMPUS_DB_PORT`) |
| `TEMPUS_PROD_RO_PASSWORD` | – | `tempus_ro`'s password: pull and read-only mode |
| `TEMPUS_PROD_PASSWORD` | – | prod's owner (`postgres`) password, the mini's `TEMPUS_DB_PASSWORD`: push only |
| `TEMPUS_DB` | `dev` | `dev`: `DATABASE_URL` as today. `prod`: prod through the tunnel, read-only |

The passwords travel like the API keys (chezmoi `secrets push|pull`), never in git.

**Read-only role.** `mise run homebase:setup` gains an idempotent step on the mini: create
`tempus_ro` (LOGIN, `ALTER ROLE … SET default_transaction_read_only = on`), grant
`USAGE ON SCHEMA public` and `SELECT ON ALL TABLES IN SCHEMA public`, and
`ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES` so
tables added later are covered. TimescaleDB propagates grants on a hypertable to its chunks;
a test confirms reads of `observations` work as `tempus_ro`. The first run generates a random
password into the mini's `.env` as `TEMPUS_DB_RO_PASSWORD` and prints the line to copy to the
MacBook. Postgres enforces read-only, so no tempus bug can write through this role.

**`tempus db where`** prints the mode (dev or prod), host, role, TimescaleDB version and
series count: a one-line answer to "which database am I on?".

**Guards shared by pull and push:** the dev side must be local (`DATABASE_URL` host is
`localhost`, `127.0.0.1`, `::1` or a socket), and `TEMPUS_DB` must be `dev`. Both sides must
have the columns the copy needs; a missing column stops with "run `tempus db init` on <side>".

### 2. `tempus db pull` (prod → dev)

**Copies** `series`, `observations` and `series_concepts`, plus `jev_decisions` once SPA-16
creates it. The table list lives in one place in `remote.py`.

**Selection:** everything by default; `--series fred:UNRATE,fred:CPIAUCSL` or
`--concept labor` (the concept and its descendants, by prod's placements) for a subset;
`--metadata-only` copies `series` and `series_concepts` but no observations.

**Unpushed-work guard.** Before writing, pull computes the push plan (section 3) for the
selected series. If it isn't empty, dev holds syncs or filings prod doesn't have; pull lists
them and stops. The user runs `tempus db push --yes` first, or `tempus db pull --discard-local`.

**Mechanics,** in one dev transaction so a failed pull leaves dev untouched:

1. Connect to prod through the tunnel as `tempus_ro`.
2. Rebuild git-derived tables in dev: `ontology load`, `card load` (the same code the CLI
   commands call).
3. Clear the selection in dev: `TRUNCATE series CASCADE` for a full pull, else `DELETE FROM
   series WHERE (source, series_id) IN (…)`; the foreign keys cascade to observations and
   placements.
4. Stream each table with `COPY (SELECT <cols> FROM <table> WHERE <selection>) TO STDOUT`
   on prod into `COPY <table> (<cols>) FROM STDIN` on dev. The `SELECT` form is required:
   plain `COPY observations TO` on a hypertable returns no rows. `<cols>` is the intersection
   of both sides' columns. Binary format when both servers share a major version, else text.
5. Placements arrive through a temporary staging table and are inserted only where the
   concept exists in dev's tree (or is NULL); the rest are counted and reported ("12
   placements skipped: concepts not in this branch's ontology").
6. Print rows per table and elapsed time.

### 3. `tempus db push` (dev → prod, merge only)

**The unit is a whole series:** its `series` row, all its observations, its placement.
Observations from two copies are never interleaved.

**Candidates** are series dev has synced (`last_synced_at` set). For each, the provider's
revision time decides:

| Dev vs prod | Plan |
|---|---|
| Prod has no row, or never synced it | **push** (new data) |
| Dev's `upstream_updated_at` is later | **push** (newer revision) |
| Equal `upstream_updated_at` | skip (same data) |
| Prod's is later | skip, listed as "prod is ahead: pull" |

**Placements** are planned separately, so a filing done in dev ships even when the series
data is the same: a dev placement replaces prod's when its method ranks higher (manual >
auto > draft), or ranks equal with a later `filed_at`. A placement whose concept is missing
from prod's tree is skipped and reported (the ontology change hasn't shipped).
**`jev_decisions`**, once it exists, is insert-only (`ON CONFLICT DO NOTHING`).

**Never deletes** anything in prod beyond the observations of a series it is replacing.

**Dry run by default.** `tempus db push` prints the plan: each series, why, observation
count, placement changes. `--yes` applies it. `--series` / `--concept` narrow it.

**Mechanics:** connect through the tunnel with `TEMPUS_PROD_PASSWORD`; one prod transaction;
`SELECT … FOR UPDATE` on the affected `series` rows first, so a scheduled sync on the mini
waits rather than interleaving; then per series: upsert the `series` row, delete its
observations, `COPY` dev's in, apply the placement. Each pushed series appends a `sync_runs`
row with status `pushed` and `rows_upserted` = observation count, so prod's log shows the
data came from a push rather than the provider.

**Workflows this enables:**

- Backfill in dev, ship once: `tempus sync …`, then `tempus db push --yes`.
- Start from prod: `tempus db pull --series X`, then `tempus sync X` fetches only what
  changed since prod's copy (sync already skips a series whose `upstream_updated_at` hasn't
  moved).

### 4. Read-only prod mode

**`TEMPUS_DB=prod`** applies to `tempus serve` and every read command. The CLI's root
callback opens the tunnel, swaps `database_url` for the `tempus_ro` URL, and holds the
tunnel for the process lifetime. A missing `TEMPUS_PROD_RO_PASSWORD` or an unreachable mini
fails immediately; it never falls back to dev.

**App.** `create_app` gains `read_only: bool`, split out of `guest`: `read_only` turns
sync-on-open off (series pages say "read-only: sync on the home base"); `guest` keeps its
password gate and hidden API docs and implies `read_only`. A new `GET /api/meta` returns
`{"read_only": bool, "remote": bool}`, where `remote` means the database is reached through
the tunnel (`TEMPUS_DB=prod`). The front end shows a small "prod · read-only" badge in the
header when `remote` is true, styled per `docs/DESIGN.md`. The mini's own apps (tailnet and
guest) are not remote, so they never show it.

Cards come from local `cards/*.yaml` files, as today, so card and UI work behave the same
against prod. The concept tree comes from prod's `concepts` table: a branch that edits
`ontology/concepts.yaml` sees prod's tree in this mode until the change ships. Documented,
not worked around.

**CLI guard.** Commands that write (`sync`, `db init/pull/push`, `catalog add/remove/import-snapshot`,
`ontology load/file`, `card load`, `series delete`) are marked as writers and refuse before
connecting when `TEMPUS_DB=prod`. If a write slips through, Postgres's
`read_only_sql_transaction` error is caught and shown as the same message.

### 5. Errors

One line each, in the CLI's existing style:

- SSH fails or the tunnel times out: "can't reach homebase over SSH (Tailscale up? Remote
  Login on?)", with `ssh`'s own last line.
- Authentication fails: names the setting to fix.
- Schema mismatch: "run `tempus db init` on the mini" (or on dev).
- Dev isn't local, or `TEMPUS_DB=prod` with pull/push: says which guard and why.

A failed push rolls back entirely; a failed pull leaves dev untouched.

## Changes during implementation

- Series metadata and observations always move whole. Of the placements, only `auto` ones
  (Jev placements) move in either direction; pins and drafts come from git on each side
  (pushing drafts would ping-pong, since every `ontology load` refreshes their `filed_at`).
- `tempus db readonly-role` creates the role; `homebase:setup` calls it.
- `/api/meta` returns `remote` (from `TEMPUS_DB=prod`) rather than a `database` name, and
  series pages carry `read_only` so the page can say why it won't sync.
- `pull --series` keeps dev's copy of a series prod doesn't have and reports it.
- `--concept` matches descendants with `starts_with`, not `LIKE` (`_` in ids is a wildcard).
- The spec, plan and code landed in one PR (#13); `docs/specs/README.md` now describes that
  flow.

## One-way doors

None. Each piece is cheap to reverse:

| Choice | Reversal |
|---|---|
| `tempus_ro` role | `DROP ROLE`; nothing references it |
| `pushed` value in `sync_runs.status` | Free text, no constraint; filter it out or rewrite rows |
| Merge rule | Code; nothing stored depends on it |
| `/api/meta` payload | Read only by the bundled front end (same status as the explore payloads) |
| No extract file format | Deliberately deferred; this is the door kept closed |

## Testing

- **Integration (DB):** two throwaway databases in the test container, `tempus_test_dev` and
  `tempus_test_prod`, with the tunnel replaced by direct URLs (the transport is a parameter).
  Cases: full and narrowed pull; `--metadata-only`; the unpushed-work guard and
  `--discard-local`; placements skipped for missing concepts; every row of the push table;
  placement precedence; push never deleting other series; the dry run writing nothing;
  `jev_decisions` insert-only (when present); `tempus_ro` reading `observations` and failing
  to write; the dev-is-local and `TEMPUS_DB=prod` guards.
- **Unit:** `Tunnel` with a fake `ssh` on `PATH`: the command it builds, waiting for the port,
  failing fast when `ssh` exits, the watchdog restart.
- **App:** `read_only` turns sync-on-open off; `/api/meta` for dev, remote and guest; `guest`
  still implies read-only.
- **Manual, against the real mini (in the plan):** `db where` in both modes; pull a few
  series; sync one in dev; push dry run and `--yes`; `TEMPUS_DB=prod tempus serve` shows the
  badge and refuses a sync.

## Rollout

Sub-issues of SPA-46, in order:

1. Tunnel, settings, `db where`, `tempus_ro` in `homebase:setup`.
2. `db pull` with the guard.
3. `db push`.
4. Read-only mode: `read_only` flag, `/api/meta`, badge, CLI writer guard.
5. Docs (below); spec status → Implemented.

## Docs to update when implemented

- `docs/HOMEBASE.md`: "Dev and prod databases" (setup once, pull, push, read-only mode),
  replacing "Use the mini's database from the MacBook".
- `README.md`: CLI section (`db pull`, `db push`, `db where`, `TEMPUS_DB`).
- `docs/ARCHITECTURE.md`: a component (Remote: tunnel, pull, push, read-only mode), a data
  flow line, and a dated decision: copy over an SSH tunnel with `COPY`, merge by whole series
  on the provider's revision time; not extract files, not replication.
- `docs/STACK.md`: Open decisions row, "Prod data in cloud sessions: extract files later".
- `.env.example`: the new settings, commented out.

## Open questions

- **Cloud sessions and prod data.** When they need more than the snapshot and replay
  fixtures, choose an extract format (and where files live). Revisit when a cloud task is
  blocked on real observations.
- **Should `TEMPUS_DB=prod tempus sync` run the sync on the mini over SSH** instead of
  refusing? Convenient, but it blurs "read-only"; left out until it's missed.
