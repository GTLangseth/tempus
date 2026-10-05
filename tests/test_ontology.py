from pathlib import Path

import pytest

from tempus import ontology
from tempus.filing import NONE, place
from tempus.jev import ChoiceAnswer
from tempus.ontology import OntologyError

TREE = """
- id: labor
  name: Labor Market
  description: Jobs and workers.
  children:
    - id: unemployment
      name: Unemployment
      description: Unemployment rates.
    - id: wages
      name: Wages
      description: Pay.
- id: prices
  name: Prices
  description: Inflation.
"""


@pytest.fixture
def roots(tmp_path):
    path = tmp_path / "concepts.yaml"
    path.write_text(TREE)
    return ontology.load(path)


def test_load_builds_dotted_ids(roots):
    assert [c.id for c in ontology.walk(roots)] == [
        "labor",
        "labor.unemployment",
        "labor.wages",
        "prices",
    ]
    assert roots[0].children[0].parent_id == "labor"


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        ("- {id: Labor, name: L, description: d}", "snake_case"),
        ("- {id: a, name: A, description: d}\n- {id: a, name: B, description: d}", "duplicate"),
        ("- {id: a, name: A}", "missing description"),
        ("- id: a\n  name: A\n  description: H.4.1: total", "invalid YAML"),
    ],
)
def test_load_rejects_invalid_trees(tmp_path, yaml_text, message):
    path = tmp_path / "concepts.yaml"
    path.write_text(yaml_text)
    with pytest.raises(OntologyError, match=message):
        ontology.load(path)


def test_repo_ontology_is_valid():
    path = Path(__file__).parents[1] / "ontology" / "concepts.yaml"
    assert len(list(ontology.walk(ontology.load(path)))) > 10


class ScriptedJev:
    """Answers each level's question from a list of probability dicts, in order."""

    def __init__(self, *answers: dict[str, float]):
        self.answers = list(answers)
        self.menus: list[set[str]] = []

    def choice(self, state, instructions, criteria):
        self.menus.append(set(criteria))
        probs = self.answers.pop(0)
        return ChoiceAnswer(max(probs, key=probs.get), probs)


def test_place_descends_on_clear_decisions(roots):
    jev = ScriptedJev({"labor": 0.9, "prices": 0.05}, {"unemployment": 0.8, "wages": 0.1})
    p = place({"title": "Unemployment Rate"}, roots, jev)
    assert (p.concept_id, p.confidence) == ("labor.unemployment", 0.8)
    assert jev.menus[0] == {"labor", "prices", NONE}
    assert jev.menus[1] == {"unemployment", "wages", NONE}


def test_place_stays_at_parent_when_child_choice_is_close(roots):
    # 0.55 is below the 0.65 floor, so the series stays at `labor`.
    jev = ScriptedJev({"labor": 0.9}, {"unemployment": 0.55, "wages": 0.45})
    assert place({}, roots, jev).concept_id == "labor"


def test_place_requires_a_lead_over_the_runner_up(roots):
    jev = ScriptedJev({"labor": 0.66, "prices": 0.5})
    assert place({}, roots, jev).concept_id is None


def test_place_respects_none_of_these(roots):
    jev = ScriptedJev({NONE: 0.9, "labor": 0.05})
    p = place({}, roots, jev)
    assert (p.concept_id, p.confidence) == (None, None)
    assert p.trail[0]["top"][0] == [NONE, 0.9]
