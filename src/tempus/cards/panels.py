"""The panel catalog: the closed list of ways a card can show a result.

Adding a type means writing its entry: what it's for and when not to use it. Channel names
follow Vega-Lite; the closed registry follows dbt Charts (see docs/STACK.md → Inspirations).
"""

from dataclasses import dataclass

from tempus.cards.spec import Panel


@dataclass(frozen=True)
class PanelType:
    name: str
    accepts: tuple[str, ...]  # static types: series | set | lagcorr
    field: str  # the panel field holding the reference(s): "y" or "data"
    multiple: bool
    doc: str
    when_not: str


PANELS = {
    p.name: p
    for p in [
        PanelType(
            "timeseries",
            ("series", "set"),
            "y",
            True,
            "Trends over time on one shared axis, with zoom and a crosshair.",
            "Series in very different units: rebase() or zscore() them first. Dual "
            "axes are left out on purpose (Cleveland & McGill).",
        ),
        PanelType(
            "lagcorr",
            ("lagcorr",),
            "data",
            False,
            "Bars of correlation by lag from lagcorr(); the peak is highlighted and the "
            "header says which series leads.",
            "Trending series in levels: correlate yoy() or diff() instead.",
        ),
        PanelType(
            "stat",
            ("series",),
            "y",
            False,
            "The latest value, its change and a sparkline.",
            "When the path matters more than the latest point: use timeseries.",
        ),
        PanelType(
            "table",
            ("lagcorr", "series"),
            "data",
            False,
            "Raw values.",
            "As a card's only panel: tables show numbers but argue poorly.",
        ),
    ]
}

_ACCEPT_TEXT = {"series": "a series", "set": "a set of series", "lagcorr": "a lagcorr table"}


def accepts_text(ptype: PanelType) -> str:
    return " or ".join(_ACCEPT_TEXT[a] for a in ptype.accepts)


def panel_refs(panel: Panel, ptype: PanelType) -> list[str]:
    value = getattr(panel, ptype.field)
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)
