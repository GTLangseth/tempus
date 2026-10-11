from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from tempus.app import create_app
from tempus.config import Settings, get_settings
from tempus.explore import catalog_from_db, explore_payload, series_page
from tempus.navigate import LexicalChooser, navigate
from tempus.sources.base import Observation, SeriesMeta

from .cards.seed import seed
from .conftest import TEST_DB


def _snapshot_series(conn, series_id="DGS10", title="10-Year Treasury Yield", concept="prices"):
    conn.execute(
        """INSERT INTO series (source, series_id, title, units, frequency, extra)
           VALUES ('fred', %s, %s, 'Percent', 'Daily', '{"popularity": 99}')""",
        (series_id, title),
    )
    conn.execute(
        """INSERT INTO series_concepts (source, series_id, concept_id, method, trail)
           VALUES ('fred', %s, %s, 'draft', '[{"draft": true, "low": null}]')""",
        (series_id, concept),
    )


def test_explore_payload_counts_and_facets(conn):
    seed(conn)
    _snapshot_series(conn)
    body = explore_payload(conn)
    concepts = {c["id"]: c for c in body["concepts"]}
    assert concepts["prices"]["series"] == 2  # YFOLLOW (pin) + DGS10 (draft)
    assert concepts["prices"]["total"] == 3  # plus XLEAD under prices.energy
    assert concepts["prices.energy"]["parent_id"] == "prices"
    series = {s["id"]: s for s in body["series"]}
    assert series["DGS10"] == {
        "source": "fred",
        "id": "DGS10",
        "title": "10-Year Treasury Yield",
        "freq": "D",
        "concept": "prices",
        "method": "draft",
        "popularity": 99,
        "synced": False,
    }
    assert series["XLEAD"]["synced"] and series["XLEAD"]["freq"] == "M"
    assert series["WEEKLY"]["concept"] is None  # in the database, not placed yet
    assert body["methods"] == {"manual": 2, "draft": 1}


def test_catalog_from_db_feeds_navigate(conn):
    seed(conn)
    _snapshot_series(conn)
    catalog = catalog_from_db(conn)
    assert [c.id for c in catalog.roots] == ["prices"]
    result = navigate("treasury yield", catalog, LexicalChooser(catalog))
    assert result.series[0]["series_id"] == "DGS10"


def test_series_page_for_a_synced_series(conn):
    seed(conn)
    page = series_page(conn, "fred", "XLEAD")
    assert page["synced"] and page["series"]["observations"] == 72
    assert [p["type"] for p in page["payloads"]] == ["stat", "timeseries"]
    assert page["payloads"][0]["stat"]["date"] == "2023-12-01"
    assert page["placement"]["method"] == "manual"
    assert [c["name"] for c in page["placement"]["path"]] == ["Prices", "Energy"]
    assert not [e for e in page["errors"] if e["level"] == "error"]


def test_series_page_for_an_unsynced_series_without_a_key(conn):
    seed(conn)
    _snapshot_series(conn)
    page = series_page(conn, "fred", "DGS10")
    assert not page["synced"] and page["payloads"] == []
    assert page["hint"] == "tempus catalog add fred DGS10 --sync"
    assert page["placement"]["method"] == "draft"
    assert page["neighbors"][0]["id"] == "YFOLLOW"
    assert page["links"]["source"] == "https://fred.stlouisfed.org/series/DGS10"


class _Fake:
    name = "fred"

    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, 0

    def fetch_meta(self, series_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("FRED is down")
        stamp = datetime(2026, 10, 2, tzinfo=UTC)
        return SeriesMeta(series_id, title="10Y", units="Percent", frequency="Daily",
                          upstream_updated_at=stamp)  # fmt: skip

    def fetch_observations(self, series_id):
        return [Observation(datetime(2026, 1, d, tzinfo=UTC), 4.0 + d / 100) for d in (2, 5, 6)]


def test_series_page_syncs_on_first_open_when_a_source_is_available(conn):
    seed(conn)
    _snapshot_series(conn)
    fake = _Fake()
    page = series_page(conn, "fred", "DGS10", sync_source=lambda name: fake)
    assert page["sync"]["status"] == "updated" and page["synced"]
    assert page["payloads"][1]["series"][0]["points"][-1] == ["2026-01-06", 4.06]
    series_page(conn, "fred", "DGS10", sync_source=lambda name: fake)
    assert fake.calls == 1  # second open reads the database


def test_series_page_reports_a_failed_sync(conn):
    seed(conn)
    _snapshot_series(conn)
    page = series_page(conn, "fred", "DGS10", sync_source=lambda name: _Fake(fail=True))
    assert page["sync"] == {"status": "failed", "error": "FRED is down"}
    assert not page["synced"] and page["hint"]


def test_series_page_unknown(conn):
    assert series_page(conn, "fred", "NOPE") is None


@pytest.fixture
def client(tmp_path: Path):
    (tmp_path / "cards").mkdir()
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    return TestClient(create_app(Settings(database_url=url, cards_path=tmp_path / "cards")))


def test_api_routes(conn, client):
    seed(conn)
    _snapshot_series(conn)
    assert len(client.get("/api/explore").json()["series"]) == 4
    nav = client.get("/api/navigate", params={"q": "treasury yield"}).json()
    assert nav["chooser"] == "lexical fallback (no TYPESAFE_API_KEY)"
    assert nav["series"][0]["series_id"] == "DGS10"
    assert nav["paths"][0]["names"] == ["Prices"]
    assert client.get("/api/navigate", params={"q": " "}).status_code == 422
    assert client.get("/api/series/fred/XLEAD").json()["synced"]
    assert client.get("/api/series/fred/NOPE").status_code == 404


class _FakeJev:
    def __init__(self, api_key, fail=False):
        self.fail = fail

    def choice(self, state, instructions, criteria):
        from tempus.jev import ChoiceAnswer, JevError

        if self.fail:
            raise JevError("Jev request failed (500)")
        probs = {k: 1 / len(criteria) for k in criteria}
        return ChoiceAnswer(next(iter(criteria)), probs)


@pytest.mark.parametrize("fail, label", [(False, "jev"), (True, "lexical fallback (Jev failed")])
def test_navigate_uses_jev_when_a_key_is_set(conn, tmp_path, monkeypatch, fail, label):
    import tempus.app

    seed(conn)
    monkeypatch.setattr(tempus.app, "JevClient", lambda key: _FakeJev(key, fail))
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    settings = Settings(database_url=url, cards_path=tmp_path, typesafe_api_key="k")
    nav = TestClient(create_app(settings)).get("/api/navigate", params={"q": "x"}).json()
    assert nav["chooser"].startswith(label)
