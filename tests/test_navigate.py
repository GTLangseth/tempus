from pathlib import Path

import pytest

from tempus import ontology
from tempus.filing import NONE
from tempus.jev import ChoiceAnswer
from tempus.navigate import Catalog, LexicalChooser, navigate

TREE = """
- id: labor
  name: Labor Market
  description: Jobs, workers and pay.
  children:
    - id: unemployment
      name: Unemployment
      description: Unemployment rates and counts of unemployed people.
    - id: wages
      name: Wages
      description: Hourly earnings and compensation.
- id: prices
  name: Prices & Inflation
  description: Consumer prices and commodity prices.
  children:
    - id: commodities
      name: Commodity Prices
      description: Spot prices of raw commodities.
      children:
        - id: energy
          name: Energy Commodities
          description: Crude oil (WTI, Brent) and natural gas (Henry Hub).
        - id: metals
          name: Metals
          description: Gold, copper and aluminum.
"""

SERIES = {
    "labor.unemployment": [
        {"source": "fred", "series_id": "UNRATE", "title": "Unemployment Rate", "popularity": 95},
        {
            "source": "fred",
            "series_id": "TXUR",
            "title": "Unemployment Rate in Texas",
            "popularity": 40,
        },
    ],
    "labor.wages": [
        {
            "source": "fred",
            "series_id": "AHETPI",
            "title": "Average Hourly Earnings",
            "popularity": 70,
        },
    ],
    "prices.commodities.energy": [
        {
            "source": "fred",
            "series_id": "DCOILWTICO",
            "title": "Crude Oil Prices: West Texas Intermediate (WTI)",
            "popularity": 90,
        },
        {
            "source": "fred",
            "series_id": "DHHNGSP",
            "title": "Henry Hub Natural Gas Spot Price",
            "popularity": 80,
        },
    ],
    "prices.commodities.metals": [],
}


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "concepts.yaml"
    path.write_text(TREE)
    return Catalog(ontology.load(path), SERIES)


class ScriptedJev:
    """Answers each question from a dict keyed by the menu's option set, recording calls."""

    def __init__(self, answers: dict[frozenset, dict[str, float]]):
        self.answers = answers
        self.asked: list[frozenset] = []

    def choice(self, state, instructions, criteria):
        menu = frozenset(criteria)
        self.asked.append(menu)
        probs = {k: 0.0 for k in criteria} | self.answers.get(menu, {NONE: 1.0})
        return ChoiceAnswer(max(probs, key=probs.get), probs)


TOP = frozenset({"labor", "prices", NONE})
PRICES = frozenset({"commodities", NONE})
COMMODITIES = frozenset({"energy", "metals", NONE})
LABOR = frozenset({"unemployment", "wages", NONE})
ENERGY_SERIES = frozenset({"fred:DCOILWTICO", "fred:DHHNGSP", NONE})


def test_beam_follows_clear_choices_to_a_leaf_and_ranks_its_series(catalog):
    jev = ScriptedJev(
        {
            TOP: {"prices": 0.9, "labor": 0.05},
            PRICES: {"commodities": 0.95},
            COMMODITIES: {"energy": 0.85, "metals": 0.1},
            ENERGY_SERIES: {"fred:DCOILWTICO": 0.8, "fred:DHHNGSP": 0.15},
        }
    )
    result = navigate("crude oil", catalog, jev)
    best = result.paths[0]
    assert best.concepts == ["prices", "prices.commodities", "prices.commodities.energy"]
    assert [s["choice"] for s in best.steps] == best.concepts
    assert best.steps[0]["p"] == 0.9 and best.stopped == "leaf"
    # Single-child transitions don't count as decisions: (0.9 * 0.85) ** (1/2)
    assert best.score == pytest.approx((0.9 * 0.85) ** 0.5)
    assert result.series[0]["series_id"] == "DCOILWTICO"
    assert result.series[0]["p"] == 0.8
    assert result.series[0]["concept_id"] == "prices.commodities.energy"


class RecordingJev(ScriptedJev):
    """ScriptedJev that also records the state of every call."""

    def __init__(self, answers):
        super().__init__(answers)
        self.states: list[dict] = []

    def choice(self, state, instructions, criteria):
        self.states.append(state)
        return super().choice(state, instructions, criteria)


CRUDE = {
    TOP: {"prices": 0.9},
    PRICES: {"commodities": 0.95},
    COMMODITIES: {"energy": 0.85},
    ENERGY_SERIES: {"fred:DCOILWTICO": 0.8},
}


