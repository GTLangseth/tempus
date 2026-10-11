from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
import typer
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from tempus import cli
from tempus.cli import app
from tempus.config import Settings, get_settings

from .conftest import TEST_DB
from .test_transfer import LATER, add_series

runner = CliRunner()
ROOT = Path(__file__).parents[1]


def test_where_on_dev(conn):
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    result = runner.invoke(app, ["db", "where"], env={"DATABASE_URL": url})
    assert result.exit_code == 0, result.output
    assert result.output.startswith("dev") and "postgres@" in result.output
    assert TEST_DB in result.output and "0 series" in result.output


def test_prod_mode_without_a_password_never_falls_back_to_dev():
    result = runner.invoke(
        app, ["db", "where"], env={"TEMPUS_DB": "prod", "TEMPUS_PROD_RO_PASSWORD": ""}
    )
    assert result.exit_code == 1
    assert "TEMPUS_PROD_RO_PASSWORD" in result.output
    assert "series" not in result.output  # no db where line from the dev database


# --- pull and push -------------------------------------------------------------------


@pytest.fixture
def linked(monkeypatch, dev_conn, prod_conn):
    """`_prod` yielding the two test databases instead of opening a tunnel."""
    monkeypatch.chdir(ROOT)  # the real ontology, catalog and cards

    @contextmanager
    def fake(owner):
        yield Settings(), dev_conn, prod_conn

    monkeypatch.setattr(cli, "_prod", fake)
    return dev_conn, prod_conn


def test_pull_cli(linked):
    dev, prod = linked
    add_series(prod, "A")
    result = runner.invoke(app, ["db", "pull"])
    assert result.exit_code == 0, result.output
    assert "Pulled 1 series, 3 observations, 0 Jev placements" in result.output
    assert dev.execute("SELECT count(*) AS n FROM concepts").fetchone()["n"] > 100  # prepare ran


def test_pull_cli_shows_unpushed_work(linked):
    dev, prod = linked
    add_series(prod, "A")
    add_series(dev, "A", upstream=LATER)
    result = runner.invoke(app, ["db", "pull"])
    assert result.exit_code == 1
    assert "fred:A" in result.output and "--discard-local" in result.output


def test_pull_cli_reports_series_prod_lacks(linked):
    _, prod = linked
    add_series(prod, "A")
    result = runner.invoke(app, ["db", "pull", "--series", "fred:A,fred:NOPE"])
    assert result.exit_code == 0, result.output
    assert "Not on prod (dev's copy kept): fred:NOPE" in result.output


def test_push_is_a_dry_run_by_default(linked):
    dev, prod = linked
    add_series(dev, "A")
    result = runner.invoke(app, ["db", "push"])
    assert result.exit_code == 0, result.output
    assert "fred:A" in result.output and "Dry run" in result.output
    assert prod.execute("SELECT count(*) AS n FROM series").fetchone()["n"] == 0


def test_push_yes_applies_the_plan(linked):
    dev, prod = linked
    add_series(dev, "A")
    result = runner.invoke(app, ["db", "push", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Pushed 1 series (3 observations), 0 Jev placements" in result.output
    assert prod.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 3


def test_push_with_nothing_to_send(linked):
    result = runner.invoke(app, ["db", "push", "--yes"])
    assert result.exit_code == 0 and "Nothing to push" in result.output


def test_pull_refuses_when_dev_is_not_local():
    url = "postgres://postgres:password@db.example.com/tempus"
    result = runner.invoke(app, ["db", "pull"], env={"DATABASE_URL": url})
    assert result.exit_code == 1 and "isn't local" in result.output


def test_pull_names_a_missing_password():
    result = runner.invoke(app, ["db", "pull"], env={"TEMPUS_PROD_RO_PASSWORD": ""})
    assert result.exit_code == 1 and "TEMPUS_PROD_RO_PASSWORD" in result.output


def test_push_names_a_missing_password():
    result = runner.invoke(app, ["db", "push"], env={"TEMPUS_PROD_PASSWORD": ""})
    assert result.exit_code == 1 and "TEMPUS_PROD_PASSWORD" in result.output


def test_pull_reports_a_refused_connection(monkeypatch):
    class DeadTunnel:  # a tunnel whose far end refuses: port 1 is never open
        local_port = 1

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(cli.remote, "Tunnel", DeadTunnel)
    result = runner.invoke(app, ["db", "pull"], env={"TEMPUS_PROD_RO_PASSWORD": "x"})
    assert result.exit_code == 1
    assert "Can't connect to prod" in result.output and "TEMPUS_PROD_RO_PASSWORD" in result.output


@pytest.mark.parametrize(
    "text, keys",
    [
        (None, None),
        ("fred:UNRATE", [("fred", "UNRATE")]),
        ("fred:A, fred:B", [("fred", "A"), ("fred", "B")]),
    ],
)
def test_parse_keys(text, keys):
    assert cli._parse_keys(text) == keys


def test_parse_keys_needs_a_source():
    with pytest.raises(typer.BadParameter, match="source:series_id"):
        cli._parse_keys("UNRATE")


# --- prod mode is read-only ----------------------------------------------------------

WRITERS = [
    ["db", "init"],
    ["db", "readonly-role"],
    ["db", "pull"],
    ["db", "push", "--yes"],
    ["catalog", "add", "fred", "UNRATE"],
    ["catalog", "remove", "fred", "UNRATE"],
    ["catalog", "import-snapshot", "data/fred/series-snapshot.json"],
    ["sync"],
    ["series", "delete", "UNRATE"],
    ["ontology", "load"],
    ["ontology", "file"],
    ["card", "load"],
]


@pytest.mark.parametrize("argv", WRITERS, ids=" ".join)
def test_writers_refuse_in_prod_mode_before_any_tunnel(argv, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli.remote, "Tunnel", lambda *a, **k: pytest.fail("opened a tunnel"))
    result = runner.invoke(app, argv, env={"TEMPUS_DB": "prod", "TEMPUS_PROD_RO_PASSWORD": "x"})
    assert result.exit_code == 1, result.output
    assert "read-only" in result.output


def test_main_turns_a_read_only_error_into_one_line(monkeypatch, capsys):
    def boom():
        raise psycopg.errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only txn")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1 and "TEMPUS_DB=prod is read-only" in capsys.readouterr().out


def test_main_turns_a_remote_error_into_one_line(monkeypatch, capsys):
    def boom():
        raise cli.remote.RemoteError("can't reach homebase over SSH")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1 and "can't reach homebase" in capsys.readouterr().out
