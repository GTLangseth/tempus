from pathlib import Path

import pytest

from tempus.cards.check import check_card, formula_order
from tempus.cards.spec import load_card, parse_card

BAD = sorted((Path(__file__).parent / "bad").glob("*.yaml"))


def _expect(path: Path) -> tuple[str, str]:
    line = next(ln for ln in path.read_text().splitlines() if ln.startswith("# expect:"))
    where, fragment = line.removeprefix("# expect:").split("|")
    return where.strip(), fragment.strip()


@pytest.mark.parametrize("path", BAD, ids=[p.stem for p in BAD])
def test_bad_cards_report_the_expected_error(path):
    card, errors = load_card(path)
    assert card is not None, errors
    _, errors = check_card(path.stem, card)
    where, fragment = _expect(path)
    assert any(e.path == where and fragment in e.message for e in errors), errors


def test_did_you_mean_hint_on_unknown_name():
    card, _ = load_card(Path(__file__).parent / "bad" / "unknown-name.yaml")
    _, errors = check_card("unknown-name", card)
    assert errors[0].hint == "Did you mean 'gasoline'?"


GOOD = {
    "version": 1,
    "title": "t",
    "question": "q",
    "inputs": {
        "crude": "fred:DCOILWTICO",
        "gas": "fred:CUSR0000SETB01",
        "energy": {"concept": "prices.consumer.energy"},
    },
    "formulas": {
        "passthru": "lagcorr(crude_yoy, gas_yoy, nlags=12)",
        "crude_yoy": 'yoy(resample(crude, "M"))',
        "gas_yoy": "yoy(gas)",
        "energy_yoy": "yoy(energy)",
        "avg": "mean(energy_yoy) - 1",
    },
    "panels": [
        {"type": "timeseries", "y": ["crude_yoy", "gas_yoy", "energy_yoy"]},
        {"type": "lagcorr", "data": "passthru"},
        {"type": "stat", "y": "avg", "change": "yoy"},
    ],
}


def test_good_card_types_and_order():
    card, errors = parse_card("good", GOOD)
    assert errors == []
    types, errors = check_card("good", card)
    assert errors == []
    assert types["energy"] == "set" and types["energy_yoy"] == "set"
    assert types["avg"] == "series" and types["passthru"] == "lagcorr"
    order = [name for name, _ in formula_order(card)]
    assert order.index("crude_yoy") < order.index("passthru")


def test_stat_panel_rejects_a_set_and_multiple_refs():
    raw = GOOD | {
        "panels": [{"type": "stat", "y": ["crude_yoy", "gas_yoy"]}, {"type": "stat", "y": "energy"}]
    }
    card, _ = parse_card("c", raw)
    _, errors = check_card("c", card)
    assert any(e.path == "panels[0]" and "only one" in e.message for e in errors)
    assert any(e.path == "panels[1]" and "'energy' is a set" in e.message for e in errors)
