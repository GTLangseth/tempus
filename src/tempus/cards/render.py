"""Panel payloads: the renderer-neutral JSON that every surface draws (the seam)."""

from datetime import date

import polars as pl

from tempus.cards.errors import CardError
from tempus.cards.panels import PANELS, panel_refs
from tempus.cards.spec import Card
from tempus.cards.values import Series, SeriesSet, Table, Value, clip

PAYLOAD_VERSION = 1


def _points(s: Series) -> list[list]:
    return [
        [d.isoformat(), v]
        for d, v in zip(s.df["ts"].to_list(), s.df["value"].to_list(), strict=True)
    ]


def _series_json(s: Series, window) -> dict:
    c = clip(s, *window)
    return {
        "name": s.name,
        "label": s.label,
        "units": s.units,
        "freq": s.freq,
        "points": _points(c),
    }


def _flatten(name: str, value: Value) -> list[Series]:
    if isinstance(value, SeriesSet):
        return [Series(f"{name}.{m.name}", m.label, m.freq, m.df, m.units) for m in value.members]
    return [value] if isinstance(value, Series) else []


def render(
    card_id: str, card: Card, env: dict[str, Value], window: tuple[date | None, date | None]
) -> tuple[list[dict], list[CardError]]:
    payloads, errors = [], []
    for i, panel in enumerate(card.panels):
        ptype = PANELS[panel.type]
        refs = panel_refs(panel, ptype)
        if any(r not in env for r in refs):
            continue  # its formula failed; that error is already reported
        p = {
            "payload_version": PAYLOAD_VERSION,
            "card": card_id,
            "panel": i,
            "type": panel.type,
            "title": panel.title,
            "encoding": {"x": "ts", "y": "value", "color": "series"},
            "series": [],
            "table": None,
            "stat": None,
            "annotations": {},
            "warnings": [],
        }
        value = env[refs[0]]
        if panel.type == "timeseries":
            p["series"] = [_series_json(s, window) for r in refs for s in _flatten(r, env[r])]
            empty = all(not s["points"] for s in p["series"])
        elif panel.type == "lagcorr":
            empty = _lagcorr(p, value)
        elif panel.type == "stat":
            empty = _stat(p, value, panel.change, window)
        else:
            empty = _table(p, value, window)
        if empty:
            msg = "no data in the window"
            p["warnings"].append(msg)
            errors.append(CardError(card_id, f"panels[{i}]", msg, level="warning"))
        payloads.append(p)
    return payloads, errors


def _lagcorr(p: dict, t: Table) -> bool:
    rows = t.df.to_dicts()
    p["encoding"] = {"x": "lag", "y": "r"}
    p["table"] = {"columns": ["lag", "r", "n"], "rows": rows}
    scored = [r for r in rows if r["r"] is not None]
    if not scored:
        return True
    peak = max(scored, key=lambda r: abs(r["r"]))
    plural = "" if peak["lag"] == 1 else "s"
    lead = f"{t.meta['x']} leads {t.meta['y']} by {peak['lag']} period{plural}"
    p["annotations"] = {"peak": {"lag": peak["lag"], "r": peak["r"]}, "lead": lead}
    p["title"] = p["title"] or f"{t.meta['x']} vs {t.meta['y']}"
    return False


def _stat(p: dict, s: Series, change: str | None, window) -> bool:
    c = clip(s, *window).df.drop_nulls()
    p["series"] = [
        {
            "name": s.name,
            "label": s.label,
            "units": s.units,
            "freq": s.freq,
            "points": _points(Series(s.name, s.label, s.freq, c.tail(24))),
        }
    ]
    if c.is_empty():
        return True
    last_ts, last = c["ts"][-1], c["value"][-1]
    delta = None
    if change == "period" and c.height > 1:
        delta = last - c["value"][-2]
    elif change == "yoy":
        prior = s.df.filter(pl.col("ts") == last_ts.replace(year=last_ts.year - 1))["value"]
        if len(prior) and prior[0]:
            delta = (last / prior[0] - 1) * 100
    p["stat"] = {
        "value": last,
        "date": last_ts.isoformat(),
        "change": delta,
        "change_kind": change,
        "units": s.units,
        "label": s.label,
    }
    p["title"] = p["title"] or s.label
    return False


def _table(p: dict, value: Value, window) -> bool:
    if isinstance(value, Table):
        p["table"] = {"columns": value.df.columns, "rows": value.df.to_dicts()}
        return value.df.is_empty()
    c = clip(value, *window)
    p["table"] = {
        "columns": ["ts", "value"],
        "rows": [{"ts": t, "value": v} for t, v in _points(c)],
    }
    return c.df.is_empty()
