# Jev decisions: a pinned model, a decision store, and replayable fixtures

- **Status:** Draft
- **Linear:** SPA-15 (pin the Jev model version), SPA-16 (record decisions by input hash,
  with a needs-review band)
- **Horizon:** 1 · Explore (see `docs/SOUL.md`); a prerequisite for Cards → Converse
- **Related:** `docs/ARCHITECTURE.md` → Using Jev → Making decisions reproducible;
  `src/tempus/jev.py`, `src/tempus/filing.py`, `src/tempus/navigate.py` (PR #5);
  `ontology/placements.draft.yaml` (PR #5)

## Goal

Every Jev decision tempus relies on should be **pinned, recorded and reviewable**, and a
recorded decision should be **replayable without a key**. That last property is the new
idea here: if the store can be exported to a reviewed file in git, the same records become
test fixtures and give cloud agents (which have no `TYPESAFE_API_KEY`) real Jev answers to
work with, instead of a lexical stand-in.

**Done when:**
- `jev.py` sends a configured, pinned model (`JEV_MODEL`), never `jev-latest`, and every
  recorded decision carries the model it came from.
- Asking the same question twice (same state, instructions, options, model) costs one API
  call; the second answer comes from the store.
- Filing and navigation mark decisions inside the ±0.05 band around a threshold as **needs
  review**, and `tempus ontology review` lists them with their trails.
- `tempus jev export` writes decisions to `decisions/*.jsonl`; with `JEV_MODE=replay` and
  no key, `tempus ontology file`, `tempus resolve` and the test suite use those records and
  fail loudly on a miss instead of calling out.
- `tempus ontology file` re-decides every draft placement, and the ones where Jev disagrees
  with the draft (or the draft was flagged `low`) show up in review.

## Out of scope

Choosing which model version to pin (an implementation-time check against TypeSafe's model
list; see Open questions). Score and yes/no primitives beyond what the store needs to be
generic. Ontology upkeep (Claude proposes a branch, Jev checks it). A UI for review beyond
the CLI and the series page's existing placement card. Migrating recorded decisions across
model versions (an upgrade re-decides; it never rewrites history).

## Design

### 1. Pin the model (SPA-15)

- `Settings.jev_model: str` (env `JEV_MODEL`), default set to a fixed version string chosen
  at implementation time. `JevClient(api_key, model=…)` sends it; `jev-latest` is rejected
  with a clear error so it can't creep back in through `.env`.
- Upgrading is a deliberate change: bump `JEV_MODEL`, run `tempus ontology file --refile`,
  then `tempus ontology review --moved` lists placements whose concept changed versus the
  previous model's recorded decision. The old decisions stay in the store (they are keyed
  by model), so rolling back is just setting the old version again.

### 2. The decision store (SPA-16)

A decision is one call to a Jev primitive. Its **key** is a hash of the exact input:

```
key = sha256(canonical_json({
  "v": 1,                      # key version: bump if canonicalization ever changes
  "model": "jev-…",
  "primitive": "choice",       # choice | score | yesno
  "state": {...},              # what Jev sees (series metadata, or {"query": ...})
  "instructions": "...",
  "criteria": {"slug": "text", ...}
}))
```

Canonical JSON: UTF-8, NFC-normalized strings, keys sorted at every level, no
insignificant whitespace, floats as given. Criteria are hashed **sorted by key**: we treat
option order as not part of the question (see Open questions; if TypeSafe says order
matters, hash it in insertion order and bump `v`).

**Table** (new; one-way door, see below):

```sql
CREATE TABLE IF NOT EXISTS jev_decisions (
    key          text        PRIMARY KEY,          -- sha256 hex of the canonical input
    key_version  smallint    NOT NULL,
    model        text        NOT NULL,
    primitive    text        NOT NULL,             -- choice | score | yesno
    purpose      text        NOT NULL,             -- filing | navigate | ... (for export/filtering)
    request      jsonb       NOT NULL,             -- the canonical input, for audit and replay
    answer       jsonb       NOT NULL,             -- e.g. {"choice": "...", "probabilities": {...}}
    origin       text        NOT NULL,             -- live | imported
    decided_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jev_decisions_purpose_idx ON jev_decisions (purpose, decided_at);
```

Storing the full `request` costs a few KB per decision (series notes are capped at 1,500
characters already); at ~2,000 series × ~4 levels that is tens of MB at most, which is fine.

**Code seam.** Filing and navigation already take a `Chooser`. The store slots in as
wrappers, so callers don't change:

```
RecordingChooser(inner=JevClient, store, purpose)   # miss → ask Jev, record; hit → stored answer
ReplayChooser(store, purpose)                       # miss → raises DecisionMissing (never calls out)
```

`Store` has two implementations behind one protocol: `DbStore` (the table) and
`FileStore` (the exported JSONL, read-only, no database). The `Chooser` returned by a
factory depends on `JEV_MODE`:

| `JEV_MODE` | Key set? | Chooser |
|---|---|---|
| `live` (default) | yes | `RecordingChooser(JevClient, DbStore)` |
| `live` | no | error for filing; navigation uses the labeled lexical fallback (as in PR #5) |
| `replay` | either | `ReplayChooser(FileStore ∪ DbStore)`; a miss is an error naming the key and purpose |
| `off` | either | lexical fallback only (useful for offline UI work) |

Recording a hit never calls out, so the ±0.03 drift measured in ARCHITECTURE disappears
for anything already decided: the same input always gets the same answer.

### 3. The needs-review band

Thresholds stay where they are (filing: p ≥ 0.65 and a lead ≥ 0.2; navigation: a child must
reach 0.2 and beat "none of these"). A decision is **needs review** when any comparison it
turned on was within ±0.05 of its threshold. That is computed, not stored, so changing the
band doesn't need a migration:

- `filing.place` adds `"review": ["near min probability" | "near min lead"]` to the trail
  step that was close. A placement needs review if any step does. The trail already lives in
  `series_concepts.trail` (jsonb), so **no schema change** is needed for this.
- `navigate` adds the same marker to route steps; the explore panel shows a small
  `REVIEW` label next to a close step, and `ambiguous` keeps its current meaning.
- `tempus ontology review` lists placements that need review, newest first, with each
  level's top three choices; `--drafts` and `--moved` filter as described below. The
  series page's placement card shows the same markers.

### 4. Recorded decisions as fixtures

```
decisions/
  filing.jsonl        # one decision per line, sorted by key
  navigate.jsonl
```

Each line is a full record (`key`, `key_version`, `model`, `primitive`, `purpose`,
`request`, `answer`). The first line is a header `{"format": "tempus-jev-decisions",
"version": 1}` so the format can migrate.

- `tempus jev export [--purpose filing] [--model …]` writes them; `tempus jev import`
  loads a file into the table with `origin = 'imported'` (on conflict, keep the existing
  row). Both are deterministic, so the diff of an export is reviewable like any other text.
- **Tests:** `tests/conftest.py` gains a `replay_jev` fixture backed by `FileStore`. Tests
  that today script answers by hand (`ScriptedJev`) can additionally assert against real
  answers, e.g. "UNRATE files to `labor.unemployment.rate` under the pinned model". If a
  test's input changes, the replay misses and the failure names the key, which is the cue to
  re-record with a key and commit the new lines.
- **Cloud agents:** with `JEV_MODE=replay` they see the real decisions for everything that
  has been recorded and exported, with no key and no network. New questions fail loudly, so
  an agent can't silently substitute its own judgment for Jev's.
- What gets committed is a choice per purpose: filing decisions (small, stable, valuable)
  yes; navigation decisions only for a curated set of example phrases, since arbitrary user
  queries don't belong in git.

### 5. How draft placements flow to Jev review

PR #5 loads Claude-written drafts as method `draft`, lowest precedence. This spec turns them
into a review queue instead of something that silently disappears:

1. `tempus ontology file` (live mode) files every draft-placed series, as PR #5 already
   does, through `RecordingChooser`, so each answer is recorded.
2. When Jev's placement replaces a draft, the trail keeps the draft for comparison:
   `{"draft": "<concept>", "low": "<reason or null>"}`. Agreement means Jev reached the
   draft's concept or one of its ancestors or descendants.
3. `tempus ontology review --drafts` lists: (a) disagreements, (b) drafts that were flagged
   `low`, and (c) Jev placements in the needs-review band, each with both answers side by
   side. The person resolves each by doing nothing (accept Jev), pinning in `catalog.toml`,
   or editing the tree.
4. When no `draft` rows remain, `placements.draft.yaml` is deleted in a normal PR.

The draft is **never** shown to Jev as a hint: that would make the drafts' biases look
calibrated.

## One-way doors

| Door | Why it's hard to reverse | How this design keeps it cheap |
|---|---|---|
| New `jev_decisions` table | Stored data other code will read; dropping it loses history that can't be re-bought exactly (drift) | Additive (`CREATE TABLE IF NOT EXISTS`), nothing else references it by foreign key, and exports to JSONL keep a copy in git |
| Key canonicalization | Changing it orphans every recorded decision | `key_version` in the key and the row; a new version recomputes keys from the stored `request`, so no re-asking is needed |
| `decisions/*.jsonl` format | Files in git that tests and agents depend on | Versioned header line; `tempus jev import/export` is the only reader/writer |
| Treating option order as insignificant | If wrong, two different questions share a key | Flagged as an open question to confirm with TypeSafe before implementation |

Not one-way: the review band (computed), `JEV_MODE` (config), the CLI commands.

## Testing

- Unit: canonicalization (key order, NFC, criteria order), key stability across runs,
  `RecordingChooser` asks once per key, `ReplayChooser` raises on a miss and never touches
  the network (respx asserts no calls), band detection at exactly ±0.05.
- Integration (DB): record → export → import round trip into a fresh database; a migration
  test against a database created before the table existed.
- Fixtures: one committed `decisions/filing.jsonl` slice covering the 28 catalog series,
  recorded once with a key, replayed in CI.

## Rollout

1. SPA-15: `JEV_MODEL` and the client change (small; can land first).
2. SPA-16a: table, store, recording/replay choosers, factory, `JEV_MODE`.
3. SPA-16b: review band markers, `tempus ontology review` (`--drafts`, `--moved`).
4. SPA-16c: export/import, the committed filing fixtures, test helper.
5. Run `tempus ontology file` on the snapshot, review the draft queue, delete the drafts
   file.

## Docs to update when implemented

`ARCHITECTURE.md`: Using Jev (rules 1–4 move from intent to built, the "Where it's used"
status column), Data model (`jev_decisions`), Components (decision store), a dated
Decisions entry. `STACK.md`: the Jev register row (pinned version), Open decisions for
`JEV_MODE` and the fixture format. `README.md`: `tempus ontology review`, `tempus jev
export/import`, `JEV_MODE`.

## Open questions

1. **Which version to pin?** Check TypeSafe's published model list at implementation time;
   pin the newest stable one and record the choice in a Decisions entry.
2. **Does option order affect Jev's probabilities?** If yes, hash criteria in order.
3. **Should navigation decisions be recorded for every live query** (cheap, good for
   evaluation) or only for curated examples? This design records all of them in the
   database and exports only curated ones; the question is privacy versus usefulness.
4. **Band width per purpose:** ±0.05 matches the measured ±0.03 drift with margin. Should
   navigation, which is interactive and cheap to redo, use a narrower band?
