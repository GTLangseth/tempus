from datetime import date

import pytest

from tempus.cards.functions import CATALOG, ArgError, call, match_args, signature
from tempus.cards.values import EvalError, Series, SeriesSet, align, frame, freq_from_fred


def series(name, freq, points, label=None):
    return Series(name, label or name, freq, frame(points))


def values(s):
    return s.df["value"].to_list()


def monthly(name, vals, start_year=2020):
    pts = [(date(start_year + i // 12, i % 12 + 1, 1), v) for i, v in enumerate(vals)]
    return series(name, "M", pts)


def fn(name, *args, **kwargs):
    return call(CATALOG[name], list(args), kwargs)


def test_freq_from_fred():
    assert freq_from_fred("Daily") == "D"
    assert freq_from_fred("Weekly, Ending Monday") == "W"
    assert freq_from_fred("Monthly") == "M"
    assert freq_from_fred("Quarterly") == "Q"
    assert freq_from_fred("Annual") == "Y"
    assert freq_from_fred(None) == "irregular"


def test_resample_daily_to_monthly_mean_and_last():
    d = series(
        "d",
        "D",
        [
            (date(2020, 1, 2), 1.0),
            (date(2020, 1, 3), None),
            (date(2020, 1, 31), 3.0),
            (date(2020, 2, 3), 5.0),
        ],
    )
    m = fn("resample", d, "M")
    assert m.freq == "M"
    assert m.df["ts"].to_list() == [date(2020, 1, 1), date(2020, 2, 1)]
    assert values(m) == [2.0, 5.0]
    assert values(fn("resample", d, "M", agg="last")) == [3.0, 5.0]


def test_resample_sum_of_all_null_period_is_null_not_zero():
    d = series("d", "D", [(date(2020, 1, 2), None), (date(2020, 2, 3), 2.0)])
    assert values(fn("resample", d, "M", agg="sum")) == [None, 2.0]


def test_resample_refuses_to_make_finer():
    with pytest.raises(EvalError, match="coarser"):
        fn("resample", monthly("m", [1.0]), "D")


def test_shift_diff_pct_change_fill_log():
    m = monthly("m", [1.0, 2.0, None, 4.0])
    assert values(fn("shift", m, 1)) == [None, 1.0, 2.0, None]
    assert values(fn("diff", m)) == [None, 1.0, None, None]
    assert values(fn("pct_change", monthly("m", [2.0, 3.0, 0.0, 1.0]))) == [
        None,
        50.0,
        -100.0,
        None,
    ]
    assert values(fn("fill", m, "ffill")) == [1.0, 2.0, 2.0, 4.0]
    assert values(fn("fill", m, "linear")) == [1.0, 2.0, 3.0, 4.0]
    with pytest.raises(EvalError, match="positive"):
        fn("log", monthly("m", [1.0, 0.0]))


def test_yoy_monthly_matches_by_date_even_with_a_gap():
    vals = [100.0] * 12 + [110.0] * 12
    m = monthly("m", vals)
    m = Series("m", "m", "M", m.df.filter(m.df["ts"] != date(2021, 3, 1)))  # drop one month
    out = fn("yoy", m)
    by_date = dict(zip(out.df["ts"].to_list(), values(out), strict=True))
    assert by_date[date(2021, 1, 1)] == pytest.approx(10.0)
    assert by_date[date(2021, 4, 1)] == pytest.approx(10.0)  # not shifted by the gap
    assert date(2021, 3, 1) not in by_date


def test_yoy_weekly_uses_52_weeks():
    mondays = [date(2020, 1, 6), date(2021, 1, 4)]  # 52 weeks apart, both Mondays
    w = series("w", "W", [(mondays[0], 2.0), (mondays[1], 3.0)])
    assert values(fn("yoy", w))[1] == pytest.approx(50.0)


def test_yoy_on_daily_asks_for_resample():
    d = series("crude", "D", [(date(2020, 1, 2), 1.0)])
    with pytest.raises(EvalError) as info:
        fn("yoy", d)
    assert info.value.hint == 'Resample first: resample(crude, "M")'


def test_functions_broadcast_over_sets():
    s = SeriesSet("set", (monthly("a", [1.0, 2.0]), monthly("b", [2.0, 4.0])))
    out = fn("diff", s)
    assert isinstance(out, SeriesSet)
    assert [values(m) for m in out.members] == [[None, 1.0], [None, 2.0]]


def test_align_rejects_mismatched_frequencies_with_hint():
    d = series("crude", "D", [(date(2020, 1, 2), 1.0)])
    with pytest.raises(EvalError) as info:
        align(d, monthly("gas", [1.0]), "crude / gas")
    assert "frequencies differ (D vs M)" in str(info.value)
    assert info.value.hint == 'Resample first: resample(crude, "M")'


def test_match_args_and_signature():
    f = CATALOG["resample"]
    assert match_args(f, ["x", "M"], {}) == {"x": "x", "freq": "M", "agg": "mean"}
    with pytest.raises(ArgError, match="unknown argument 'how'"):
        match_args(f, ["x", "M"], {"how": "sum"})
    with pytest.raises(ArgError, match="missing argument 'freq'"):
        match_args(f, ["x"], {})
    with pytest.raises(ArgError, match="too many"):
        match_args(f, ["x", "M", "mean", 1], {})
    assert signature(f) == 'resample(x, freq, agg="mean")'
