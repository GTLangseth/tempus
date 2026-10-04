---
version: alpha
name: Tempus — Neon Notebook
description: >-
  A retro graph-paper notebook that stays calm and neutral, so that hot arcade-neon
  numbers and chart lines can do the shouting. Draws on docs/design/C2.md (cream sketchbook,
  hairline cards, flat surfaces) and docs/design/C1.md (monospace lab labels, data-first
  restraint).
colors:
  canvas: "#f2efe6"
  paper: "#fbf9f3"
  grid: "#e2ddcf"
  hairline: "#d4cfbf"
  ink: "#1c1b19"
  graphite: "#4a4843"
  pencil: "#6b675e"
  primary: "#ff1f8f"
  neon-orange: "#ff5a1f"
  neon-violet: "#8b3dff"
  neon-cyan: "#00a3e8"
  neon-green: "#00b860"
  highlight-lime: "#b6ff00"
  highlight-yellow: "#fff200"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 72px
    fontWeight: 700
    lineHeight: 0.95
    letterSpacing: -0.03em
  headline:
    fontFamily: Space Grotesk
    fontSize: 36px
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: -0.03em
  title:
    fontFamily: Space Grotesk
    fontSize: 22px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.01em
  stat:
    fontFamily: JetBrains Mono
    fontSize: 40px
    fontWeight: 700
    lineHeight: 1
    letterSpacing: -0.02em
  body:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.55
  body-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: JetBrains Mono
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0.06em
    fontFeature: '"calt" 0, "liga" 0'
rounded:
  sm: 4px
  md: 8px
  pill: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  xxl: 64px
  grid-cell: 24px
components:
  page:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
  page-grid-line:
    backgroundColor: "{colors.grid}"
  divider:
    backgroundColor: "{colors.hairline}"
  feed-card:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: 24px
  card-question:
    textColor: "{colors.ink}"
    typography: "{typography.headline}"
  card-eyebrow:
    textColor: "{colors.pencil}"
    typography: "{typography.label}"
  card-notes:
    textColor: "{colors.graphite}"
    typography: "{typography.body-sm}"
  stat-figure:
    textColor: "{colors.primary}"
    typography: "{typography.stat}"
  highlight-mark:
    backgroundColor: "{colors.highlight-lime}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
  tag-chip:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 4px 10px
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.sm}"
    padding: 10px 18px
  button-primary-hover:
    backgroundColor: "{colors.neon-violet}"
    textColor: "{colors.paper}"
  highlight-mark-alt:
    backgroundColor: "{colors.highlight-yellow}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
  chart-series-1:
    textColor: "{colors.primary}"
  chart-series-2:
    textColor: "{colors.neon-cyan}"
  chart-series-3:
    textColor: "{colors.neon-orange}"
  chart-series-4:
    textColor: "{colors.neon-violet}"
  chart-series-5:
    textColor: "{colors.neon-green}"
  chart-axis:
    textColor: "{colors.pencil}"
    typography: "{typography.label}"
---

## Overview

Tempus looks like a well-used graph-paper notebook that someone has been marking up with
neon pens. The page itself is quiet: a warm cream canvas with a faint 24px grid, paper cards
with hairline edges, and near-black ink for anything you read. Color is saved for the things
we want you to notice: the headline number on a card, the series lines in a chart, and the
phrase in a question that matters. Because the background never competes, a single neon
figure on a card holds attention.

The feed is a column of cards, like pages torn out and pinned in order. Each card asks a
question in big ink type, answers it with one neon figure and a chart, and labels everything
else in small monospace "lab notes" text.

## Colors

- **Neutrals carry the page.** Canvas `#f2efe6` is the notebook. Grid `#e2ddcf` is the ruling
  and stays faint. Paper `#fbf9f3` lifts cards off the canvas. Hairline `#d4cfbf` draws card
  edges and dividers. Ink `#1c1b19` is used for all reading text. Graphite `#4a4843` is for
  secondary text, and pencil `#6b675e` for labels and axes.
