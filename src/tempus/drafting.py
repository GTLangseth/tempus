"""Draft a card that answers a question: docs/specs/2026-10-05-question-to-card-design.md.

Three stages, each a separate decision so the eval can say which one failed:

1. **Shape.** Jev chooses one of SHAPES for the question.
2. **Picks.** For each of the shape's roles, `navigate` finds the series that fills it.
3. **Assembly.** The shape's `build` writes a plain v1 card from the picks.

SHAPES is the only place that knows about shapes. A new shape is one entry here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
import yaml

from tempus.cards.spec import Card
from tempus.cards.values import FREQ_ORDER, freq_from_fred
from tempus.explore import SourceFactory, catalog_from_db
from tempus.filing import Chooser
from tempus.jev import ChoiceAnswer
from tempus.navigate import navigate
from tempus.sync import sync_series


@dataclass(frozen=True)
class Pick:
    """One role, resolved to a series."""

    role: str
    series: str  # "fred:DCOILWTICO"
    probability: float
    runners_up: list[tuple[str, float]] = field(default_factory=list)


class DraftError(ValueError):
    """The picks can't make a sensible card, e.g. a spread of two series in different units."""


# Each picked series ref -> its row: title, units, frequency, observation_start.
Meta = dict[str, dict]
# build(question, picks, meta, chooser) -> Card; `chooser` serves shapes that need one more
# decision. Raises DraftError when the picks can't make a sensible card.
Build = Callable[[str, list[Pick], Meta, Chooser], Card]


@dataclass(frozen=True)
class Shape:
    name: str
    describe: str  # the criterion text Jev reads
    roles: tuple[str, ...]
    ordered: bool  # False: the roles may be filled in either order (compare)
    build: Build


@dataclass(frozen=True)
class Draft:
    question: str
    shape: ChoiceAnswer  # Jev's choice over SHAPES, with every probability
    picks: list[Pick]  # one per role, in the shape's role order
    card: Card | None  # None when assembly failed; `error` says why
    error: str | None = None
    synced: list[str] = field(default_factory=list)  # picked series synced while drafting


# --- builders ----------------------------------------------------------------------------

REBASE_FLOOR = "2000-01-01"  # the earliest date a compare rebases at
PER_YEAR = {"W": 52, "M": 12, "Q": 4, "Y": 1}
Input = tuple[str, dict]  # (series ref, its meta row)


def _freq(row: dict) -> str:
    return freq_from_fred(row.get("frequency"))


def _coarsest(*freqs: str) -> str:
    known = [f for f in freqs if f in FREQ_ORDER]
    return max(known, key=FREQ_ORDER.__getitem__) if known else "M"


def _yearly(freq: str) -> str:
    """The frequency to take a change over a year at: yoy needs weekly or coarser."""
    return freq if freq in PER_YEAR else "M"


def _at(name: str, freq: str, target: str) -> str:
    return name if freq == target else f'resample({name}, "{target}", "mean")'


def _aligned(names: tuple[str, str], rows: tuple[dict, dict]) -> tuple[str, str]:
    """Both series at the coarser frequency, so arithmetic can line them up."""
    freqs = (_freq(rows[0]), _freq(rows[1]))
    target = _coarsest(*freqs)
    return _at(names[0], freqs[0], target), _at(names[1], freqs[1], target)


def _card(question: str, title: str, inputs: dict[str, Input], formulas, panels) -> Card:
    return Card.model_validate(
        {
            "version": 1,
            "title": title,
            "question": question,
            "tags": ["drafted"],
            "inputs": {
                n: {"series": ref, "label": row["title"]} for n, (ref, row) in inputs.items()
            },
            "formulas": formulas,
            "panels": panels,
        }
    )


def _inputs(picks: list[Pick], meta: Meta) -> list[Input]:
    return [(p.series, meta[p.series]) for p in picks]


