import json
from datetime import UTC, datetime

import pytest

from tempus.cards.values import freq_from_fred
from tempus.snapshot import SnapshotError, import_snapshot, parse_snapshot
from tempus.sources.base import Observation, SeriesMeta
from tempus.sync import sync_series

SNAPSHOT = {
    "source": "fred",
    "fetched": "2026-10-04",
    "series": [
        {
            "id": "DGS10",
            "title": "10-Year Treasury",
            "frequency": "Daily",
            "frequency_short": "D",
            "units": "Percent",
            "units_short": "%",
            "seasonal_adjustment_short": "NSA",
            "observation_start": "1962-01-02",
            "observation_end": "2026-10-01",
            "popularity": 99,
            "last_updated": "2026-10-02 15:16:37-05",
        },
        {
            "id": "GDP",
            "title": "Gross Domestic Product",
            "frequency_short": "Q",  # no long form: mapped from the short code
            "units": "Billions of Dollars",
            "seasonal_adjustment_short": "SAAR",
            "popularity": 90,
        },
    ],
}


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "snap.json"
    p.write_text(json.dumps(SNAPSHOT))
    return p


@pytest.mark.parametrize(
    "short, freq", [("D", "D"), ("W", "W"), ("M", "M"), ("Q", "Q"), ("A", "Y")]
)
def test_frequency_short_maps_to_text_the_engine_understands(tmp_path, short, freq):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"source": "fred", "series": [{"id": "X", "frequency_short": short}]}))
    (meta,) = parse_snapshot(p)
    assert freq_from_fred(meta.frequency) == freq


def test_parse_keeps_metadata_but_no_upstream_stamp(path):
    dgs10, gdp = parse_snapshot(path)
    assert dgs10.frequency == "Daily" and gdp.frequency == "Quarterly"
    assert gdp.seasonal_adjustment == "Seasonally Adjusted Annual Rate"
    assert dgs10.seasonal_adjustment == "Not Seasonally Adjusted"
    # The snapshot's last_updated must not look like a sync, or `sync` would skip the download.
    assert dgs10.upstream_updated_at is None
    assert dgs10.extra["snapshot_last_updated"] == "2026-10-02 15:16:37-05"
    assert dgs10.extra["popularity"] == 99 and dgs10.extra["units_short"] == "%"


def test_parse_rejects_bad_files(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"series": [{"title": "no id"}]}))
    with pytest.raises(SnapshotError, match="id"):
        parse_snapshot(p)
    p.write_text("[]")
    with pytest.raises(SnapshotError):
        parse_snapshot(p)


def test_import_is_idempotent_and_writes_series_only(conn, path):
    assert import_snapshot(conn, path) == (2, 0)
    assert import_snapshot(conn, path) == (0, 2)  # second run: nothing new, two refreshed
    rows = conn.execute("SELECT * FROM series ORDER BY series_id").fetchall()
    assert [r["series_id"] for r in rows] == ["DGS10", "GDP"]
    assert all(r["last_synced_at"] is None and r["upstream_updated_at"] is None for r in rows)
    assert conn.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 0


class _Fake:
    name = "fred"

    def fetch_meta(self, series_id):
        stamp = datetime(2026, 10, 2, tzinfo=UTC)
        return SeriesMeta(
            series_id, title="Synced title", frequency="Daily", upstream_updated_at=stamp
        )

    def fetch_observations(self, series_id):
        return [Observation(datetime(2026, 1, 2, tzinfo=UTC), 4.1)]


def test_import_never_overwrites_a_synced_series_and_sync_still_downloads(conn, path):
    import_snapshot(conn, path)
    # A snapshot row must not block the first real sync.
    assert sync_series(conn, _Fake(), "DGS10").status == "updated"
    import_snapshot(conn, path)
    row = conn.execute("SELECT title FROM series WHERE series_id = 'DGS10'").fetchone()
    assert row["title"] == "Synced title"
