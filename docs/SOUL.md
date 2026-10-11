# Soul

Why tempus exists and where it's headed. `ARCHITECTURE.md` describes what the system is
today, and `STACK.md` explains why it's built with these tools. This doc is the north star
both are judged against. Change it rarely and deliberately.

## Why

Tempus is a personal lab for understanding the world through time series. It has three
jobs, and each one feeds the others:

1. **Track what matters.** Collect public data such as economic series, market prices and
   prediction markets. Shape it into views worth returning to, and keep them current
   without effort.
2. **Reason about markets.** Use those views to think about portfolio composition and
   hedging. Generate signals from them, and check today against past trends and
   precedents. "When has this happened before, and what followed?"
3. **Learn by doing.** Act as a playground for teaching myself advanced time-series
   analysis and forecasting. Each method I learn should become a tool tempus keeps, not a
   notebook I throw away.

## The core idea: cards

A **card** is the unit of thought in tempus. It's a saved, named view that bundles three
layers:

- **What:** the upstream metrics it draws on, with metadata about where they come from and
  what they mean.
- **How:** the ways those metrics are transformed, combined and compared. For example:
  YoY change, ratios and spreads, rebasing, lagged correlation, regression, a forecast.
- **Show:** how the result is presented, such as a chart type, overlays, annotations or a
  table.

Dashboards, grids and reports are all built from saved cards. Comparing and investigating
relationships isn't a separate feature: it's something a card does.

Cards are written in a **small semantic language that LLMs handle well**. It's declarative,
has a closed vocabulary, is validated before it runs, and is readable in git. An LLM
should be able to draft a card from a question ("does crude lead gasoline CPI?"). A person
should be able to review it like any other diff. The same cards later become the inputs to
signals.

## Where it's going

Roughly in order. Each horizon builds on the ones before it.

1. **Explore:** browse the concept tree, open any series, see its chart and metadata.
2. **Cards:** compose, save and revisit cards, and arrange them into dashboards.
3. **Converse:** an LLM explores the data and drafts cards through tempus's own tools
   (an MCP server), using the card language.
4. **Learn:** forecasting and analysis methods join the card vocabulary as I learn them,
   each with an explanation of what it does and when it fails.
5. **Widen:** add more sources, including prediction markets (as context next to the
   indicators they bet on), more markets, and possibly upstream warehouse metrics through
   a semantic layer.
6. **Signal:** turn cards into signals, backtest them honestly, and use them to guide
   portfolio composition and hedging.

The core bet sits across these horizons. Tempus is worth returning to only if a question
becomes a card that combines the right series in the right way to answer it. Watches,
precedents and digests are only as good as the cards underneath them. So drafting a card
from a question comes first, judged by a measured eval, even though it belongs to
Converse. The horizon order bends around it.

## Values

- **Understanding over automation.** Tempus supports decisions; it doesn't make them. A
  signal I can't explain isn't one I'll act on.
- **Honest with time.** Backtests use only what was known at that moment. That requires
  point-in-time data (vintages), because revised economic data makes the past look more
  predictable than it was.
- **Beautiful and fast.** Exploring should feel good. A chart worth keeping is worth
  making clear.
- **Declarative and reviewable.** Catalog, ontology and cards are text in git. The
  database can always be rebuilt from them and the upstream sources.
- **Teach as you build.** Each new method is documented well enough that future me (or an
  LLM) can understand why it's there.

## Not goals

- A trading system that executes orders.
- A multi-user product *yet*. Tempus is personal first. A customer-facing version may
  come later, so choices shouldn't rule it out (see the card storage decision in
  `STACK.md`).
- Covering every dataset. Depth on what matters beats breadth.
