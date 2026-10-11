from pathlib import Path

import psycopg
from psycopg import sql
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


def ensure_readonly_role(conn: psycopg.Connection, password: str, role: str = "tempus_ro") -> None:
    """Create or update a login role that can only read this database (idempotent).

    Postgres enforces it: `default_transaction_read_only` plus SELECT-only grants, including
    on tables created later. Grants on a hypertable cover its chunks.
    """
    r = sql.Identifier(role)
    with conn.transaction():
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = sql.SQL("ALTER" if exists else "CREATE")
        conn.execute(
            sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {}").format(verb, r, sql.Literal(password))
        )
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(r))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(conn.info.dbname), r
            )
        )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(r))
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(r)
        )
