from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from tempus.app import create_app
from tempus.cards.spec import Card
from tempus.config import Settings, get_settings
from tempus.drafting import SHAPES, card_yaml
from tempus.filing import NONE

from .cards.seed import seed
from .conftest import TEST_DB
from .test_drafting import PRICES, TOP, ScriptedJev, _Fred, _snapshot_series, shape_menu

LEADER, FOLLOWER = SHAPES["lead_lag"].roles
AT_PRICES = frozenset({"fred:XLEAD", "fred:YFOLLOW", NONE})
QUESTION = "Does the leader lead the follower?"


@pytest.fixture
def settings(tmp_path: Path):
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    return Settings(database_url=url, cards_path=tmp_path / "cards")


def lead_lag_jev() -> ScriptedJev:
    return ScriptedJev(
        shape_menu(lead_lag=0.8, compare=0.15, change=0.05)
        | {
            (LEADER, TOP): {"prices": 0.9},
            (LEADER, PRICES): {NONE: 0.7},
            (LEADER, AT_PRICES): {"fred:XLEAD": 0.6, "fred:YFOLLOW": 0.3},
            (FOLLOWER, TOP): {"prices": 0.9},
            (FOLLOWER, PRICES): {NONE: 0.7},
            (FOLLOWER, AT_PRICES): {"fred:XLEAD": 0.2, "fred:YFOLLOW": 0.7},
        }
    )


def test_draft_returns_the_card_how_it_was_drafted_and_its_panels(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings, chooser=lead_lag_jev()))
    body = client.post("/api/draft", json={"question": QUESTION}).json()

    assert {k: body[k] for k in ("question", "shape", "picks", "error", "synced", "chooser")} == {
        "question": QUESTION,
        "shape": {
            "name": "lead_lag",
            "p": 0.8,
            "ranked": [["lead_lag", 0.8], ["compare", 0.15], ["change", 0.05]],
        },
        "picks": [
            {
                "role": LEADER,
                "series": "fred:XLEAD",
                "title": "Leader",
                "probability": 0.6,
                "runners_up": [{"series": "fred:YFOLLOW", "title": "Follower", "probability": 0.3}],
            },
            {
                "role": FOLLOWER,
                "series": "fred:YFOLLOW",
                "title": "Follower",
                "probability": 0.7,
                "runners_up": [],
            },
        ],
        "error": None,
        "synced": [],
        "chooser": "jev",
    }
    card = Card.model_validate(body["card"])
    assert (card.title, card.formulas) == (
        "Leader → Follower",
        {
            "leader_yoy": "yoy(leader)",
            "follower_yoy": "yoy(follower)",
            "correlation": "lagcorr(leader_yoy, follower_yoy, nlags=12)",
        },
    )
    assert body["yaml"] == card_yaml(card)
    assert [p["type"] for p in body["payloads"]] == ["timeseries", "lagcorr"]
    assert body["errors"] == []


def swapped(shape: str = "lead_lag", *refs: str) -> dict:
    roles = SHAPES[shape].roles
    return {
        "question": QUESTION,
        "shape": shape,
        "shape_p": 0.8,
        "picks": [
            {"role": role, "series": ref, "probability": 0.5}
            for role, ref in zip(roles, refs, strict=True)
        ],
    }


def test_assemble_rebuilds_the_card_from_the_picks_sent_without_asking_jev(conn, settings):
    seed(conn)
    jev = ScriptedJev({})
    client = TestClient(create_app(settings, chooser=jev))
    request = swapped("lead_lag", "fred:YFOLLOW", "fred:XLEAD")
    request["picks"][0]["runners_up"] = [
        {"series": "fred:XLEAD", "title": "Leader", "probability": 0.4}
    ]
    body = client.post("/api/draft/assemble", json=request).json()

    assert jev.states == []
    assert body["shape"] == {"name": "lead_lag", "p": 0.8, "ranked": [["lead_lag", 0.8]]}
    assert [(p["series"], p["title"], p["runners_up"]) for p in body["picks"]] == [
        ("fred:YFOLLOW", "Follower", [{"series": "fred:XLEAD", "title": "Leader",
                                       "probability": 0.4}]),
        ("fred:XLEAD", "Leader", []),
    ]  # fmt: skip
    card = Card.model_validate(body["card"])
    assert card.title == "Follower → Leader"
    assert {n: i.series for n, i in card.inputs.items()} == {
        "leader": "fred:YFOLLOW",
        "follower": "fred:XLEAD",
    }
    assert card.formulas["correlation"] == "lagcorr(leader_yoy, follower_yoy, nlags=12)"
    assert body["errors"] == [] and body["error"] is None


