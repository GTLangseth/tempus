"""Shared fixtures. `conn` is a throwaway `tempus_test` database; tests using it skip when the
database isn't reachable (start it with `mise run db:up`)."""

import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings

# TEMPUS_TEST_DB lets parallel runs (e.g. agents in separate worktrees) share one Postgres.
TEST_DB = os.environ.get("TEMPUS_TEST_DB", "tempus_test")


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
    """Tests never open a tunnel to the home base, whatever the developer's shell or .env
    says (mise loads .env into the environment)."""
    monkeypatch.setenv("TEMPUS_DB", "dev")
    for name in (
        "TEMPUS_HOMEBASE_HOST",
        "TEMPUS_PROD_DB_PORT",
        "TEMPUS_PROD_RO_PASSWORD",
        "TEMPUS_PROD_PASSWORD",
        "TEMPUS_DB_RO_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


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
    """The dev side of a pull or push (`<TEST_DB>_dev`)."""
    yield from _schema_conn(f"{TEST_DB}_dev")


@pytest.fixture
def prod_conn():
    """The prod side of a pull or push (`<TEST_DB>_prod`)."""
    yield from _schema_conn(f"{TEST_DB}_prod")
