from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from tempus.cli import app
from tempus.config import get_settings

from .conftest import TEST_DB

runner = CliRunner()


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
