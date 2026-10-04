"""Shared fixtures. `conn` is a throwaway `tempus_test` database; tests using it skip when the
database isn't reachable (start it with `mise run db:up`)."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings

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
