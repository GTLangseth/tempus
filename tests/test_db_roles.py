from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings

from .conftest import TEST_DB

# Roles are cluster-wide: name it after the test database so parallel runs can't collide.
ROLE = f"{TEST_DB}_ro"


@pytest.fixture
def ro_url(conn):
    db.ensure_readonly_role(conn, "pw1", role=ROLE)
    db.ensure_readonly_role(conn, "pw2", role=ROLE)  # idempotent; the password updates
    yield make_conninfo(get_settings().database_url, dbname=TEST_DB, user=ROLE, password="pw2")
    conn.execute(f"DROP OWNED BY {ROLE}")
    conn.execute(f"DROP ROLE {ROLE}")


def test_readonly_role_reads_every_table_including_hypertable_chunks(conn, ro_url):
    conn.execute("INSERT INTO series (source, series_id) VALUES ('fred', 'A')")
    for year in (2001, 2011, 2021):  # three 5-year chunks
        conn.execute(
            "INSERT INTO observations VALUES ('fred', 'A', %s, 1.0)",
            (datetime(year, 1, 1, tzinfo=UTC),),
        )
    with db.connect(ro_url) as ro:
        assert ro.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 3
        for table in ("series", "series_concepts", "concepts", "cards", "sync_runs"):
            ro.execute(f"SELECT * FROM {table} LIMIT 1")


def test_readonly_role_cannot_write(conn, ro_url):
    with db.connect(ro_url) as ro, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        ro.execute("INSERT INTO series (source, series_id) VALUES ('fred', 'B')")


def test_readonly_role_covers_tables_created_later(conn, ro_url):
    conn.execute("CREATE TABLE later (x int)")
    with db.connect(ro_url) as ro:
        ro.execute("SELECT * FROM later")
