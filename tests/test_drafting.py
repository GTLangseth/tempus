import pytest

from tempus.cards.check import check_card
from tempus.cards.spec import load_card
from tempus.drafting import SHAPES, DraftError, Pick, assemble, card_yaml
from tempus.jev import ChoiceAnswer

WTI = {"title": "Crude Oil WTI", "units": "Dollars per Barrel", "frequency": "Daily",
       "observation_start": "1986-01-02"}  # fmt: skip
BRENT = {"title": "Crude Oil Brent", "units": "Dollars per Barrel", "frequency": "Daily",
         "observation_start": "1987-05-20"}  # fmt: skip
GAS_CPI = {"title": "CPI Gasoline", "units": "Index 1982-1984=100", "frequency": "Monthly",
           "observation_start": "1967-01-01"}  # fmt: skip
UNRATE = {"title": "Unemployment Rate", "units": "Percent", "frequency": "Monthly",
          "observation_start": "1948-01-01"}  # fmt: skip
GOLD = {"title": "Gold Price", "units": "Index", "frequency": "Monthly",
        "observation_start": "2005-01-01"}  # fmt: skip
META = {
    "fred:DCOILWTICO": WTI,
    "fred:DCOILBRENTEU": BRENT,
    "fred:CUSR0000SETB01": GAS_CPI,
    "fred:UNRATE": UNRATE,
    "fred:GOLD": GOLD,
}


class FixedChooser:
    """Answers every choice with `pick` at probability 0.7, recording what it was asked."""

    def __init__(self, pick: str | None = None):
        self.pick = pick
        self.asked: list[dict[str, str]] = []

    def choice(self, state, instructions, criteria):
        self.asked.append(criteria)
        probs = {k: (0.7 if k == self.pick else 0.3 / (len(criteria) - 1)) for k in criteria}
        return ChoiceAnswer(self.pick, probs)


def picks(shape: str, *refs: str) -> list[Pick]:
    roles = SHAPES[shape].roles
    return [
        Pick(role, ref, 0.9 - 0.1 * i)
        for i, (role, ref) in enumerate(zip(roles, refs, strict=True))
    ]


def build(shape: str, *refs: str, chooser=None):
    card = assemble(
        SHAPES[shape], 0.8, "the question?", picks(shape, *refs), META, chooser or FixedChooser()
    )
    assert check_card("drafted", card)[1] == []
    return card


def body(card) -> dict:
    dumped = card.model_dump(exclude_none=True)
    return {k: dumped[k] for k in ("title", "inputs", "formulas", "panels")}


def test_level_shows_the_latest_value_and_the_history():
    assert body(build("level", "fred:UNRATE")) == {
        "title": "Unemployment Rate",
        "inputs": {"x": {"series": "fred:UNRATE", "label": "Unemployment Rate"}},
        "formulas": {},
        "panels": [
            {"type": "stat", "y": "x"},
            {"type": "timeseries", "title": "History", "y": ["x"]},
        ],
    }


def test_change_of_a_level_is_its_yoy_with_daily_series_resampled_first():
    assert body(build("change", "fred:DCOILWTICO")) == {
        "title": "Crude Oil WTI, change over a year",
        "inputs": {"x": {"series": "fred:DCOILWTICO", "label": "Crude Oil WTI"}},
        "formulas": {"x_yoy": 'yoy(resample(x, "M", "mean"))'},
        "panels": [
            {"type": "stat", "y": "x", "change": "yoy"},
            {"type": "timeseries", "title": "YoY change", "y": ["x_yoy"]},
        ],
    }


def test_change_of_a_percent_is_its_difference_from_a_year_earlier():
    assert body(build("change", "fred:UNRATE")) == {
        "title": "Unemployment Rate, change over a year",
        "inputs": {"x": {"series": "fred:UNRATE", "label": "Unemployment Rate"}},
        "formulas": {"x_change": "diff(x, 12)"},
        "panels": [
            {"type": "stat", "y": "x"},
            {"type": "timeseries", "title": "Change from a year earlier", "y": ["x_change"]},
        ],
    }


def test_compare_in_the_same_units_plots_both_as_they_are():
    assert body(build("compare", "fred:DCOILWTICO", "fred:DCOILBRENTEU")) == {
        "title": "Crude Oil WTI vs Crude Oil Brent",
        "inputs": {
            "a": {"series": "fred:DCOILWTICO", "label": "Crude Oil WTI"},
            "b": {"series": "fred:DCOILBRENTEU", "label": "Crude Oil Brent"},
        },
        "formulas": {},
        "panels": [{"type": "timeseries", "y": ["a", "b"]}],
    }


def test_compare_in_different_units_rebases_both_where_both_have_data():
    assert body(build("compare", "fred:GOLD", "fred:DCOILWTICO")) == {
        "title": "Gold Price vs Crude Oil WTI",
        "inputs": {
            "a": {"series": "fred:GOLD", "label": "Gold Price"},
            "b": {"series": "fred:DCOILWTICO", "label": "Crude Oil WTI"},
        },
        "formulas": {
            "a_rebased": 'rebase(a, "2005-01-01")',
            "b_rebased": 'rebase(b, "2005-01-01")',
        },
        "panels": [
            {"type": "timeseries", "title": "Rebased to 100", "y": ["a_rebased", "b_rebased"]}
        ],
    }


def test_spread_subtracts_on_a_shared_frequency():
    assert body(build("spread", "fred:DCOILBRENTEU", "fred:DCOILWTICO")) == {
        "title": "Crude Oil Brent minus Crude Oil WTI",
        "inputs": {
            "a": {"series": "fred:DCOILBRENTEU", "label": "Crude Oil Brent"},
            "b": {"series": "fred:DCOILWTICO", "label": "Crude Oil WTI"},
        },
        "formulas": {"spread": "a - b"},
        "panels": [
            {"type": "stat", "y": "spread"},
            {"type": "timeseries", "title": "Spread", "y": ["spread"]},
        ],
    }