def test_a_role_rides_in_every_state_of_the_walk_and_the_series_ranking(catalog):
    jev = RecordingJev(CRUDE)
    result = navigate(
        "does crude lead gasoline?", catalog, jev, role="the measure that moves first"
    )
    assert result.series[0]["series_id"] == "DCOILWTICO"
    assert (
        jev.states
        == [{"query": "does crude lead gasoline?", "find": "the measure that moves first"}] * 4
    )


def test_without_a_role_the_state_is_only_the_query(catalog):
    jev = RecordingJev(CRUDE)
    navigate("crude oil", catalog, jev)
    assert jev.states == [{"query": "crude oil"}] * 4


def test_beam_keeps_two_routes_when_the_choice_is_split(catalog):
    jev = ScriptedJev(
        {
            TOP: {"labor": 0.5, "prices": 0.45},
            LABOR: {"wages": 0.9},
            PRICES: {"commodities": 0.9},
            COMMODITIES: {"metals": 0.9},
        }
    )
    result = navigate("pay", catalog, jev)
    reached = [p.concepts[-1] for p in result.paths]
    assert reached[:2] == ["labor.wages", "prices.commodities.metals"]
    assert result.ambiguous


def test_none_of_these_stops_a_route_at_its_parent(catalog):
    jev = ScriptedJev({TOP: {"labor": 0.9}, LABOR: {NONE: 0.7, "wages": 0.2}})
    result = navigate("workers", catalog, jev)
    assert result.paths[0].concepts == ["labor"]
    assert result.paths[0].stopped == "none"
    # Series come from the whole subtree of where the route stopped.
    assert {s["series_id"] for s in result.series} <= {"UNRATE", "TXUR", "AHETPI"}


def test_nothing_fits_returns_no_paths(catalog):
    result = navigate("bananas", catalog, ScriptedJev({TOP: {NONE: 0.95}}))
    assert result.paths == [] and result.series == []


def test_beam_width_caps_routes(catalog):
    jev = ScriptedJev(
        {
            TOP: {"labor": 0.5, "prices": 0.5},
            LABOR: {"wages": 0.5, "unemployment": 0.5},
            PRICES: {"commodities": 1.0},
            COMMODITIES: {"metals": 0.5, "energy": 0.5},
        }
    )
    assert len(navigate("x", catalog, jev, beam=2).paths) == 2


@pytest.mark.parametrize(
    "query, concept, series_id",
    [
        ("crude oil price", "prices.commodities.energy", "DCOILWTICO"),
        ("unemployment rate", "labor.unemployment", "UNRATE"),
        ("natural gas", "prices.commodities.energy", "DHHNGSP"),
        ("hourly earnings", "labor.wages", "AHETPI"),
    ],
)
def test_lexical_fallback_navigates_without_a_key(catalog, query, concept, series_id):
    result = navigate(query, catalog, LexicalChooser(catalog))
    assert result.paths[0].concepts[-1] == concept
    assert result.series[0]["series_id"] == series_id


def test_lexical_fallback_says_none_for_nonsense(catalog):
    assert navigate("zebra quasar", catalog, LexicalChooser(catalog)).paths == []


def test_repo_tree_lexical_smoke():
    """The real tree and draft placements route common phrases sensibly with no key."""
    import json

    root = Path(__file__).parents[1]
    roots = ontology.load(root / "ontology" / "concepts.yaml")
    drafts = ontology.load_drafts(root / "ontology" / "placements.draft.yaml")
    snapshot = json.loads((root / "data" / "fred" / "series-snapshot.json").read_text())
    series: dict[str, list[dict]] = {}
    for s in snapshot["series"]:
        concept = drafts[("fred", s["id"])].concept
        series.setdefault(concept, []).append(
            {"source": "fred", "series_id": s["id"], "title": s["title"], "popularity": 0}
        )
    cat = Catalog(roots, series)
    chooser = LexicalChooser(cat)
    for query, expected in [
        ("mortgage rates", "rates.mortgage"),
        ("vix volatility", "markets.volatility"),
        ("jobless claims", "labor.claims"),
        ("house prices case-shiller", "housing.home_prices"),
        ("10 year treasury yield", "rates.treasury"),
    ]:
        reached = [p.concepts[-1] for p in navigate(query, cat, chooser).paths]
        assert any(r.startswith(expected) for r in reached), (query, reached)
