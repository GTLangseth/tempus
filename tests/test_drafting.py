from datetime import UTC, datetime

import pytest

from tempus.cards.check import check_card
from tempus.cards.run import run_spec
from tempus.cards.spec import load_card
from tempus.drafting import SHAPES, DraftError, JevDrafter, Pick, assemble, card_yaml
from tempus.filing import NONE
from tempus.jev import ChoiceAnswer
from tempus.sources.base import Observation, SeriesMeta

from .cards.seed import seed

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


MORTGAGE = {"title": "30-Year Mortgage Rate", "units": "Percent", "frequency": "Weekly, Ending Thursday",
            "observation_start": "1971-04-02"}  # fmt: skip
TEN_YEAR = {"title": "10-Year Treasury", "units": "Percent", "frequency": "Daily",
            "observation_start": "1962-01-02"}  # fmt: skip


def test_spread_of_a_weekly_and_a_daily_resamples_both_to_one_weekly_anchor():
    META.update({"fred:MORTGAGE30US": MORTGAGE, "fred:DGS10": TEN_YEAR})
    card = build("spread", "fred:MORTGAGE30US", "fred:DGS10")
    assert card.formulas == {"spread": 'resample(a, "W", "mean") - resample(b, "W", "mean")'}


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


# --- the drafter, end to end on seeded data ------------------------------------------------


class ScriptedJev:
    """Answers from a dict keyed by (the role being found, the menu's option set)."""

    def __init__(self, answers: dict[tuple[str | None, frozenset], dict[str, float]]):
        self.answers = answers
        self.states: list[dict] = []

    def choice(self, state, instructions, criteria):
        self.states.append(state)
        default = {NONE: 1.0} if NONE in criteria else {}
        found = self.answers.get((state.get("find"), frozenset(criteria)), default)
        probs = {k: 0.0 for k in criteria} | found
        return ChoiceAnswer(max(probs, key=probs.get), probs)


LEADER, FOLLOWER = SHAPES["lead_lag"].roles
TOP = frozenset({"prices", NONE})
PRICES = frozenset({"energy", "empty", NONE})
ENERGY = frozenset({"fred:XLEAD", "fred:SNAP", NONE})


def shape_menu(**probs: float) -> dict:
    return {(None, frozenset(SHAPES)): probs}


def test_drafter_picks_a_series_per_role_skipping_one_already_taken(conn):
    seed(conn)
    jev = ScriptedJev(
        shape_menu(lead_lag=0.8, compare=0.15)
        | {
            (LEADER, TOP): {"prices": 0.9},
            (LEADER, PRICES): {"energy": 0.8},
            (LEADER, frozenset({"fred:XLEAD", NONE})): {"fred:XLEAD": 0.9},
            (FOLLOWER, TOP): {"prices": 0.9},
            (FOLLOWER, PRICES): {NONE: 0.7},
            (FOLLOWER, frozenset({"fred:XLEAD", "fred:YFOLLOW", NONE})): {
                "fred:XLEAD": 0.6,
                "fred:YFOLLOW": 0.3,
            },
        }
    )
    draft = JevDrafter(conn, jev).draft("Does the leader lead the follower?")

    assert draft.shape.choice == "lead_lag" and draft.shape.probabilities["compare"] == 0.15
    assert draft.picks == [
        Pick(LEADER, "fred:XLEAD", 0.9, []),
        Pick(FOLLOWER, "fred:YFOLLOW", 0.3, []),
    ]
    assert draft.error is None and draft.synced == []
    assert body(draft.card) == {
        "title": "Leader → Follower",
        "inputs": {
            "leader": {"series": "fred:XLEAD", "label": "Leader"},
            "follower": {"series": "fred:YFOLLOW", "label": "Follower"},
        },
        "formulas": {
            "leader_yoy": "yoy(leader)",
            "follower_yoy": "yoy(follower)",
            "correlation": "lagcorr(leader_yoy, follower_yoy, nlags=12)",
        },
        "panels": [
            {"type": "timeseries", "title": "YoY change", "y": ["leader_yoy", "follower_yoy"]},
            {"type": "lagcorr", "data": "correlation"},
        ],
    }
    assert draft.card.notes.startswith("Drafted as lead_lag (p 0.80).\n")
    assert run_spec(conn, "drafted", draft.card).errors == []
    assert {
        "query": "Does the leader lead the follower? (Find only the measure that responds later.)",
        "find": FOLLOWER,
    } in jev.states


