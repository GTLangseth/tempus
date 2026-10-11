# Question to card: drafting a card that answers a question, measured by an eval

- **Status:** Implemented (phase 1) 2026-10-11, PR #11
- **Linear:** SPA-43 (ask a question and get an accurate drafted card I can keep)
- **Horizon:** 3 · Converse, pulled ahead of the rest of Cards (see `docs/SOUL.md`)
- **Related:** `src/tempus/navigate.py`, `src/tempus/jev.py`, `src/tempus/cards/`,
  `docs/specs/2026-10-04-jev-decision-store-design.md` (SPA-16),
  `docs/ARCHITECTURE.md` → Using Jev

## Goal

A person types a question such as "does crude lead gasoline CPI?" and tempus drafts a card
that answers it: the right series, combined the right way, shown with a panel that makes
the answer visible. Everything else on the roadmap (watches, precedents, digests) assumes
the cards are right, so accuracy comes first and is measured before any UI is built.

**Done when (phase 1, this spec):**
- `evals/drafting.yaml` holds 20–30 real questions. Each one names the answer shape and the
  acceptable series for each role.
- `tempus card draft "<question>"` prints a card that passes `tempus card check`, plus how
  it was drafted: the shape Jev chose with its probabilities, and the series picked for
  each role with its probability and the runners-up.
- `tempus card draft --eval` runs every question, grades each draft stage by stage, and
  prints pass or fail per question with the failing stage, plus accuracy per stage and
  overall, across several runs.
- The baseline goes to the user, who sets the accuracy bar for phase 2.

Phase 2 (the **Draft a card** button, **Keep** and **Discard**) gets its own short
addendum once the bar is met. It is out of scope here.

## Out of scope

The MCP server (SPA-29). Jev checks on finished cards (SPA-28). New card-language syntax.
Series labels (SPA-18), unless the eval shows unit-unsafe transforms. Editing cards in the
browser.

## What the probe showed

Before writing this spec, a throwaway script asked Jev to choose an answer shape for 16
questions and checked what `/api/navigate` returned. These numbers come from one run of
labels written by the author. Treat them as direction, not a baseline.

- **Shape choice is strong.** Jev picked the labeled shape for 14 of 16 questions. One miss
  is arguable ("gold versus the dollar" scored `ratio` 0.77). The other is a real miss.
  "Does crude lead gasoline CPI?" scored `compare` 0.55 and `lead_lag` 0.37.
- **Whole-question navigation drops a series.** Navigating the full question returned every
  needed series for only 8 of 16 questions. For multi-series questions it usually finds
  the second-named thing and loses the first.
- **Navigating one role at a time recovers most of it.** Appending "(Find only the measure
  that moves first.)" to the query and navigating once per role put an acceptable series
  first in 9 of 18 slots and in the top five in 15 of 18. Several top-five cases were a
  defensible series that the author's acceptable lists left out (an average gasoline price
  for "gasoline").

So the hard part is resolving each role to the right series. Choosing the comparison is
the easy part. The design below makes these separate stages and grades each one, so a
failure points at the stage that caused it.

## Design

### The data shapes

```python
class Shape:              # a row in the SHAPES registry
    name: str             # "lead_lag"
    describe: str         # the criterion text Jev reads
    roles: tuple[Role]    # ("the measure that moves first", "the measure that responds later")
    ordered: bool         # False for compare: either order passes
    build: Callable[[Picks], Card]

class Pick:               # one role, resolved
    role: Role
    series: str           # "fred:DCOILWTICO"
    probability: float
    runners_up: list[tuple[str, float]]

class Draft:
    question: str
    shape: ChoiceAnswer   # Jev's choice over SHAPES, with every probability
    picks: list[Pick]     # one per role, in the shape's role order
    card: Card            # a plain card-language v1 card

class Drafter(Protocol):
    def draft(self, question: str) -> Draft: ...
```

The registry is the only place that knows about shapes. Adding a shape adds one entry: its
description, its roles, and the function that builds the card. No `if shape == ...`
branches elsewhere.

### The first shapes

Each shape builds a card from v1 functions and panels only.

