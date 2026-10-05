"""Import a FRED metadata snapshot (data/fred/series-snapshot.json) into `series`.

A snapshot gives the explore view a wide catalog to browse without an API key. It writes
metadata only: no observations, `last_synced_at` stays NULL, and `upstream_updated_at`
stays NULL so the first real `tempus sync` of a series still downloads it. Rows that have
been synced are never touched; the provider's own metadata wins. catalog.toml (the list
`sync` keeps fresh) is not changed.
"""

import json
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from tempus.sources.base import SeriesMeta

# frequency_short → the long form FRED uses, which `freq_from_fred` understands.
FREQUENCY = {"D": "Daily", "W": "Weekly", "M": "Monthly", "Q": "Quarterly", "A": "Annual"}
ADJUSTMENT = {
    "SA": "Seasonally Adjusted",
    "NSA": "Not Seasonally Adjusted",
    "SAAR": "Seasonally Adjusted Annual Rate",
    "SSA": "Smoothed Seasonally Adjusted",
}
EXTRA_KEYS = (
    "frequency_short",
    "units_short",
    "seasonal_adjustment_short",
    "observation_start",
    "observation_end",
    "popularity",
)


class SnapshotError(ValueError):
    pass


def parse_snapshot(path: Path) -> list[SeriesMeta]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("series"), list):
        raise SnapshotError(f"{path}: expected an object with a 'series' list")
    out = []
    for i, s in enumerate(raw["series"]):
        if not s.get("id"):
            raise SnapshotError(f"{path}: series[{i}] has no id")
        short = s.get("frequency_short")
        adj = s.get("seasonal_adjustment_short")
        extra = {k: s[k] for k in EXTRA_KEYS if k in s}
        if s.get("last_updated"):
            extra["snapshot_last_updated"] = s["last_updated"]
        if raw.get("fetched"):
            extra["snapshot_fetched"] = raw["fetched"]
        out.append(
            SeriesMeta(
                series_id=s["id"],
                title=s.get("title"),
                units=s.get("units"),
                frequency=s.get("frequency") or FREQUENCY.get(short or "", short),
                seasonal_adjustment=ADJUSTMENT.get(adj or "", adj),
                extra=extra,
            )
        )
    return out


def import_snapshot(conn: psycopg.Connection, path: Path, source: str = "fred") -> tuple[int, int]:
    """Upsert the snapshot's metadata. Returns (inserted, refreshed); synced rows are skipped."""
    inserted = refreshed = 0
    with conn.transaction():
        for m in parse_snapshot(path):
            row = conn.execute(
                """
                INSERT INTO series (source, series_id, title, units, frequency,
                                    seasonal_adjustment, extra)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, series_id) DO UPDATE SET
                    title = EXCLUDED.title, units = EXCLUDED.units,
                    frequency = EXCLUDED.frequency,
                    seasonal_adjustment = EXCLUDED.seasonal_adjustment, extra = EXCLUDED.extra
                WHERE series.last_synced_at IS NULL
                RETURNING (xmax = 0) AS inserted
                """,
                (
                    source,
                    m.series_id,
                    m.title,
                    m.units,
                    m.frequency,
                    m.seasonal_adjustment,
                    Jsonb(m.extra),
                ),
            ).fetchone()
            if row is None:
                continue  # already synced: the provider's metadata wins
            inserted += row["inserted"]
            refreshed += not row["inserted"]
    return inserted, refreshed
