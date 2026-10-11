from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from tempus.app import card_event, create_app
from tempus.cards.repository import FileCardRepository
from tempus.config import Settings, get_settings

from .cards.seed import seed
from .conftest import TEST_DB

LEAD = Path(__file__).parent / "cards" / "fixtures" / "lead.yaml"


def _lead_card() -> str:
    return LEAD.read_text()


@pytest.fixture
def settings(tmp_path: Path):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "lead.yaml").write_text(_lead_card())
    (tmp_path / "cards" / "broken.yaml").write_text("title: [oops\n")
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    return Settings(database_url=url, cards_path=tmp_path / "cards")


def test_cards_list_needs_no_db(settings):
    client = TestClient(create_app(settings))
    cards = {c["id"]: c for c in client.get("/api/cards").json()}
    assert cards["lead"]["title"] == "Leader → follower" and cards["lead"]["errors"] == 0
    assert cards["broken"]["errors"] == 1


def test_card_payloads_and_errors(conn, settings):
    seed(conn)
    client = TestClient(create_app(settings))
    body = client.get("/api/cards/lead").json()
    assert body["payloads"][0]["type"] == "timeseries"
    assert client.get("/api/cards/lead?start=2030-01-01").json()["errors"]
    assert client.get("/api/cards/nope").status_code == 404
    broken = client.get("/api/cards/broken").json()
    assert broken["errors"][0]["message"].startswith("invalid YAML")


def test_concepts_tree_with_counts_and_cards(conn, settings):
    seed(conn)
    tree = TestClient(create_app(settings)).get("/api/concepts").json()
    prices = tree["tree"][0]
    assert prices["id"] == "prices" and prices["series"] == 1
    assert [c["id"] for c in prices["children"]] == ["prices.energy", "prices.empty"]
    assert "lead" in tree["unfiled_cards"]


def test_card_event(settings, tmp_path: Path):
    repo = FileCardRepository(settings.cards_path)
    assert card_event(repo, settings.cards_path / "lead.yaml") == {
        "event": "card-changed",
        "card": "lead",
    }
    invalid = card_event(repo, settings.cards_path / "broken.yaml")
    assert invalid["event"] == "card-invalid" and invalid["errors"]
    assert card_event(repo, settings.cards_path / "gone.yaml") == {
        "event": "card-removed",
        "card": "gone",
    }
    assert card_event(repo, settings.cards_path / "notes.txt") is None


@pytest.mark.parametrize("name", [".#lead.yaml", "lead.tmp.yaml", "Lead Copy.yaml", "~lead.yaml"])
def test_card_event_ignores_editor_temp_files(settings, name):
    # Editors write these next to the real file; they aren't cards and shouldn't flash errors.
    path = settings.cards_path / name
    path.write_text("title: [half-saved\n")
    assert card_event(FileCardRepository(settings.cards_path), path) is None


def test_static_index_is_served(settings):
    assert TestClient(create_app(settings)).get("/").status_code == 200
