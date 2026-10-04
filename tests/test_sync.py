"""Integration tests against a real TimescaleDB (a throwaway `tempus_test` database).

Skipped when the database isn't reachable — start it with `mise run db:up`.
"""

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings
from tempus.sources.base import Observation, SeriesMeta
from tempus.sync import sync_series

TEST_DB = "tempus_test"


@pytest.fixture
def conn():
    url = get_settings().database_url
    try:
        admin = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with db.connect(make_conninfo(url, dbname=TEST_DB)) as c:
        db.init_schema(c)
        yield c


class FakeSource:
    name = "fake"

    def __init__(self):
        self.updated_at = datetime(2024, 1, 1, tzinfo=UTC)
        self.points = {1: 1.0, 2: 2.0, 3: 3.0}
        self.observation_fetches = 0

    def fetch_meta(self, series_id):
        return SeriesMeta(series_id, title="Fake", upstream_updated_at=self.updated_at)

    def fetch_observations(self, series_id):
        self.observation_fetches += 1
        return [Observation(datetime(2024, m, 1, tzinfo=UTC), v) for m, v in self.points.items()]


def _values(conn):
    rows = conn.execute("SELECT ts, value FROM observations ORDER BY ts").fetchall()
    return {r["ts"].month: r["value"] for r in rows}


def test_sync_inserts_then_skips_when_unchanged(conn):
    src = FakeSource()
    first = sync_series(conn, src, "X")
    assert (first.status, first.rows_upserted) == ("updated", 3)
    assert _values(conn) == {1: 1.0, 2: 2.0, 3: 3.0}

    second = sync_series(conn, src, "X")
    assert second.status == "unchanged"
    assert src.observation_fetches == 1


def test_sync_applies_revisions_and_removals(conn):
    src = FakeSource()
    sync_series(conn, src, "X")

    src.updated_at = datetime(2024, 2, 1, tzinfo=UTC)
    src.points = {1: 1.0, 2: 2.5, 4: None}  # revise Feb, drop Mar, add Apr (missing value)
    result = sync_series(conn, src, "X")

    assert (result.rows_upserted, result.rows_deleted) == (2, 1)
    assert _values(conn) == {1: 1.0, 2: 2.5, 4: None}


def test_failed_sync_is_recorded_and_rolls_back(conn):
    src = FakeSource()
    src.fetch_observations = lambda _id: (_ for _ in ()).throw(RuntimeError("boom"))

    result = sync_series(conn, src, "X")

    assert (result.status, result.error) == ("failed", "boom")
    assert conn.execute("SELECT status, error FROM sync_runs").fetchone() == {
        "status": "failed",
        "error": "boom",
    }
    assert conn.execute("SELECT count(*) AS n FROM series").fetchone()["n"] == 0
