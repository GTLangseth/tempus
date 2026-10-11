"""Compare several hand-picked series: docs/specs/2026-10-05-question-to-card-design.md,
"Compare several series". The card is a plain v1 card; the correlation is shown only."""

import math
from functools import reduce

import polars as pl
import psycopg

from tempus.cards.evaluate import evaluate, fetch
from tempus.cards.resolve import resolve
from tempus.cards.spec import Card
from tempus.cards.values import SCHEMA, Series
from tempus.drafting import PER_YEAR, REBASE_FLOOR, Meta, _at, _coarsest, _freq, _yearly

# mode -> (formula name suffix, panel title); levels plots the inputs themselves.
MODES = {
    "levels": (None, "Levels"),
    "rebased": ("rebased", "Rebased to 100"),
    "yoy": ("yoy", "Year-over-year %"),
    "zscore": ("z", "Standard deviations"),
}
MIN_MONTHS = 12  # fewer shared months than this and a correlation means little


def _units_match(meta: Meta) -> bool:
    return len({row.get("units") for row in meta.values()}) == 1


def modes(meta: Meta) -> list[str]:
    """The modes that make sense: plain levels only when every series has the same units."""
    return [m for m in MODES if m != "levels" or _units_match(meta)]


def default_mode(meta: Meta) -> str:
    return "levels" if _units_match(meta) else "rebased"


def _short(row: dict, ref: str) -> str:
    """The title's first six words, before any parenthetical: "Average Price: Gasoline,
    Unleaded Regular (Cost per Gallon…)" → "Average Price: Gasoline, Unleaded Regular"."""
    title = row.get("title") or ref
    return " ".join((title.split("(")[0] or title).split()[:6]).rstrip(",;:-")


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _formula(mode: str, name: str, row: dict, rebase_at: str) -> str:
    if mode == "rebased":
        return f'rebase({name}, "{rebase_at}")'
    if mode == "yoy":
        freq = _freq(row)
        return f"yoy({_at(name, freq, _yearly(freq))})"
    return f"zscore({name})"


def compare_card(refs: list[str], meta: Meta, mode: str | None) -> Card:
    """One timeseries panel of `refs` (inputs s1..sN) in `mode`, the default when None."""
    mode = mode or default_mode(meta)
    suffix, panel_title = MODES[mode]
    rows = [meta[ref] for ref in refs]
    names = [f"s{i}" for i in range(1, len(refs) + 1)]
    rebase_at = max([REBASE_FLOOR, *(r.get("observation_start") or REBASE_FLOOR for r in rows)])
    formulas = (
        {
            f"{n}_{suffix}": _formula(mode, n, row, rebase_at)
            for n, row in zip(names, rows, strict=True)
        }
        if suffix
        else {}
    )
    short = [_short(row, ref) for ref, row in zip(refs, rows, strict=True)]
    return Card.model_validate(
        {
            "version": 1,
            "title": " vs ".join(short),
            "question": f"How do {_joined(short)} move together?",
            "tags": ["compare"],
            "inputs": {
                n: {"series": ref, "label": row.get("title") or ref}
                for n, ref, row in zip(names, refs, rows, strict=True)
            },
            "formulas": formulas,
            "panels": [{"type": "timeseries", "title": panel_title, "y": list(formulas) or names}],
        }
    )


def correlation(conn: psycopg.Connection, refs: list[str]) -> dict:
    """Pearson correlations of the series' YoY changes at a common monthly frequency (or the
    coarsest of them, when one is quarterly or annual), over the dates all of them share.
    Evaluated through the card engine, so frequencies and gaps behave as on a card."""
    card = Card.model_validate(
        {
            "version": 1,
            "title": "correlation",
            "question": "correlation",
            "inputs": {f"s{i}": ref for i, ref in enumerate(refs, 1)},
            "panels": [{"type": "timeseries", "y": "s1"}],
        }
    )
    resolved, _ = resolve(conn, "compare", card)
    target = _coarsest("M", *(infos[0].freq for infos in resolved.values()))
    names = {s: f"c{s[1:]}" for s in card.inputs}
    formulas = {names[s]: f"yoy({_at(s, infos[0].freq, target)})" for s, infos in resolved.items()}
    card = card.model_copy(update={"formulas": formulas})
    data = fetch(conn, [i for infos in resolved.values() for i in infos])
    env, _ = evaluate("compare", card, resolved, data, (None, None))

    def changes(c: str) -> pl.DataFrame:
        value = env.get(c)
        df = value.df.drop_nulls() if isinstance(value, Series) else pl.DataFrame(schema=SCHEMA)
        return df.rename({"value": c})

    joined = reduce(
        lambda left, right: left.join(right, on="ts", how="inner"),
        (changes(c) for c in names.values()),
    ).sort("ts")
    months = joined.height * 12 // PER_YEAR[target]

    def r(a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        if months < MIN_MONTHS:
            return None
        value = joined.select(pl.corr(a, b)).item()
        return None if value is None or math.isnan(value) else round(value, 4)

    return {
        "refs": refs,
        "matrix": [[r(a, b) for b in names.values()] for a in names.values()],
        "months": months,
        "start": joined["ts"][0].isoformat() if months else None,
        "end": joined["ts"][-1].isoformat() if months else None,
    }
