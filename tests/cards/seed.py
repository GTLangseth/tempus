"""Small synthetic dataset for card integration tests."""

from datetime import UTC, datetime

from tempus.sources.base import Observation, SeriesMeta
from tempus.sync import sync_series


class _Source:
    name = "fred"

    def __init__(self, metas, points):
        self.metas, self.points = metas, points

    def fetch_meta(self, series_id):
        return self.metas[series_id]

    def fetch_observations(self, series_id):
        return self.points[series_id]


def _monthly(values, start_year=2018):
    return [
        Observation(datetime(start_year + i // 12, i % 12 + 1, 1, tzinfo=UTC), v)
        for i, v in enumerate(values)
    ]


def seed(conn):
    base = [100 + (i % 7) * 3 + i * 0.5 for i in range(72)]
    revised = datetime(2024, 1, 1, tzinfo=UTC)
    metas = {
        "XLEAD": SeriesMeta(
            "XLEAD", title="Leader", units="Index", frequency="Monthly", upstream_updated_at=revised
        ),
        "YFOLLOW": SeriesMeta(
            "YFOLLOW",
            title="Follower",
            units="Index",
            frequency="Monthly",
            upstream_updated_at=revised,
        ),
        "WEEKLY": SeriesMeta(
            "WEEKLY",
            title="Weekly thing",
            frequency="Weekly, Ending Monday",
            upstream_updated_at=revised,
        ),
    }
    points = {
        "XLEAD": _monthly(base),
        "YFOLLOW": _monthly([100.0] + base[:-1]),  # follows XLEAD one month later
        "WEEKLY": [
            Observation(datetime(2020, 1, 6, tzinfo=UTC), 1.0),
            Observation(datetime(2021, 1, 4, tzinfo=UTC), 2.0),
        ],
    }
    src = _Source(metas, points)
    for sid in metas:
        sync_series(conn, src, sid)
    conn.execute("""INSERT INTO concepts (id, parent_id, name, description, position) VALUES
        ('prices', NULL, 'Prices', 'p', 0),
        ('prices.energy', 'prices', 'Energy', 'e', 0),
        ('prices.empty', 'prices', 'Empty', 'e', 1)""")
    conn.execute("""INSERT INTO series_concepts (source, series_id, concept_id, method) VALUES
        ('fred', 'XLEAD', 'prices.energy', 'manual'),
        ('fred', 'YFOLLOW', 'prices', 'manual')""")
