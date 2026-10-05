# tempus

Collect public time-series data into TimescaleDB and explore it. Start with
`docs/SOUL.md` (why and where it's headed), `docs/ARCHITECTURE.md` (the blueprint) and `README.md` (setup and CLI).

## Keep the blueprint current

When a change alters the system's shape, update `docs/ARCHITECTURE.md` in the same change.
That includes a new component or data source, a schema change, a new or reversed decision,
or a resolved open question. Add a dated entry under **Decisions** saying what was chosen
and why. Skip this for refactors and bug fixes.

## Working here

- Work is tracked in the Linear project **tempus** (team Spaceman, `SPA-` issues; milestones follow
  the horizons in `docs/SOUL.md`). Add new work there. Decisions still go in `docs/ARCHITECTURE.md`.
- Architectural work gets a spec in `docs/specs/`, reviewed as a PR; see `docs/specs/README.md`
  for how specs, branches and Linear issues fit together.
- `mise run test` and `mise run lint` must pass. Integration tests need the DB (`mise run db:up`).
- Run repo chores as mise tasks. Data actions are `tempus` CLI subcommands.
- Before a hard-to-reverse choice (stored data shape, file formats, public interfaces,
  patterns that spread across the code), stop and flag it as a one-way door. Say what
  reversing it would cost, offer a reversible alternative, and wait for a decision. Easy
  choices: pick a default and log it under **Open decisions** in `docs/STACK.md`.
- New tool or dependency: add a row to the register in `docs/STACK.md` with its license,
  the alternatives considered, and a revisit trigger.
- Visual work follows `docs/DESIGN.md` (design.md format); run `mise run design:lint` after editing it.
- New provider: implement `Source` in `src/tempus/sources/` and register it in `sources/__init__.py`.
