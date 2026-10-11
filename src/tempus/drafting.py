"""Draft a card that answers a question: docs/specs/2026-10-05-question-to-card-design.md.

Three stages, each a separate decision so the eval can say which one failed:

1. **Shape.** Jev chooses one of SHAPES for the question.
2. **Picks.** For each of the shape's roles, `navigate` finds the series that fills it.
3. **Assembly.** The shape's `build` writes a plain v1 card from the picks.

SHAPES is the only place that knows about shapes. A new shape is one entry here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from tempus.cards.spec import Card
from tempus.filing import Chooser
from tempus.jev import ChoiceAnswer


@dataclass(frozen=True)
class Pick:
    """One role, resolved to a series."""

    role: str
    series: str  # "fred:DCOILWTICO"
    probability: float
    runners_up: list[tuple[str, float]] = field(default_factory=list)


class DraftError(ValueError):
    """The picks can't make a sensible card, e.g. a spread of two series in different units."""


# build(question, picks, meta, chooser) -> Card. `meta` maps each picked series ref to its
# row (title, units, frequency); `chooser` serves shapes that need one more decision.
Build = Callable[[str, list[Pick], dict[str, dict], Chooser], Card]


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


def _todo(question: str, picks: list[Pick], meta: dict[str, dict], chooser: Chooser) -> Card:
    raise NotImplementedError


SHAPES: dict[str, Shape] = {
    s.name: s
    for s in (
        Shape(
            "level",
            "Asks what one measure is or where it stands now: its current value and history.",
            ("the measure",),
            True,
            _todo,
        ),
        Shape(
            "change",
            "Asks how fast one measure is rising or falling: its growth or change over a year.",
            ("the measure",),
            True,
            _todo,
        ),
        Shape(
            "compare",
            "Asks to see two measures side by side, how they move together or diverge.",
            ("the first measure", "the second measure"),
            False,
            _todo,
        ),
        Shape(
            "spread",
            "Asks for the gap between two measures in the same units: one minus the other.",
            ("the measure subtracted from", "the measure subtracted"),
            True,
            _todo,
        ),
        Shape(
            "ratio",
            "Asks for one measure relative to another: one divided by the other.",
            ("the numerator", "the denominator"),
            True,
            _todo,
        ),
        Shape(
            "lead_lag",
            "Asks whether one measure moves first and another follows, and by how long.",
            ("the measure that moves first", "the measure that responds later"),
            True,
            _todo,
        ),
        Shape(
            "threshold",
            "Asks whether one measure is above or below a line, such as zero or its normal level.",
            ("the measure",),
            True,
            _todo,
        ),
    )
}
