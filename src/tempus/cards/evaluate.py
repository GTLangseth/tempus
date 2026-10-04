"""Fetch the observations a card needs and evaluate its formulas in dependency order.

Transforms run on full history; windowed functions (lagcorr) see only the card window.
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import date

import polars as pl
import psycopg

from tempus.cards.check import formula_order
from tempus.cards.errors import CardError
from tempus.cards.expr import BinOp, Call, Name, Neg, Node, Num, Str, names
from tempus.cards.functions import CATALOG, binop, call
from tempus.cards.resolve import SeriesInfo
from tempus.cards.spec import Card, SeriesInput
from tempus.cards.values import SCHEMA, EvalError, Series, SeriesSet, Table, Value, clip


def fetch(
    conn: psycopg.Connection, infos: Iterable[SeriesInfo]
) -> dict[tuple[str, str], pl.DataFrame]:
    keys = sorted({(i.source, i.series_id) for i in infos})
    if not keys:
        return {}
    rows = conn.execute(
        """
        SELECT o.source, o.series_id, o.ts::date AS ts, o.value
        FROM observations o
        JOIN unnest(%s::text[], %s::text[]) AS k(source, series_id) USING (source, series_id)
        ORDER BY o.ts
        """,
        ([k[0] for k in keys], [k[1] for k in keys]),
    ).fetchall()
    out: dict[tuple[str, str], list] = {k: [] for k in keys}
    for r in rows:
        out[(r["source"], r["series_id"])].append((r["ts"], r["value"]))
    return {k: pl.DataFrame(v, schema=SCHEMA, orient="row") for k, v in out.items()}


def evaluate(
    card_id: str,
    card: Card,
    resolved: dict[str, list[SeriesInfo]],
    data: dict[tuple[str, str], pl.DataFrame],
    window: tuple[date | None, date | None],
) -> tuple[dict[str, Value], list[CardError]]:
    env: dict[str, Value] = {}
    errors: list[CardError] = []
    empty = pl.DataFrame(schema=SCHEMA)
    for name, infos in resolved.items():
        inp = card.inputs[name]
        members = tuple(
            Series(
                name if isinstance(inp, SeriesInput) else f"{i.source}:{i.series_id}",
                (inp.label if isinstance(inp, SeriesInput) else None)
                or i.title
                or f"{i.source}:{i.series_id}",
                i.freq,
                data.get((i.source, i.series_id), empty),
                i.units,
            )
            for i in infos
        )
        env[name] = members[0] if isinstance(inp, SeriesInput) else SeriesSet(name, members)

    failed: set[str] = set()
    for name, node in formula_order(card):
        if names(node) - set(env):
            failed.add(name)
            continue
        text = card.formulas[name]
        try:
            value = _eval(node, env, window, text)
        except EvalError as exc:
            errors.append(CardError(card_id, f"formulas.{name}", f"{text} — {exc}", hint=exc.hint))
            failed.add(name)
            continue
        if isinstance(value, Series | SeriesSet | Table):
            value = replace(value, name=name)
        if isinstance(value, Table):
            for warning in value.meta.get("warnings", []):
                errors.append(CardError(card_id, f"formulas.{name}", warning, level="warning"))
        env[name] = value
    return env, errors


def _eval(node: Node, env: dict[str, Value], window, text: str) -> Value:
    match node:
        case Name(id=name):
            return env[name]
        case Num(value=value) | Str(value=value):
            return value
        case Neg(operand=operand):
            return binop("*", -1, _eval(operand, env, window, text), text)
        case BinOp(op=op, left=left, right=right):
            return binop(op, _eval(left, env, window, text), _eval(right, env, window, text), text)
        case Call(func=func, args=args, kwargs=kwargs):
            fn = CATALOG[func]
            values = [_eval(a, env, window, text) for a in args]
            kw = {k: _eval(v, env, window, text) for k, v in kwargs}
            if fn.windowed:
                values = [_clip(v, window) for v in values]
                kw = {k: _clip(v, window) for k, v in kw.items()}
            return call(fn, values, kw)
    raise EvalError("unsupported expression")


def _clip(value: Value, window) -> Value:
    if isinstance(value, Series):
        return clip(value, *window)
    if isinstance(value, SeriesSet):
        return SeriesSet(value.name, tuple(clip(m, *window) for m in value.members))
    return value
