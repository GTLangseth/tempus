from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database_url: str) -> psycopg.Connection:
    """Autocommit connection; use `conn.transaction()` for atomic blocks."""
    return psycopg.connect(database_url, autocommit=True, row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text())


def status(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            current_database()                                              AS database,
            (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS timescaledb,
            (SELECT count(*) FROM series)                                   AS series,
            (SELECT count(*) FROM observations)                             AS observations,
            (SELECT max(started_at) FROM sync_runs)                         AS last_sync
        """
    ).fetchone()
    assert row is not None
    return row