| Shape | Roles | Card it builds |
|---|---|---|
| `level` | the measure | `stat` and `timeseries` of the series |
| `change` | the measure | `stat` with `change: yoy`, `timeseries` of `yoy(x)`, or of `diff(x, 12)` for series already in percent |
| `compare` | first, second (unordered) | `timeseries` of both, `rebase`d to a common start when units differ |
| `spread` | first, second | `timeseries` and `stat` of `a - b`. A unit mismatch fails the draft with a reason |
| `ratio` | numerator, denominator | `timeseries` of `a / b` |
| `lead_lag` | leader, follower | `timeseries` of both `yoy`s and a `lagcorr` panel, as in `cards/crude-to-gasoline.yaml` |
| `threshold` | the measure | `stat` and `timeseries`, against zero or against the long-run mean (`zscore`), chosen by Jev |

`threshold` cannot answer "for how long" yet. The card language has no function that
counts a streak. Adding one is an additive catalog change, listed under Open questions.

### The drafting stages

1. **Shape.** One Jev `choice` over the registry's `describe` texts, with the question as
   state.
2. **Picks.** For each of the shape's roles, run `navigate` with the role in the state:
   `{"query": question, "find": role}`. `navigate` takes the role as a parameter, so the
   string trick from the probe never reaches the code. The top-ranked series fills the
   role, and the runners-up are kept for display.
3. **Assembly.** The shape's `build` writes the card. It sets `question`, a title, and
   `tags: [drafted]`, and puts the chosen shape and probabilities in `notes`. Drafted cards
   need no new card fields, and `git log` plus the `drafted` tag answer "how many cards
   came from drafts".
4. **Data.** Snapshot series have no observations yet. The drafter syncs any picked series
   that has none, the same way a series page does on first open (`FRED_API_KEY`).

Jev decides at every stage and code builds the card. Nothing is generated. This matches
"Jev decides, Claude generates, code computes". If the eval shows that picks stay below the
bar after tuning, the next drafter to try uses Claude to write one phrase per role,
generating only inside that slot, and then navigates each phrase. That needs
`ANTHROPIC_API_KEY` and the `anthropic` SDK, which tempus doesn't have today, so it would
come back as a decision with a STACK row. Every drafter implements `Drafter` and runs
against the same eval, so they are compared on the same numbers.

### The eval file

`evals/drafting.yaml` is versioned and lives in git, next to `cards/` and `ontology/`:

```yaml
version: 1
cases:
  - id: crude-leads-gasoline-cpi
    question: Does crude lead gasoline CPI?
    shape: lead_lag
    roles:
      - [fred:DCOILWTICO, fred:DCOILBRENTEU, fred:MCOILWTICO]
      - [fred:CUSR0000SETB01, fred:CUUR0000SETB01]
  - id: gold-versus-dollar
    question: Gold versus the dollar
    shape: compare
    also: [ratio]                     # other shapes that also answer it
    roles:
      - [fred:NASDAQQGLDI]
      - [fred:DTWEXBGS, fred:DTWEXAFEGS]
```

