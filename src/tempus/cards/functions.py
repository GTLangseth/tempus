"""The function catalog: every function a formula may call.

Each entry has a typed signature, a doc line and a caveat (what it does and when it misleads).
Names are borrowed from pandas, Datadog and PromQL (see docs/STACK.md → Inspirations).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

import polars as pl

from tempus.cards.values import (
    FREQ_ORDER,
    FREQS,
    EvalError,
    Series,
    SeriesSet,
    Table,
    Value,
    align,
    resample_hint,
)

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
    return replace(_with(x, pl.col("value").diff(n), f"{x.label}, change"), kind="change")


def _pct_change(x: Series, n: int) -> Series:
    v = pl.col("value")
    out = _with(x, (_safe_ratio(v, v.shift(n)) - 1) * 100, f"{x.label}, % change")
    return replace(out, kind="change")


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
    return replace(x, df=df.sort("ts"), label=f"{x.label}, YoY %", units="%", kind="change")


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


# --- smoothing and normalization --------------------------------------------------------


def _rolling(x: Series, n: int, agg: str) -> Series:
    v = pl.col("value")
    expr = {
        "mean": v.rolling_mean(window_size=n),
        "sum": v.rolling_sum(window_size=n),
        "min": v.rolling_min(window_size=n),
        "max": v.rolling_max(window_size=n),
        "std": v.rolling_std(window_size=n),
    }[agg]
    return _with(x, expr, f"{x.label}, {n}-period rolling {agg}")


def _ewm(x: Series, span: int) -> Series:
    return _with(x, pl.col("value").ewm_mean(span=span), f"{x.label}, EWM span {span}")


def _rebase(x: Series, at: date, to: float) -> Series:
    base = x.df.filter((pl.col("ts") >= at) & pl.col("value").is_not_null()).head(1)
    if base.is_empty():
        raise EvalError(f"rebase: '{x.name}' has no value at or after {at}")
    b = base["value"][0]
    if b == 0:
        raise EvalError(f"rebase: '{x.name}' is 0 at {base['ts'][0]}")
    return _with(x, pl.col("value") / b * to, f"{x.label}, {base['ts'][0]} = {to:g}")


def _zscore(x: Series, window: int | None) -> Series:
    v = pl.col("value")
    if window:
        expr = (v - v.rolling_mean(window_size=window)) / v.rolling_std(window_size=window)
    else:
        expr = (v - v.mean()) / v.std()
    return _with(x, expr, f"{x.label}, z-score")


# --- across a set -----------------------------------------------------------------------


def _set_agg(how: str):
    def impl(s: SeriesSet) -> Series:
        if not s.members:
            raise EvalError(f"{how}: '{s.name}' has no members")
        freqs = sorted({m.freq for m in s.members})
        if len(freqs) > 1:
            raise EvalError(
                f"{how}: members of '{s.name}' have different frequencies ({', '.join(freqs)})",
                hint="Resample the members first.",
            )
        v = pl.col("value")
        expr = {
            "mean": v.mean(),
            "sum": pl.when(v.count() > 0).then(v.sum()),
            "min": v.min(),
            "max": v.max(),
        }[how]
        df = pl.concat([m.df for m in s.members]).group_by("ts").agg(expr.alias("value"))
        first = s.members[0]
        return Series(
            s.name, f"{how} of {s.name}", first.freq, df.sort("ts"), first.units, first.kind
        )

    return impl


# --- relationships ----------------------------------------------------------------------

_LAG_UNIT = {"D": (1, "d"), "W": (1, "w"), "M": (1, "mo"), "Q": (3, "mo"), "Y": (1, "y")}


def _lag1_autocorr(s: Series) -> float | None:
    df = s.df.drop_nulls()
    if df.height < 3:
        return None
    r = df.select(pl.corr(pl.col("value"), pl.col("value").shift(1))).item()
    return None if r is None or r != r else r


def _corr(x: Series, y: Series, window: int) -> Series:
    df = align(x, y, f"corr({x.name}, {y.name})")
    out = df.select(
        "ts", pl.rolling_corr(pl.col("a"), pl.col("b"), window_size=window).alias("value")
    )
    return Series(x.name, f"{window}-period correlation: {x.label} vs {y.label}", x.freq, out)


def _lagcorr(x: Series, y: Series, nlags: int) -> Table:
    if x.freq != y.freq:
        raise EvalError(
            f"lagcorr({x.name}, {y.name}) — frequencies differ ({x.freq} vs {y.freq}).",
            hint=resample_hint(x, y),
        )
    if x.freq == "irregular":
        raise EvalError("lagcorr needs a regular frequency", hint='Resample first, e.g. "M"')
    if nlags < 0:
        raise EvalError("lagcorr: nlags must be 0 or more")
    mult, unit = _LAG_UNIT[x.freq]
    rows = []
    for k in range(nlags + 1):
        shifted = (
            x.df if k == 0 else x.df.with_columns(pl.col("ts").dt.offset_by(f"{k * mult}{unit}"))
        )
        joined = shifted.join(y.df, on="ts", how="inner", suffix="_y").drop_nulls()
        r = None
        if joined.height >= 3:
            r = joined.select(pl.corr("value", "value_y")).item()
            r = None if r is None or r != r else r
        rows.append({"lag": k, "r": r, "n": joined.height})
    warnings = []
    for s in (x, y):
        if s.kind != "level":
            continue  # a change series that trends isn't the spurious-levels case
        ac = _lag1_autocorr(s)
        if ac is not None and ac > 0.95:
            warnings.append(
                f"'{s.name}' looks trending in levels; correlations between trending "
                "series are often spurious. Try yoy() or diff() first."
            )
    df = pl.DataFrame(rows, schema={"lag": pl.Int64, "r": pl.Float64, "n": pl.Int64})
    meta = {"kind": "lagcorr", "x": x.label, "y": y.label, "freq": x.freq, "warnings": warnings}
    return Table(x.name, df, meta)


# --- arithmetic -------------------------------------------------------------------------

_ARITH = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: _safe_ratio(a, b),
}


def binop(op: str, left: Value, right: Value, what: str) -> Value:
    if (
        isinstance(left, Table)
        or isinstance(right, Table)
        or isinstance(left, str)
        or isinstance(right, str)
    ):
        raise EvalError(f"{what} — tables and strings can't be used in arithmetic")
    if isinstance(left, SeriesSet):
        return SeriesSet(left.name, tuple(binop(op, m, right, what) for m in left.members))
    if isinstance(right, SeriesSet):
        return SeriesSet(right.name, tuple(binop(op, left, m, what) for m in right.members))
    f = _ARITH[op]
    if isinstance(left, Series) and isinstance(right, Series):
        df = align(left, right, what)
        out = df.select("ts", f(pl.col("a"), pl.col("b")).alias("value"))
        kind = "change" if left.kind == right.kind == "change" else "level"
        return Series(left.name, what, left.freq, out, kind=kind)
    if isinstance(left, Series):
        return _with(left, f(pl.col("value"), pl.lit(right)), what)
    if isinstance(right, Series):
        return _with(right, f(pl.lit(left), pl.col("value")), what)
    return f(left, right) if op != "/" else (None if right == 0 else left / right)


register(
    Function(
        "rolling",
        (X, Param("n", "int"), Param("agg", "str", "mean", ("mean", "sum", "min", "max", "std"))),
        "same",
        "Rolling statistic over the last n periods.",
        "The first n-1 points are gaps. Smoothing delays turning points.",
        _rolling,
    )
)
register(
    Function(
        "ewm",
        (X, Param("span", "int")),
        "same",
        "Exponentially weighted moving average.",
        "Reacts faster than rolling(), but still lags turning points.",
        _ewm,
    )
)
register(
    Function(
        "rebase",
        (X, Param("at", "date"), Param("to", "number", 100)),
        "same",
        "Index the series so its value at `at` equals `to` (default 100).",
        "Uses the first observation at or after `at`. Rebasing makes series with "
        "different units comparable without dual axes.",
        _rebase,
    )
)
register(
    Function(
        "zscore",
        (X, Param("window", "int", None)),
        "same",
        "Standardize: (x − mean) / std, over the full history or a rolling window.",
        "Full-history z-scores use future data at every point: fine for "
        "comparison, wrong for signals.",
        _zscore,
    )
)
for _how in ("mean", "sum", "min", "max"):
    register(
        Function(
            _how,
            (Param("s", "set"),),
            "series",
            f"{_how} across a set's members.",
            "Dates where some members are missing use the members present, which "
            "can make sums jump.",
            _set_agg(_how),
            broadcast=False,
        )
    )
register(
    Function(
        "corr",
        (X, Param("y", "series"), Param("window", "int")),
        "series",
        "Rolling Pearson correlation between two series.",
        "Trending series correlate spuriously; correlate yoy() or diff() instead.",
        _corr,
        broadcast=False,
    )
)
register(
    Function(
        "lagcorr",
        (X, Param("y", "series"), Param("nlags", "int", 12)),
        "lagcorr",
        "Correlation of x and y at lags 0..nlags. Positive lag k means x leads y by k periods.",
        "Uses only points inside the card window. Trending levels correlate "
        "spuriously; use yoy() or diff() first.",
        _lagcorr,
        broadcast=False,
        windowed=True,
    )
)
