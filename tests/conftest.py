"""Shared fixtures. `conn` is a throwaway `tempus_test` database; tests using it skip when the
database isn't reachable (start it with `mise run db:up`)."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings

TEST_DB = "tempus_test"


@pytest.fixture(autouse=True)
def no_api_keys(monkeypatch):
    """Tests never call paid or rate-limited APIs, whatever the developer's .env holds.

    Environment variables beat .env in pydantic-settings, so blanking them here wins. A test
    that needs a key passes one to Settings(...) explicitly and fakes the client.
    """
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("FRED_API_KEY", "")


@pytest.fixture(autouse=True)
def dev_database(monkeypatch):
    """Tests never open a tunnel to the home base, whatever the developer's shell says."""
    monkeypatch.setenv("TEMPUS_DB", "dev")


def fresh_database(name: str) -> str:
    """Drop and recreate `name` next to the dev database; returns its URL."""
    url = get_settings().database_url
    try:
        admin = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")
    return make_conninfo(url, dbname=name)


def _schema_conn(name: str):
    with db.connect(fresh_database(name)) as c:
        db.init_schema(c)
        yield c


@pytest.fixture
def conn():
    yield from _schema_conn(TEST_DB)


@pytest.fixture
def dev_conn():
    """The dev side of a pull or push (`tempus_test_dev`)."""
    yield from _schema_conn("tempus_test_dev")


@pytest.fixture
def prod_conn():
    """The prod side of a pull or push (`tempus_test_prod`)."""
    yield from _schema_conn("tempus_test_prod")
