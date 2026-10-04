"""Pull series from a Source and reconcile them into the database."""

from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from tempus.sources import Source

Status = Literal["updated", "unchanged", "failed"]


@dataclass(frozen=True)
class SyncResult:
    source: str
    series_id: str
    status: Status
    rows_upserted: int = 0
    rows_deleted: int = 0
    error: str | None = None


def sync_series(
    conn: psycopg.Connection, source: Source, series_id: str, *, force: bool = False
) -> SyncResult:
    """Sync one series.

    Skips the download when the source's `upstream_updated_at` hasn't moved since the last
    sync (unless `force`). Otherwise fetches the full series and reconciles it, so upstream
    revisions and removals are picked up — economic data is revised often.
    """
    run_id = conn.execute(
        "INSERT INTO sync_runs (source, series_id) VALUES (%s, %s) RETURNING id",
        (source.name, series_id),
    ).fetchone()["id"]

    try:
        result = _sync(conn, source, series_id, force=force)
    except Exception as exc:
        result = SyncResult(source.name, series_id, "failed", error=str(exc))

    conn.execute(
        """
        UPDATE sync_runs
        SET finished_at = now(), status = %s, rows_upserted = %s, rows_deleted = %s, error = %s
        WHERE id = %s
        """,
        (result.status, result.rows_upserted, result.rows_deleted, result.error, run_id),
    )
    return result


def _sync(conn: psycopg.Connection, source: Source, series_id: str, *, force: bool) -> SyncResult:
    meta = source.fetch_meta(series_id)
    stored = conn.execute(
        "SELECT upstream_updated_at FROM series WHERE source = %s AND series_id = %s",
        (source.name, series_id),
    ).fetchone()

    if (
        not force
        and stored
        and stored["upstream_updated_at"]
        and meta.upstream_updated_at
        and meta.upstream_updated_at <= stored["upstream_updated_at"]
    ):
        conn.execute(
            "UPDATE series SET last_synced_at = now() WHERE source = %s AND series_id = %s",
            (source.name, series_id),
        )
        return SyncResult(source.name, series_id, "unchanged")

    observations = source.fetch_observations(series_id)

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO series (source, series_id, title, units, frequency, seasonal_adjustment,
                                notes, extra, upstream_updated_at, last_synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (source, series_id) DO UPDATE SET
                title = EXCLUDED.title,
                units = EXCLUDED.units,
                frequency = EXCLUDED.frequency,
                seasonal_adjustment = EXCLUDED.seasonal_adjustment,
                notes = EXCLUDED.notes,
                extra = EXCLUDED.extra,
                upstream_updated_at = EXCLUDED.upstream_updated_at,
                last_synced_at = EXCLUDED.last_synced_at
            """,
            (
                source.name,
                series_id,
                meta.title,
                meta.units,
                meta.frequency,
                meta.seasonal_adjustment,
                meta.notes,
                Jsonb(meta.extra),
                meta.upstream_updated_at,
            ),
        )

        conn.execute(
            "CREATE TEMP TABLE _incoming (ts timestamptz PRIMARY KEY, value double precision)"
            " ON COMMIT DROP"
        )
        with conn.cursor() as cur, cur.copy("COPY _incoming (ts, value) FROM STDIN") as copy:
            for o in observations:
                copy.write_row((o.ts, o.value))

        upserted = conn.execute(
            """
            INSERT INTO observations (source, series_id, ts, value)
            SELECT %s, %s, ts, value FROM _incoming
            ON CONFLICT (source, series_id, ts) DO UPDATE SET value = EXCLUDED.value
            WHERE observations.value IS DISTINCT FROM EXCLUDED.value
            """,
            (source.name, series_id),
        ).rowcount

        deleted = conn.execute(
            """
            DELETE FROM observations o
            WHERE o.source = %s AND o.series_id = %s
              AND NOT EXISTS (SELECT 1 FROM _incoming i WHERE i.ts = o.ts)
            """,
            (source.name, series_id),
        ).rowcount

    return SyncResult(source.name, series_id, "updated", upserted, deleted)