def _level(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    [x] = _inputs(picks, meta)
    panels = [{"type": "stat", "y": "x"}, {"type": "timeseries", "title": "History", "y": ["x"]}]
    return _card(question, x[1]["title"], {"x": x}, {}, panels)


def _change(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    [x] = _inputs(picks, meta)
    freq = _freq(x[1])
    at = _yearly(freq)
    if "Percent" in (x[1].get("units") or ""):
        # The yoy of a rate is a percent of a percent; the change in points is what's meant.
        formulas = {"x_change": f"diff({_at('x', freq, at)}, {PER_YEAR[at]})"}
        stat = {"type": "stat", "y": "x"}
        shown = {"type": "timeseries", "title": "Change from a year earlier", "y": ["x_change"]}
    else:
        formulas = {"x_yoy": f"yoy({_at('x', freq, at)})"}
        stat = {"type": "stat", "y": "x", "change": "yoy"}
        shown = {"type": "timeseries", "title": "YoY change", "y": ["x_yoy"]}
    title = f"{x[1]['title']}, change over a year"
    return _card(question, title, {"x": x}, formulas, [stat, shown])


def _compare(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    a, b = _inputs(picks, meta)
    title = f"{a[1]['title']} vs {b[1]['title']}"
    if a[1].get("units") == b[1].get("units"):
        panels = [{"type": "timeseries", "y": ["a", "b"]}]
        return _card(question, title, {"a": a, "b": b}, {}, panels)
    at = max([REBASE_FLOOR, *(r.get("observation_start") or REBASE_FLOOR for _, r in (a, b))])
    formulas = {"a_rebased": f'rebase(a, "{at}")', "b_rebased": f'rebase(b, "{at}")'}
    panels = [{"type": "timeseries", "title": "Rebased to 100", "y": ["a_rebased", "b_rebased"]}]
    return _card(question, title, {"a": a, "b": b}, formulas, panels)


def _spread(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    a, b = _inputs(picks, meta)
    if a[1].get("units") != b[1].get("units"):
        raise DraftError(
            f"a spread needs both series in the same units: {a[1]['title']} is in "
            f"{a[1].get('units')}, {b[1]['title']} is in {b[1].get('units')}"
        )
    left, right = _aligned(("a", "b"), (a[1], b[1]))
    panels = [
        {"type": "stat", "y": "spread"},
        {"type": "timeseries", "title": "Spread", "y": ["spread"]},
    ]
    title = f"{a[1]['title']} minus {b[1]['title']}"
    return _card(question, title, {"a": a, "b": b}, {"spread": f"{left} - {right}"}, panels)


def _ratio(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    num, den = _inputs(picks, meta)
    top, bottom = _aligned(("num", "den"), (num[1], den[1]))
    panels = [{"type": "timeseries", "title": "Ratio", "y": ["ratio"]}]
    title = f"{num[1]['title']} / {den[1]['title']}"
    return _card(question, title, {"num": num, "den": den}, {"ratio": f"{top} / {bottom}"}, panels)


def _lead_lag(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    leader, follower = _inputs(picks, meta)
    freqs = (_freq(leader[1]), _freq(follower[1]))
    at = _yearly(_coarsest(*freqs))
    formulas = {
        "leader_yoy": f"yoy({_at('leader', freqs[0], at)})",
        "follower_yoy": f"yoy({_at('follower', freqs[1], at)})",
        "correlation": "lagcorr(leader_yoy, follower_yoy, nlags=12)",
    }
    panels = [
        {"type": "timeseries", "title": "YoY change", "y": ["leader_yoy", "follower_yoy"]},
        {"type": "lagcorr", "data": "correlation"},
    ]
    title = f"{leader[1]['title']} → {follower[1]['title']}"
    return _card(question, title, {"leader": leader, "follower": follower}, formulas, panels)


LINES = {"zero": "zero", "mean": "its long-run mean"}


def _threshold(question: str, picks: list[Pick], meta: Meta, chooser: Chooser) -> Card:
    [x] = _inputs(picks, meta)
    line = chooser.choice(
        {"query": question, "series": x[1]["title"]},
        "Which line does the question measure this series against?",
        LINES,
    ).choice
    if line == "zero":
        formulas, y = {}, "x"
        shown = {"type": "timeseries", "title": "Against zero", "y": ["x"]}
    else:
        formulas, y = {"x_z": "zscore(x)"}, "x_z"
        shown = {"type": "timeseries", "title": "Standard deviations from the mean", "y": ["x_z"]}
    title = f"{x[1]['title']} against {LINES[line]}"
    return _card(question, title, {"x": x}, formulas, [{"type": "stat", "y": y}, shown])


SHAPES: dict[str, Shape] = {
    s.name: s
    for s in (
        Shape(
            "level",
            "Asks what one measure is or where it stands now: its current value and history.",
            ("the measure",),
            True,
            _level,
        ),
        Shape(
            "change",
            "Asks how fast one measure is rising or falling: its growth rate, its change over a "
            "year, or how hot or cool it is running (inflation, growth, wage gains).",
            ("the measure",),
            True,
            _change,
        ),
        Shape(
            "compare",
            "Asks to see two measures side by side, how they move together or diverge.",
            ("the first measure", "the second measure"),
            False,
            _compare,
        ),
        Shape(
            "spread",
            "Asks for the gap between two measures in the same units: one minus the other.",
            ("the measure subtracted from", "the measure subtracted"),
            True,
            _spread,
        ),
        Shape(
            "ratio",
            "Asks for one measure in terms of another: one divided by the other. Includes one "
            "index or price relative to another (stocks in gold, Nasdaq relative to the S&P) and "
            "a real, inflation-adjusted price (a price divided by a price index).",
            ("the numerator", "the denominator"),
            True,
            _ratio,
        ),
        Shape(
            "lead_lag",
            "Asks whether one measure moves first and another follows, and by how long.",
            ("the measure that moves first", "the measure that responds later"),
            True,
            _lead_lag,
        ),
        Shape(
            "threshold",
            "Asks whether one measure is above or below a line, such as zero or its normal level.",
            ("the measure",),
            True,
            _threshold,
        ),
    )
}


def assemble(
    shape: Shape, p: float, question: str, picks: list[Pick], meta: Meta, chooser: Chooser
) -> Card:
    """The shape's card, with how it was drafted in `notes`. Raises DraftError."""
    card = shape.build(question, picks, meta, chooser)
    lines = [f"Drafted as {shape.name} (p {p:.2f})."]
    lines += [f"{pk.role}: {pk.series} (p {pk.probability:.2f})" for pk in picks]
    return card.model_copy(update={"notes": "\n".join(lines) + "\n"})


# --- the drafter -------------------------------------------------------------------------

SHAPE_QUESTION = "What kind of answer does this question ask for?"
RUNNERS_UP = 4


class Drafter(Protocol):
    def draft(self, question: str) -> Draft: ...


class JevDrafter:
    """Jev decides every stage: the shape, then one `navigate` per role. Code builds the card."""

    def __init__(
        self,
        conn: psycopg.Connection,
        chooser: Chooser,
        *,
        sync_source: SourceFactory | None = None,
    ):
        self.conn, self.chooser, self.sync_source = conn, chooser, sync_source
        self.catalog = catalog_from_db(conn)

    def draft(self, question: str) -> Draft:
        criteria = {name: s.describe for name, s in SHAPES.items()}
        answer = self.chooser.choice({"query": question}, SHAPE_QUESTION, criteria)
        shape = SHAPES[answer.choice]
        picks: list[Pick] = []
        for role in shape.roles:
            pick = self._pick(question, role, {p.series for p in picks})
            if pick is None:
                return Draft(question, answer, picks, None, f"no series found for {role}")
            picks.append(pick)
        synced = [p.series for p in picks if self._sync(p.series)]
        meta = {p.series: self._meta(p.series) for p in picks}
        try:
            card = assemble(
                shape, answer.probabilities[shape.name], question, picks, meta, self.chooser
            )
        except DraftError as exc:
            return Draft(question, answer, picks, None, str(exc), synced)
        return Draft(question, answer, picks, card, None, synced)

    def _pick(self, question: str, role: str, taken: set[str]) -> Pick | None:
        found = navigate(question, self.catalog, self.chooser, role=role).series
        ranked = [
            (ref, s["p"]) for s in found if (ref := f"{s['source']}:{s['series_id']}") not in taken
        ]
        if not ranked:
            return None
        (ref, p), rest = ranked[0], ranked[1 : 1 + RUNNERS_UP]
        return Pick(role, ref, p, rest)

    def _sync(self, ref: str) -> bool:
        """Sync a picked series that has no observations yet, as a series page does on open."""
        source, series_id = ref.split(":", 1)
        if self.sync_source is None or self._meta(ref)["observations"]:
            return False
        src = self.sync_source(source)
        return src is not None and sync_series(self.conn, src, series_id).status != "failed"

    def _meta(self, ref: str) -> dict:
        source, series_id = ref.split(":", 1)
        return self.conn.execute(
            """
            SELECT s.title, s.units, s.frequency,
                   coalesce(s.extra->>'observation_start', o.first::date::text)
                       AS observation_start,
                   o.n AS observations
            FROM series s,
                 LATERAL (SELECT min(ts) AS first, count(*) AS n FROM observations
                          WHERE source = s.source AND series_id = s.series_id) o
            WHERE s.source = %s AND s.series_id = %s
            """,
            (source, series_id),
        ).fetchone()


# --- the card as a file ------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Collections inline (`{series: …, label: …}`, `[a, b]`), long text as a `|` block."""


class _Line(dict):
    """The one `key: value` line being written, the only block-style mapping."""


_Dumper.add_representer(
    str,
    lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None),
)
_Dumper.add_representer(dict, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v, True))
_Dumper.add_representer(list, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, True))
_Dumper.add_representer(_Line, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v, False))


def _entry(key: str, value, indent: str = "") -> str:
    text = yaml.dump(_Line({key: value}), Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                     width=1000)  # fmt: skip
    return "".join(indent + line + "\n" for line in text.splitlines())


def card_yaml(card: Card) -> str:
    """The card as cards/<id>.yaml would hold it, laid out like the hand-written cards."""
    data = card.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    head = "".join(_entry(k, data[k]) for k in data if k not in ("inputs", "formulas", "panels"))
    inputs = "inputs:\n" + "".join(_entry(k, v, "  ") for k, v in data["inputs"].items())
    out = [head, inputs]
    if data.get("formulas"):
        out.append("formulas:\n" + "".join(_entry(k, v, "  ") for k, v in data["formulas"].items()))
    panels = ""
    for panel in data["panels"]:
        body = "".join(_entry(k, v, "    ") for k, v in panel.items())
        panels += "  - " + body[4:]
    out.append("panels:\n" + panels)
    return "\n".join(out)