Each entry under `roles` lists the acceptable series for one role, in the shape's role
order. `also` lists other shapes that count as correct. `xfail: <reason>` marks a question
tempus can't answer well yet (for example, no gold spot price in the snapshot). It is graded
like any other but reported separately, so a new source or function shows up as a measured
gain. The set includes two or three of these. The set covers every shape at least
twice, holds at least eight multi-series questions and three compound phrasings ("oil
price and gasoline"), and starts from the user's own habits: the release calendar, rates
and the curve, market prices, inflation and its pass-through. `mise run lint` validates the
file offline: every series ID must exist in the snapshot or the catalog, and every role
count must match its shape.

### Grading

A draft passes when every stage passes. The grader reports the first stage that failed:

| Stage | Passes when |
|---|---|
| `shape` | The chosen shape is `shape` or in `also` |
| `series` | Each role's pick is in that role's acceptable list. For unordered shapes, either order passes |
| `valid` | `tempus card check` accepts the card |
| `renders` | The card runs against the database with no errors, and its first panel has at least 24 points |

`--eval` runs every case three times by default (`--runs`), because Jev drifts by about
±0.03 between identical calls. For each stage it reports accuracy as the mean and the
range across runs, and it names the stage with the most failures. Once the SPA-16 decision
store lands, repeated runs replay stored decisions instead. `--json` writes the full report,
so a baseline can be committed and later runs compared against it.

## Changes during implementation

- **The role goes in the text Jev reads.** A separate `find` field in the state didn't steer
  Jev; the role now goes into the query ("… (Find only <role>.)") and into every question
  `navigate` asks. The lexical pre-filter still scores the user's own words.
- **Shape descriptions and role wording are tuned against the eval** (below). The `change`
  role asks for a level or index rather than a growth rate, and the `spread` roles ask for
  one leg rather than a ready-made spread. Rewording the `ratio` roles made things worse, so
  it was reverted.
- **Weekly resampling re-anchors.** `resample(x, "W")` was a no-op on an already-weekly series,
  so Thursday mortgage rates never met a daily series resampled to Monday weeks. "W" now
  always truncates to the week's start, and drafted pairs that meet at weekly resample both.
- **One filing fix.** `catalog.toml` pins `NASDAQQGLDI` (a gold price index) under
  commodities → metals; Jev had filed it under equity indexes by its name.
- **xfail** cases can't express "the data doesn't exist" (a role needs at least one series),
  so `gold-spot` is an ordinary case answered by the NASDAQ gold index.

## Baseline and tuning

Eval of 28 questions (26 scored, 2 xfail), measured on a full copy of prod:

| Version | Runs | Shape | Series | Passed |
|---|---|---|---|---|
| As specified (`evals/baselines/2026-10-11-jev-drafter.json`) | 3 | 0.88 | 0.39 | 0.39 |
| Role in the text Jev reads | 1 | 0.88 | 0.54 | 0.54 |
| + sharper `change` and `ratio` descriptions | 1 | 1.00 | 0.62 | 0.62 |
| + `change` role asks for a level | 1 | 1.00 | 0.69 | 0.69 |
| + `spread` roles ask for one leg; weekly fix | 1 | 1.00 | 0.77 | 0.77 |
| + gold index pinned under metals | 1 | 1.00 | 0.73 | 0.73 |
| Reworded `ratio` roles (reverted) | 3 | 1.00 | 0.68 | 0.68 |
| **Kept** (`evals/baselines/2026-10-11-jev-drafter-tuned.json`) | 3 | 1.00 | 0.74 (0.73–0.77) | **0.74** |

The phase 2 bar, set by the user, is 0.70 passed across three runs. Every draft that picked
the right series also checked and rendered. Remaining misses: core PCE vs target (picks the
growth-rate series), Sahm rule (picks UNRATE), crude → gasoline CPI (picks core CPI for
gasoline), wages and inflation (picks core CPI), gold vs dollar (the dollar side), S&P in
gold, and payrolls (1 of 3). The Claude-phrase drafter isn't needed yet.

## One-way doors

- **The eval file format** (approved 2026-10-11, with `xfail`). It becomes the regression suite, so later drafters are judged
  against it. To reverse it, convert about 25 cases with a script, which is cheap now and
  grows with every case. `version: 1` keeps that path open.
- **The shape names** (kept as written, 2026-10-11). They appear in the eval file, in `tempus card draft` output, and
  later in the UI and the MCP server. They never enter card files, so renaming one touches
  only the registry and the eval file.

The design keeps the card format unchanged on purpose. Drafted cards are ordinary v1 cards.
The write path from `tempus serve` to `cards/` waits for the phase 2 addendum.

## Testing

- Unit tests use a fake chooser, as `test_ontology.py` does. Given fixed choices, `draft`
  returns a card whose inputs, formulas, and panels equal literal expected values for every
  shape.
- Grader tests feed hand-built drafts and assert the exact stage and reason it reports.
- The eval itself needs `TYPESAFE_API_KEY` and is not part of `mise run test`. Replay
  fixtures from SPA-16 can bring it into CI later.

## Rollout

1. `navigate` takes a role, keeping today's behavior when no role is given.
2. The shape registry, `Draft`, and the drafter, behind `tempus card draft "<question>"`.
3. `evals/drafting.yaml` and its lint check, reviewed by the user, who adds their own
   questions.
4. The grader and `--eval`. Run the baseline and report it, with run count, range, and the
   stage that limits it.
5. Tune against the failures, starting with the weakest stage, and re-run after each
   change. Stop when the user's bar is met or when a stage stops improving. A stage that
   stops improving is the cue to try the Claude-phrase drafter.

## Docs to update when implemented

- `docs/ARCHITECTURE.md`: a Drafting component row, the data-flow line from question to
  card, and a dated decision naming the drafter the eval chose and why.
- `README.md`: `tempus card draft` and `--eval`.
- `docs/SOUL.md` gets its note in this spec's PR (see below).

## Open questions

- **A streak function.** Decided 2026-10-11: left to SPA-41 (watches), which needs the
  same idea. `threshold` cards show the current state only.
- **The phase 2 bar.** The user sets it after seeing the baseline.
- **Series the snapshot lacks.** Decided 2026-10-11: yes, two or three `xfail` cases.