- **Neon is for signal.** The "arcade" set is deliberately hot: primary pink `#ff1f8f`, cyan
  `#00a3e8`, orange `#ff5a1f`, violet `#8b3dff` and green `#00b860`. That heat costs
  contrast, so the colors split into two jobs:
  - **Type neons:** pink (3.4:1 on paper) and violet (4.7:1) may set large type (24px and up)
    such as stat figures and hero words. Never body or label text.
  - **Mark-only neons:** cyan (2.7:1), orange (3.0:1) and green (2.5:1) are too light for
    type. Use them only for chart lines, bars and dots, and always pair them with a legend
    chip or direct label so the line never relies on color alone.
- **Highlighters are fills, not ink.** Lime `#b6ff00` and yellow `#fff200` have about 1.1:1
  contrast on cream, so they only ever sit behind ink text, like a marker stroke. Ink on lime
  reaches 14:1.
- **Series order** for charts (`chart-series-1…5`): pink, cyan, orange, violet, green. A card's
  first series always takes pink, so its stat figure matches its main line. Secondary stats
  are set in ink with a small series-colored dot.

## Typography

- **Space Grotesk** (700) is the display voice. It is a techy grotesque with quirky
  details, tracked tight at -0.03em. Card questions use `headline`, and the occasional hero uses
  `display`.
- **JetBrains Mono** is the notebook-annotation voice. Use `label` (uppercase, open tracking,
  ligatures off) for eyebrows, units, axis ticks, sources and dates. Use `stat` for the neon
  headline number, which gives it tabular digits.
- **Inter** is for reading: notes, caveats and descriptions.
- All three are free on Google Fonts.

## Layout

- The canvas grid is 24px, and spacing snaps to it where it can (8 / 16 / 24 / 40 / 64).
- The feed is a single column with a 960px maximum width and 40px between cards. Dashboards
  may use a two-column grid of cards on wide screens.
- Inside a card the order is: eyebrow → question → stat row → chart → notes → footer labels,
  with 16px between items.
- Use a 16px side gutter on phones, and never scroll the page horizontally.

## Elevation & Depth

Every card is a sticker: paper with a 1px ink edge and a hard offset shadow
(`3px 3px 0 ink`), with no blur. It reads as a sticker pressed onto the page, not as a
floating layer. Nothing else casts a shadow. A pinned card is marked by `PINNED` in its
eyebrow, not by a different shadow.

## Shapes

Cards use crisp 8px corners. Buttons use 4px. Tags and chips are pills. Chart lines are a
fine, pen-like 2px with round joins, and points are only drawn at the last value, as a
4px dot with a neon fill and an ink ring.

## Components

- **Feed card:** paper surface, ink edge, sticker shadow, 24px padding. The eyebrow carries the concept
  path and frequency, such as `ENERGY / PRICES · MONTHLY`.
- **Card question:** ink headline. One key phrase gets a lime highlighter swipe across the
  lower half of the words only (roughly 52% to 92% of the line height), like a quick marker
  stroke.
- **Stat figure:** the latest value in pink (or violet) mono, with its unit and change in `label` beside it.
  A positive or negative change is shown by an arrow glyph, not by red or green alone.
- **Chart:** ink-free plot area. Gridlines use the grid color, axis text uses pencil `label`
  text, and series follow the neon order. Tooltips are ink boxes with paper text.
- **Tag chip:** pill with a hairline border and mono label, used for sources and concepts.
- **Primary button:** ink fill with paper text. It turns violet on hover.

## Do's and Don'ts

- Do keep each card to one neon figure. If everything glows, nothing does.
- Do let the grid show through the canvas, but never through cards.
- Do put lime or yellow behind ink text only. Never set text in those colors.
- Don't set body or label text in neon, and never set type in cyan, orange or green.
- Don't use gradients, glows or blurs. Neon here means flat, saturated ink on paper.
- Don't use pure white or pure black. Paper and ink are warm on purpose.
- Don't encode meaning with color alone. Pair it with a label, arrow or line style.
