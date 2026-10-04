"""The values formulas compute: series, sets of series, and tables."""

from dataclasses import dataclass, field
from datetime import date

import polars as pl

FREQS = ("D", "W", "M", "Q", "Y")
FREQ_ORDER = {f: i for i, f in enumerate(FREQS)}
SCHEMA = {"ts": pl.Date, "value": pl.Float64}


class EvalError(ValueError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Series:
    name: str
    label: str
    freq: str  # one of FREQS, or "irregular"
    df: pl.DataFrame  # ts: Date, value: Float64, sorted by ts
    units: str | None = None


@dataclass(frozen=True)
class SeriesSet:
    name: str
    members: tuple[Series, ...]


@dataclass(frozen=True)
class Table:
    name: str
    df: pl.DataFrame
    meta: dict = field(default_factory=dict)


Value = Series | SeriesSet | Table | int | float | str


def frame(points: list[tuple[date, float | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"ts": [p[0] for p in points], "value": [p[1] for p in points]}, schema=SCHEMA
    ).sort("ts")


def freq_from_fred(text: str | None) -> str:
    for prefix, freq in (
        ("Daily", "D"),
        ("Weekly", "W"),
        ("Monthly", "M"),
        ("Quarterly", "Q"),
        ("Annual", "Y"),
    ):
        if text and text.startswith(prefix):
            return freq
    return "irregular"


def resample_hint(a: Series, b: Series) -> str | None:
    if "irregular" in (a.freq, b.freq):
        return None
    finer, coarser = sorted((a, b), key=lambda s: FREQ_ORDER[s.freq])
    return f'Resample first: resample({finer.name}, "{coarser.freq}")'


def align(a: Series, b: Series, what: str) -> pl.DataFrame:
    if a.freq != b.freq:
        raise EvalError(
            f"{what} — frequencies differ ({a.freq} vs {b.freq}).", hint=resample_hint(a, b)
        )
    return (
        a.df.join(b.df, on="ts", how="inner", suffix="_b")
        .rename({"value": "a", "value_b": "b"})
        .sort("ts")
    )


def clip(s: Series, start: date | None, end: date | None) -> Series:
    df = s.df
    if start:
        df = df.filter(pl.col("ts") >= start)
    if end:
        df = df.filter(pl.col("ts") <= end)
    return Series(s.name, s.label, s.freq, df, s.units)
