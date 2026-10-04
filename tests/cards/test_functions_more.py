from datetime import date

import pytest

from tempus.cards.functions import CATALOG, binop, call
from tempus.cards.values import EvalError, Series, SeriesSet, Table, frame


def monthly(name, vals, start=(2020, 1)):
    y, m = start
    pts = []
    for i, v in enumerate(vals):
        k = (m - 1) + i
        pts.append((date(y + k // 12, k % 12 + 1, 1), v))
    return Series(name, name, "M", frame(pts))


def values(s):
    return s.df["value"].to_list()


def fn(name, *args, **kwargs):
    return call(CATALOG[name], list(args), kwargs)


def test_rolling_ewm_zscore_rebase():
    m = monthly("m", [1.0, 2.0, 3.0, 4.0])
    assert values(fn("rolling", m, 2)) == [None, 1.5, 2.5, 3.5]
    assert values(fn("rolling", m, 2, agg="max")) == [None, 2.0, 3.0, 4.0]
    assert values(fn("ewm", m, 2))[0] == 1.0
    z = values(fn("zscore", m))
    assert z[0] == pytest.approx(-1.161895, rel=1e-5) and sum(z) == pytest.approx(0.0)
    assert values(fn("rebase", m, "2020-02-01")) == [50.0, 100.0, 150.0, 200.0]
    with pytest.raises(EvalError, match="no value at or after"):
        fn("rebase", m, "2030-01-01")


def test_set_aggregation_ignores_missing_members_per_date():
    a = monthly("a", [1.0, 3.0])
    b = monthly("b", [3.0, None])
    s = SeriesSet("s", (a, b))
    assert values(fn("mean", s)) == [2.0, 3.0]
    assert values(fn("sum", s)) == [4.0, 3.0]
    with pytest.raises(EvalError, match="different frequencies"):
        fn("mean", SeriesSet("s", (a, Series("d", "d", "D", a.df))))


def test_lagcorr_finds_a_known_lead():
    base = [float(v) for v in [5, 1, 4, 2, 8, 3, 9, 1, 7, 2, 6, 3, 8, 2, 9, 4, 7, 1, 5, 3]]
    x = monthly("x", base)
    y = monthly("y", [0.0, 0.0] + base[:-2])  # y follows x two months later
    table = fn("lagcorr", x, y, nlags=4)
    assert isinstance(table, Table) and table.meta["kind"] == "lagcorr"
    rows = table.df.to_dicts()
    best = max(rows, key=lambda r: r["r"])
    assert best["lag"] == 2 and best["r"] == pytest.approx(1.0)
    assert [r["lag"] for r in rows] == [0, 1, 2, 3, 4]


def test_lagcorr_warns_on_trending_levels():
    trend = monthly("t", [float(i) for i in range(30)])
    table = fn("lagcorr", trend, trend, nlags=1)
    assert any("trending" in w for w in table.meta["warnings"])


def test_lagcorr_with_too_little_data_gives_none_not_nan():
    table = fn("lagcorr", monthly("a", [1.0]), monthly("b", [1.0]), nlags=0)
    assert table.df.to_dicts() == [{"lag": 0, "r": None, "n": 1}]


def test_corr_rolling():
    a = monthly("a", [1.0, 2.0, 3.0, 4.0])
    b = monthly("b", [2.0, 4.0, 6.0, 8.0])
    out = fn("corr", a, b, 3)
    assert values(out)[2] == pytest.approx(1.0)


def test_binop_series_numbers_and_sets():
    a = monthly("a", [2.0, 4.0])
    b = monthly("b", [1.0, 0.0])
    assert values(binop("/", a, b, "a / b")) == [2.0, None]  # divide by zero → gap
    assert values(binop("*", 2, a, "2 * a")) == [4.0, 8.0]
    s = binop("-", SeriesSet("s", (a, b)), 1, "s - 1")
    assert [values(m) for m in s.members] == [[1.0, 3.0], [0.0, -1.0]]
    with pytest.raises(EvalError, match="frequencies differ"):
        binop("+", a, Series("d", "d", "D", a.df), "a + d")
