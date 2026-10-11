import json
from pathlib import Path

from typer.testing import CliRunner

from tempus.cli import app

from ..conftest import TEST_DB

runner = CliRunner()
ROOT = Path(__file__).parents[2]


def test_check_offline_passes_on_the_example_cards(monkeypatch):
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["card", "check", "--offline"])
    assert result.exit_code == 0, result.output
    assert "crude-to-gasoline" in result.output


def test_check_offline_fails_on_a_bad_card(monkeypatch, tmp_path: Path):
    (tmp_path / "cards").mkdir()
    bad = Path(__file__).parent / "bad" / "unknown-name.yaml"
    (tmp_path / "cards" / "unknown-name.yaml").write_text(bad.read_text())
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["card", "check", "--offline"])
    assert result.exit_code == 1
    assert "Did you mean 'gasoline'?" in result.output


def test_new_writes_a_template_that_parses(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["card", "new", "my-card"]).exit_code == 0
    assert runner.invoke(app, ["card", "new", "my-card"]).exit_code == 1  # exists
    from tempus.cards.spec import load_card

    card, errors = load_card(tmp_path / "cards" / "my-card.yaml")
    assert errors == [] and card.question


def test_functions_and_panels_catalogs():
    out = runner.invoke(app, ["card", "functions", "lagcorr"]).output
    assert "lagcorr(x, y, nlags=12)" in out and "leads" in out
    assert "timeseries" in runner.invoke(app, ["card", "panels"]).output


def test_show_json_against_the_test_db(conn, monkeypatch, tmp_path: Path):
    from psycopg.conninfo import make_conninfo

    from tempus.config import get_settings

    from .seed import seed

    seed(conn)
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "lead.yaml").write_text(
        (Path(__file__).parent / "fixtures" / "lead.yaml").read_text()
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", make_conninfo(get_settings().database_url, dbname=TEST_DB))
    result = runner.invoke(app, ["card", "show", "lead", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["payloads"][1]["annotations"]["peak"]["lag"] == 1
