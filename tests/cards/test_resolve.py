from tempus.cards.resolve import SeriesInfo, resolve
from tempus.cards.spec import parse_card

from .seed import seed


def card(inputs, concepts=()):
    raw = {
        "version": 1,
        "title": "t",
        "question": "q",
        "inputs": inputs,
        "concepts": list(concepts),
        "panels": [{"type": "timeseries", "y": [next(iter(inputs))]}],
    }
    c, errors = parse_card("c", raw)
    assert errors == []
    return c


def test_series_and_concepts_resolve_with_frequency(conn):
    seed(conn)
    resolved, errors = resolve(
        conn,
        "c",
        card(
            {"x": "fred:XLEAD", "w": "fred:WEEKLY", "p": {"concept": "prices"}},
            concepts=["prices.energy"],
        ),
    )
    assert errors == []
    assert resolved["x"] == [SeriesInfo("fred", "XLEAD", "Leader", "Index", "M")]
    assert resolved["w"][0].freq == "W"
    assert {i.series_id for i in resolved["p"]} == {"XLEAD", "YFOLLOW"}  # includes descendants


def test_missing_series_hints(conn):
    seed(conn)
    _, errors = resolve(conn, "c", card({"x": "fred:XLEAF", "z": "fred:NOPE123"}))
    by_path = {e.path: e for e in errors}
    assert by_path["inputs.x"].hint == "Did you mean 'fred:XLEAD'?"
    assert "tempus catalog add fred NOPE123 --sync" in by_path["inputs.z"].hint


def test_empty_and_unknown_concepts(conn):
    seed(conn)
    _, errors = resolve(
        conn,
        "c",
        card(
            {"e": {"concept": "prices.empty"}, "u": {"concept": "prices.enrgy"}}, concepts=["nope"]
        ),
    )
    messages = {e.path: e.message for e in errors}
    assert messages["inputs.e"] == "concept 'prices.empty' has no series filed under it"
    assert "unknown concept 'prices.enrgy'" in messages["inputs.u"]
    assert "unknown concept 'nope'" in messages["concepts[0]"]