def _snapshot_series(conn, series_id: str, units: str, concept: str = "prices.energy"):
    conn.execute(
        """INSERT INTO series (source, series_id, title, units, frequency, extra)
           VALUES ('fred', %s, %s, %s, 'Monthly', '{"observation_start": "2010-01-01"}')""",
        (series_id, f"Snapshot {series_id}", units),
    )
    conn.execute(
        """INSERT INTO series_concepts (source, series_id, concept_id, method)
           VALUES ('fred', %s, %s, 'auto')""",
        (series_id, concept),
    )


class _Fred:
    name = "fred"

    def fetch_meta(self, series_id):
        stamp = datetime(2026, 10, 1, tzinfo=UTC)
        return SeriesMeta(series_id, title=f"Synced {series_id}", units="Percent",
                          frequency="Monthly", upstream_updated_at=stamp)  # fmt: skip

    def fetch_observations(self, series_id):
        return [Observation(datetime(2020, m, 1, tzinfo=UTC), 4.0 + m) for m in range(1, 13)]


def test_drafter_syncs_a_picked_series_that_has_no_observations(conn):
    seed(conn)
    _snapshot_series(conn, "SNAP", "Percent")
    jev = ScriptedJev(
        shape_menu(level=0.9)
        | {
            ("the measure", TOP): {"prices": 0.9},
            ("the measure", PRICES): {"energy": 0.8},
            ("the measure", ENERGY): {"fred:SNAP": 0.7, "fred:XLEAD": 0.2},
        }
    )
    draft = JevDrafter(conn, jev, sync_source=lambda name: _Fred()).draft("Where is snap?")
    assert draft.synced == ["fred:SNAP"]
    assert draft.picks == [Pick("the measure", "fred:SNAP", 0.7, [("fred:XLEAD", 0.2)])]
    assert draft.card.inputs["x"].label == "Synced SNAP"
    assert run_spec(conn, "drafted", draft.card).errors == []


def test_drafter_reports_why_assembly_failed(conn):
    seed(conn)
    _snapshot_series(conn, "SNAP", "Percent")
    first, second = SHAPES["spread"].roles
    jev = ScriptedJev(
        shape_menu(spread=0.9)
        | {(role, TOP): {"prices": 0.9} for role in (first, second)}
        | {(role, PRICES): {"energy": 0.8} for role in (first, second)}
        | {(first, ENERGY): {"fred:XLEAD": 0.8}, (second, ENERGY): {"fred:SNAP": 0.8}}
    )
    draft = JevDrafter(conn, jev).draft("Leader minus snap?")
    assert draft.card is None and draft.synced == []
    assert draft.error == (
        "a spread needs both series in the same units: Leader is in Index, "
        "Snapshot SNAP is in Percent"
    )


def test_drafter_says_when_no_series_fills_a_role(conn):
    seed(conn)
    draft = JevDrafter(conn, ScriptedJev(shape_menu(level=0.9))).draft("bananas")
    assert draft.picks == [] and draft.card is None
    assert draft.error == "no series found for the measure"


def test_card_draft_eval_offline_checks_the_real_eval_file(monkeypatch):
    from pathlib import Path

    from typer.testing import CliRunner

    from tempus.cli import app

    monkeypatch.chdir(Path(__file__).parents[1])
    result = CliRunner().invoke(app, ["card", "draft", "--eval", "--offline"])
    assert result.exit_code == 0, result.output
    assert "eval cases check clean" in result.output


def test_card_draft_needs_a_question_or_eval():
    from typer.testing import CliRunner

    from tempus.cli import app

    result = CliRunner().invoke(app, ["card", "draft"])
    assert result.exit_code == 2 and "give a question, or --eval" in result.output
