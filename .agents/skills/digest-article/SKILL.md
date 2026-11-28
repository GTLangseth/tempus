---
name: digest-article
description: Digest a research article, macro letter, PDF, or URL into a card-backed tempus reading enriched with stored time-series data. Trigger when the user shares an article or asks to turn research into cards and readings.
---

Digest external research into `readings/<slug>.md` backed by ordinary `cards/*.yaml` files that stay live as `tempus sync` brings in new observations.

## Steps

1. **Inventory claims and charts.** Extract the thesis, every empirical time-series claim, the charts worth recreating or extending, and any institutional/survey claims that sit outside time-series providers.
   *Done when:* every core chart and empirical claim is listed, with non-store claims separated upfront.

2. **Resolve and sync series.** Map each empirical claim to stored data via `uv run tempus resolve "<phrase>"` or database queries; for gaps, search with `uv run tempus fred search "<query>"` and pull missing series into `catalog.toml` via `uv run tempus catalog add fred <ID> --sync`. If a dataset does not exist in any registered source, keep it marked `unverified` rather than substituting an unrelated proxy silently.
   *Done when:* every series used by planned cards and verified `claim` blocks is in `catalog.toml` and synced in the database (`last_synced_at` populated).

3. **Author and check 3–6 cards.** Recreate the article's key charts and add second-order context or post-publication updates as standard v1 cards in `cards/<slug>.yaml`. Inspect `uv run tempus card functions` and `uv run tempus card panels` before writing formulas; align frequencies before arithmetic or `lagcorr()`, and prefer `yoy()` / `rebase()` over dual axes. Run `uv run tempus card check` and `uv run tempus card load`.
   *Done when:* `uv run tempus card check` passes with zero errors and every card produces live payloads in `uv run tempus card show <id>`.

4. **Author and validate `readings/<slug>.md`.** When the full article text is available (e.g. a shared PDF or open essay), preserve the full prose with `[^n]` footnotes and `credit` colophon so the user can read the piece cleanly in **Essay** mode or interleaved with `claim`, `note`, and `card` context in **Annotated** mode (and scan claims/cards alone in **Digest** mode). Keep copyrighted source files in `readings/sources/` (gitignored). Run `uv run tempus reading check <slug>` and `uv run tempus reading show <slug>`.
   *Done when:* `uv run tempus reading check <slug>` exits 0 with zero errors or warnings.

---

## Reference

### Front matter (`readings/<slug>.md`)

```yaml
---
version: 1
title: Short Article Title
authors: ["Author One", "Author Two"]
source: Publication · Series or Issue
published: "2026-08"
url: https://example.com/article   # optional
credit: Written by ...             # optional colophon shown after endnotes
summary: Two-sentence thesis and what tempus adds.
concepts: [rates.policy.fed_funds, households.wealth.distribution]
tags: [macro, rates]
---
```

### Fenced blocks & footnotes

- **Embed a saved card:**
  ````markdown
  ```card
  card: coffee-pass-through
  caption: Optional reading-specific note below the question.
  start: 1980-01-01   # optional window override
  end: 2026-12-31
  ```
  ````
- **Grade a claim against the store:**
  ````markdown
  ```claim
  quote: "Exact or tightly paraphrased claim from the text."
  status: extended   # confirmed | extended | nuanced | unverified
  verdict: What the stored series actually show, with dates and figures.
  series: [fred:WFRBST01134, fred:WFRBSB50215]
  card: crude-to-gasoline   # optional link to an embedded card
  ```
  ````
- **Add a lab note (`note` block):**
  ````markdown
  ```note
  title: What to watch next in tempus
  - **Follow-up:** bullet or paragraph rendered in Annotated and Digest modes.
  ```
  ````
- **Footnotes:** use standard `[^1]` inline markers and `[^1]: ...` definitions; `tempus reading check` verifies every reference is defined and warns on unused notes.

### Claim statuses

- `confirmed`: stored observations match the article's numbers or trajectory.
- `extended`: newer prints past publication move further in or test the thesis.
- `nuanced`: stored data supports part of the claim, omits a slice (e.g. age cohort), or reveals a subtler pattern.
- `unverified`: the underlying dataset is not in `tempus` yet (passes `reading check` without requiring synced series).