def test_assemble_says_why_the_picks_cannot_make_a_card(conn, settings):
    seed(conn)
    _snapshot_series(conn, "SNAP", "Percent")
    client = TestClient(create_app(settings, chooser=ScriptedJev({})))
    body = client.post(
        "/api/draft/assemble", json=swapped("spread", "fred:XLEAD", "fred:SNAP")
    ).json()
    assert (body["card"], body["yaml"], body["payloads"], body["errors"]) == (None, None, [], [])
    assert body["error"] == (
        "a spread needs both series in the same units: Leader is in Index, "
        "Snapshot SNAP is in Percent"
    )


def test_assemble_rejects_picks_that_do_not_fill_the_shape(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings, chooser=ScriptedJev({})))
    assert client.post("/api/draft/assemble", json=swapped("level", "fred:XLEAD") | {
        "shape": "lead_lag"}).status_code == 422  # fmt: skip
    assert client.post("/api/draft/assemble", json=swapped("level", "fred:XLEAD") | {
        "shape": "nope"}).status_code == 422  # fmt: skip


def test_keep_writes_the_card_file_and_counts_up_a_taken_id(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings, chooser=lead_lag_jev()))
    draft = client.post("/api/draft", json={"question": QUESTION}).json()

    first = client.post("/api/cards", json={"card": draft["card"]})
    second = client.post("/api/cards", json={"card": draft["card"]})

    assert (first.status_code, first.json()) == (201, {"id": "leader-follower"})
    assert (second.status_code, second.json()) == (201, {"id": "leader-follower-2"})
    assert (settings.cards_path / "leader-follower.yaml").read_text() == draft["yaml"]
    assert client.get("/api/cards/leader-follower-2").json()["errors"] == []


def test_keep_refuses_a_card_that_fails_its_checks(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings, chooser=lead_lag_jev()))
    card = client.post("/api/draft", json={"question": QUESTION}).json()["card"]
    card["formulas"]["correlation"] = "lagcorr(missing, follower_yoy, nlags=12)"

    broken = client.post("/api/cards", json={"card": card})
    malformed = client.post("/api/cards", json={"card": {"title": 3}})

    assert broken.status_code == 400
    assert [(e["path"], e["level"]) for e in broken.json()["errors"]] == [
        ("formulas.correlation", "error")
    ]
    assert malformed.status_code == 400 and malformed.json()["errors"]
    assert not settings.cards_path.exists() or list(settings.cards_path.iterdir()) == []


def test_read_only_app_can_still_keep(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings, read_only=True, chooser=lead_lag_jev()))
    card = client.post("/api/draft", json={"question": QUESTION}).json()["card"]
    assert client.post("/api/cards", json={"card": card}).status_code == 201


def test_guest_app_cannot_draft_or_keep(settings):
    client = TestClient(create_app(settings, guest=True))
    assert client.post("/api/draft", json={"question": QUESTION}).status_code == 403
    assert client.post("/api/draft/assemble", json=swapped("level", "x:y")).status_code == 403
    assert client.post("/api/cards", json={"card": {}}).status_code == 403


def test_an_empty_question_is_422(settings):
    client = TestClient(create_app(settings, chooser=ScriptedJev({})))
    assert client.post("/api/draft", json={"question": "  "}).status_code == 422


SNAP_JEV = shape_menu(level=0.9) | {
    ("the measure", TOP): {"prices": 0.9},
    ("the measure", PRICES): {"energy": 0.8},
    ("the measure", frozenset({"fred:XLEAD", "fred:SNAP", NONE})): {"fred:SNAP": 0.7},
}


@pytest.mark.parametrize("read_only, synced", [(False, ["fred:SNAP"]), (True, [])])
def test_drafting_syncs_a_picked_series_unless_read_only(conn, settings, read_only, synced):
    seed(conn)
    _snapshot_series(conn, "SNAP", "Percent")
    asked: list[str] = []

    def sync_source(name):
        asked.append(name)
        return _Fred()

    app = create_app(
        settings, sync_source=sync_source, read_only=read_only, chooser=ScriptedJev(SNAP_JEV)
    )
    body = TestClient(app).post("/api/draft", json={"question": "Where is snap?"}).json()
    assert body["synced"] == synced and len(asked) == len(synced)
    assert body["card"]["title"] == ("Synced SNAP" if synced else "Snapshot SNAP")
