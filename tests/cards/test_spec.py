from pathlib import Path

from tempus.cards.errors import CardError, did_you_mean, has_errors
from tempus.cards.spec import ConceptInput, SeriesInput, load_card, parse_card

VALID = {
    "version": 1,
    "title": "Crude → gasoline",
    "question": "Does crude lead gasoline CPI?",
    "inputs": {
        "crude": "fred:DCOILWTICO",
        "gasoline": {"series": "fred:CUSR0000SETB01", "label": "CPI gasoline"},
        "energy": {"concept": "prices.consumer.energy"},
    },
    "formulas": {"crude_yoy": 'yoy(resample(crude, "M"))'},
    "panels": [{"type": "timeseries", "y": ["crude_yoy"]}],
}


def test_valid_card_parses_with_shorthand_inputs():
    card, errors = parse_card("crude-to-gasoline", VALID)
    assert errors == []
    assert isinstance(card.inputs["crude"], SeriesInput)
    assert (card.inputs["crude"].source, card.inputs["crude"].series_id) == ("fred", "DCOILWTICO")
    assert card.inputs["gasoline"].label == "CPI gasoline"
    assert isinstance(card.inputs["energy"], ConceptInput)


def test_missing_question_and_unknown_key_are_both_reported():
    raw = {k: v for k, v in VALID.items() if k != "question"} | {"colour": "red"}
    card, errors = parse_card("c", raw)
    assert card is None
    paths = {e.path for e in errors}
    assert {"question", "colour"} <= paths


def test_bad_series_reference():
    raw = VALID | {"inputs": {"crude": "DCOILWTICO"}}
    _, errors = parse_card("c", raw)
    assert any("source:series_id" in e.message for e in errors)


def test_name_used_for_input_and_formula():
    raw = VALID | {"formulas": {"crude": "yoy(gasoline)"}}
    _, errors = parse_card("c", raw)
    assert any("both an input and a formula" in e.message for e in errors)


def test_version_must_be_1():
    _, errors = parse_card("c", VALID | {"version": 2})
    assert errors and errors[0].path == "version"


def test_load_card_rejects_non_kebab_filename(tmp_path: Path):
    path = tmp_path / "Crude_Card.yaml"
    path.write_text("version: 1\n")
    card, errors = load_card(path)
    assert card is None and "kebab-case" in errors[0].message


def test_load_card_reports_broken_yaml(tmp_path: Path):
    path = tmp_path / "broken.yaml"
    path.write_text("title: [unclosed\n")
    card, errors = load_card(path)
    assert card is None and errors[0].message.startswith("invalid YAML")


def test_load_card_reports_non_mapping(tmp_path: Path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    _, errors = load_card(path)
    assert errors[0].message == "a card must be a YAML mapping"


def test_error_helpers():
    err = CardError("c", "formulas.x", "unknown name 'gasolin'.", hint="Did you mean 'gasoline'?")
    assert err.format() == (
        "cards/c.yaml · formulas.x\n  unknown name 'gasolin'. Did you mean 'gasoline'?"
    )
    assert has_errors([err]) and not has_errors([CardError("c", "p", "m", level="warning")])
    assert did_you_mean("gasolin", ["gasoline", "crude"]) == "Did you mean 'gasoline'?"
    assert did_you_mean("zzz", ["gasoline"]) is None
