"""The function catalog: every function a formula may call.

Each entry has a typed signature, a doc line and a caveat (what it does and when it misleads).
Names are borrowed from pandas, Datadog and PromQL (see docs/STACK.md → Inspirations).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

import polars as pl

from tempus.cards.values import FREQ_ORDER, FREQS, EvalError, Series, SeriesSet, Value

REQUIRED = object()
TRUNCATE = {"D": "1d", "W": "1w", "M": "1mo", "Q": "1q", "Y": "1y"}
AGGS = ("mean", "last", "sum", "min", "max")


class ArgError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # series | set | int | number | str | date
    default: object = REQUIRED
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Function:
    name: str
    params: tuple[Param, ...]
    returns: str  # same | series | lagcorr
    doc: str
    caveat: str
    impl: Callable[..., Value]
    broadcast: bool = True  # applies per member when the first argument is a SeriesSet
    windowed: bool = False  # series arguments are clipped to the card window first


CATALOG: dict[str, Function] = {}


def register(fn: Function) -> Function:
    CATALOG[fn.name] = fn
    return fn


def match_args(fn: Function, args: list, kwargs: dict) -> dict[str, object]:
    if len(args) > len(fn.params):
        raise ArgError(f"too many arguments for {signature(fn)}")
    bound = {p.name: a for p, a in zip(fn.params, args, strict=False)}
    known = {p.name for p in fn.params}
    for key, value in kwargs.items():
        if key not in known:
            raise ArgError(f"unknown argument '{key}' for {signature(fn)}")
        if key in bound:
            raise ArgError(f"argument '{key}' given twice")
        bound[key] = value
    for p in fn.params:
        if p.name not in bound:
            if p.default is REQUIRED:
                raise ArgError(f"missing argument '{p.name}' for {signature(fn)}")
            bound[p.name] = p.default
    return bound


def signature(fn: Function) -> str:
    parts = []
    for p in fn.params:
        if p.default is REQUIRED:
            parts.append(p.name)
        elif isinstance(p.default, str):
            parts.append(f'{p.name}="{p.default}"')
        else:
            parts.append(f"{p.name}={p.default}")
    return f"{fn.name}({', '.join(parts)})"


def _coerce(fn: Function, p: Param, value: object, first: bool) -> object:
    ok = {
        "series": isinstance(value, Series)
        or (first and fn.broadcast and isinstance(value, SeriesSet)),
        "set": isinstance(value, SeriesSet),
        "int": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "str": isinstance(value, str),
        "date": isinstance(value, str),
    }[p.kind]
    if value is None and p.default is None:
        return None
    if not ok:
        raise EvalError(f"{fn.name}: '{p.name}' must be a {p.kind}")
    if p.choices and value not in p.choices:
        raise EvalError(f"{fn.name}: '{p.name}' must be one of {', '.join(p.choices)}")
    if p.kind == "date":
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise EvalError(f"{fn.name}: '{p.name}' must be a date like 2000-01-01") from exc
    return value


def call(fn: Function, args: list[Value], kwargs: dict[str, Value]) -> Value:
    try:
        bound = match_args(fn, args, kwargs)
    except ArgError as exc:
        raise EvalError(str(exc)) from exc
    bound = {p.name: _coerce(fn, p, bound[p.name], i == 0) for i, p in enumerate(fn.params)}
    first = bound[fn.params[0].name]
    if isinstance(first, SeriesSet) and fn.params[0].kind == "series":
        members = tuple(fn.impl(**{**bound, fn.params[0].name: m}) for m in first.members)
        return SeriesSet(first.name, members)
    return fn.impl(**bound)


def _with(x: Series, expr: pl.Expr, label: str, freq: str | None = None) -> Series:
    df = x.df.select("ts", expr.alias("value"))
    return replace(x, df=df, label=label, freq=freq or x.freq)


def _safe_ratio(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    return pl.when(den == 0).then(None).otherwise(num / den)


# --- time shape -------------------------------------------------------------------------


def _resample(x: Series, freq: str, agg: str) -> Series:
    if x.freq == freq:
        return x
    if x.freq != "irregular" and FREQ_ORDER[freq] < FREQ_ORDER[x.freq]:
        raise EvalError(f"resample can only make a series coarser ({x.freq} → {freq} is finer)")
    v = pl.col("value")
    expr = {
        "mean": v.mean(),
        "last": v.drop_nulls().last(),
        "sum": pl.when(v.count() > 0).then(v.sum()),
        "min": v.min(),
        "max": v.max(),
    }[agg]
    df = (
        x.df.sort("ts")
        .group_by(pl.col("ts").dt.truncate(TRUNCATE[freq]), maintain_order=True)
        .agg(expr.alias("value"))
        .sort("ts")
    )
    return replace(x, df=df, freq=freq)


def _shift(x: Series, n: int) -> Series:
    return _with(x, pl.col("value").shift(n), f"{x.label}, shifted {n}")


def _fill(x: Series, method: str) -> Series:
    v = pl.col("value")
    expr = v.fill_null(strategy="forward") if method == "ffill" else v.interpolate()
    return _with(x, expr, x.label)


# --- change -----------------------------------------------------------------------------


def _diff(x: Series, n: int) -> Series:
    return _with(x, pl.col("value").diff(n), f"{x.label}, change")


def _pct_change(x: Series, n: int) -> Series:
    v = pl.col("value")
    return _with(x, (_safe_ratio(v, v.shift(n)) - 1) * 100, f"{x.label}, % change")


def _yoy(x: Series) -> Series:
    if x.freq not in ("W", "M", "Q", "Y"):
        raise EvalError(
            f"yoy needs a weekly, monthly, quarterly or annual series; '{x.name}' is {x.freq}.",
            hint=f'Resample first: resample({x.name}, "M")',
        )
    offset = "52w" if x.freq == "W" else "1y"
    prior = x.df.with_columns(pl.col("ts").dt.offset_by(offset)).rename({"value": "prior"})
    df = x.df.join(prior, on="ts", how="left").select(
        "ts", ((_safe_ratio(pl.col("value"), pl.col("prior")) - 1) * 100).alias("value")
    )
    return replace(x, df=df.sort("ts"), label=f"{x.label}, YoY %", units="%")


def _log(x: Series) -> Series:
    if (x.df["value"] <= 0).any():
        raise EvalError(f"log needs positive values; '{x.name}' has values ≤ 0")
    return _with(x, pl.col("value").log(), f"log {x.label}")


X = Param("x", "series")

register(
    Function(
        "resample",
        (X, Param("freq", "str", choices=FREQS), Param("agg", "str", "mean", AGGS)),
        "same",
        "Change frequency to a coarser one (D/W/M/Q/Y), aggregating each period.",
        "Timestamps mark the start of each period, as FRED does. 'mean' smooths "
        "spikes; use 'last' for end-of-period levels.",
        _resample,
    )
)
register(
    Function(
        "shift",
        (X, Param("n", "int")),
        "same",
        "Move values n periods later (negative n moves them earlier).",
        "Shifts by rows, so gaps in the series shift with it.",
        _shift,
    )
)
register(
    Function(
        "fill",
        (X, Param("method", "str", choices=("ffill", "linear"))),
        "same",
        "Fill gaps by carrying the last value forward or interpolating linearly.",
        "The only way gaps get filled. Filled points look like real data on charts.",
        _fill,
    )
)
register(
    Function(
        "diff",
        (X, Param("n", "int", 1)),
        "same",
        "Change from n periods earlier.",
        "Works by rows; a gap makes the next change span two periods.",
        _diff,
    )
)
register(
    Function(
        "pct_change",
        (X, Param("n", "int", 1)),
        "same",
        "Percent change from n periods earlier (×100).",
        "Undefined when the earlier value is 0 (left as a gap).",
        _pct_change,
    )
)
register(
    Function(
        "yoy",
        (X,),
        "same",
        "Percent change from the same date a year earlier (52 weeks for weekly).",
        "Matches by date, not row, so gaps don't misalign it. Daily series must be "
        "resampled first.",
        _yoy,
    )
)
register(
    Function(
        "log",
        (X,),
        "same",
        "Natural log.",
        "Needs positive values. Differences of logs ≈ percent changes for small moves.",
        _log,
    )
)