def test_spread_of_different_units_fails_with_a_reason():
    with pytest.raises(DraftError, match="Dollars per Barrel.*Index 1982-1984=100"):
        build("spread", "fred:DCOILWTICO", "fred:CUSR0000SETB01")


def test_ratio_divides_with_the_finer_series_resampled():
    assert body(build("ratio", "fred:DCOILWTICO", "fred:CUSR0000SETB01")) == {
        "title": "Crude Oil WTI / CPI Gasoline",
        "inputs": {
            "num": {"series": "fred:DCOILWTICO", "label": "Crude Oil WTI"},
            "den": {"series": "fred:CUSR0000SETB01", "label": "CPI Gasoline"},
        },
        "formulas": {"ratio": 'resample(num, "M", "mean") / den'},
        "panels": [{"type": "timeseries", "title": "Ratio", "y": ["ratio"]}],
    }


def test_lead_lag_mirrors_crude_to_gasoline():
    assert body(build("lead_lag", "fred:DCOILWTICO", "fred:CUSR0000SETB01")) == {
        "title": "Crude Oil WTI → CPI Gasoline",
        "inputs": {
            "leader": {"series": "fred:DCOILWTICO", "label": "Crude Oil WTI"},
            "follower": {"series": "fred:CUSR0000SETB01", "label": "CPI Gasoline"},
        },
        "formulas": {
            "leader_yoy": 'yoy(resample(leader, "M", "mean"))',
            "follower_yoy": "yoy(follower)",
            "correlation": "lagcorr(leader_yoy, follower_yoy, nlags=12)",
        },
        "panels": [
            {"type": "timeseries", "title": "YoY change", "y": ["leader_yoy", "follower_yoy"]},
            {"type": "lagcorr", "data": "correlation"},
        ],
    }


def test_threshold_against_zero_plots_the_series_itself():
    chooser = FixedChooser("zero")
    card = build("threshold", "fred:UNRATE", chooser=chooser)
    assert chooser.asked == [{"zero": "zero", "mean": "its long-run mean"}]
    assert body(card) == {
        "title": "Unemployment Rate against zero",
        "inputs": {"x": {"series": "fred:UNRATE", "label": "Unemployment Rate"}},
        "formulas": {},
        "panels": [
            {"type": "stat", "y": "x"},
            {"type": "timeseries", "title": "Against zero", "y": ["x"]},
        ],
    }


def test_threshold_against_the_mean_plots_the_zscore():
    assert body(build("threshold", "fred:UNRATE", chooser=FixedChooser("mean"))) == {
        "title": "Unemployment Rate against its long-run mean",
        "inputs": {"x": {"series": "fred:UNRATE", "label": "Unemployment Rate"}},
        "formulas": {"x_z": "zscore(x)"},
        "panels": [
            {"type": "stat", "y": "x_z"},
            {"type": "timeseries", "title": "Standard deviations from the mean", "y": ["x_z"]},
        ],
    }


def test_every_drafted_card_carries_the_question_tag_and_how_it_was_drafted():
    card = build("lead_lag", "fred:DCOILWTICO", "fred:CUSR0000SETB01")
    assert (card.version, card.question, card.tags) == (1, "the question?", ["drafted"])
    assert card.notes == (
        "Drafted as lead_lag (p 0.80).\n"
        "the measure that moves first: fred:DCOILWTICO (p 0.90)\n"
        "the measure that responds later: fred:CUSR0000SETB01 (p 0.80)\n"
    )


def test_card_yaml_reads_like_a_card_file():
    card = build("lead_lag", "fred:DCOILWTICO", "fred:CUSR0000SETB01")
    assert (
        card_yaml(card)
        == """\
version: 1
title: Crude Oil WTI → CPI Gasoline
question: the question?
notes: |
  Drafted as lead_lag (p 0.80).
  the measure that moves first: fred:DCOILWTICO (p 0.90)
  the measure that responds later: fred:CUSR0000SETB01 (p 0.80)
tags: [drafted]

inputs:
  leader: {series: 'fred:DCOILWTICO', label: Crude Oil WTI}
  follower: {series: 'fred:CUSR0000SETB01', label: CPI Gasoline}

formulas:
  leader_yoy: yoy(resample(leader, "M", "mean"))
  follower_yoy: yoy(follower)
  correlation: lagcorr(leader_yoy, follower_yoy, nlags=12)

panels:
  - type: timeseries
    title: YoY change
    y: [leader_yoy, follower_yoy]
  - type: lagcorr
    data: correlation
"""
    )


@pytest.mark.parametrize(
    "shape, refs",
    [
        ("level", ["fred:UNRATE"]),
        ("change", ["fred:DCOILWTICO"]),
        ("compare", ["fred:GOLD", "fred:DCOILWTICO"]),
        ("spread", ["fred:DCOILBRENTEU", "fred:DCOILWTICO"]),
        ("ratio", ["fred:DCOILWTICO", "fred:CUSR0000SETB01"]),
        ("lead_lag", ["fred:DCOILWTICO", "fred:CUSR0000SETB01"]),
        ("threshold", ["fred:UNRATE"]),
    ],
)
def test_card_yaml_loads_back_as_the_same_card(tmp_path, shape, refs):
    card = build(shape, *refs, chooser=FixedChooser("mean"))
    path = tmp_path / "drafted.yaml"
    path.write_text(card_yaml(card))
    assert load_card(path) == (card, [])
