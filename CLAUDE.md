# tempus

Collect public time-series data into TimescaleDB and explore it. Start with
`docs/ARCHITECTURE.md` (the blueprint) and `README.md` (setup and CLI).

## Keep the blueprint current

When a change alters the system's shape, update `docs/ARCHITECTURE.md` in the same change.
That includes a new component or data source, a schema change, a new or reversed decision,
or a resolved open question. Add a dated entry under **Decisions** saying what was chosen
and why. Skip this for refactors and bug fixes.

## Working here

- `mise run test` and `mise run lint` must pass. Integration tests need the DB (`mise run db:up`).
- Run repo chores as mise tasks. Data actions are `tempus` CLI subcommands.
- New provider: implement `Source` in `src/tempus/sources/` and register it in `sources/__init__.py`.
