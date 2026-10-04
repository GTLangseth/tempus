from datetime import date
from pathlib import Path

import pytest

from tempus.cards.repository import FileCardRepository
from tempus.cards.run import PAYLOAD_VERSION, run_card

from .seed import seed

CARD = (Path(__file__).parent / "fixtures" / "lead.yaml").read_text()


@pytest.fixture
def repo(tmp_path: Path):
    (tmp_path / "lead.yaml").write_text(CARD)
    return FileCardRepository(tmp_path)


def test_run_card_end_to_end(conn, repo):
    seed(conn)
    result = run_card(conn, repo, "lead")
    assert [e for e in result.errors if e.level == "error"] == []
    ts, lag, stat, table = result.payloads
    assert ts["payload_version"] == PAYLOAD_VERSION and ts["type"] == "timeseries"
    assert [s["name"] for s in ts["series"]] == ["x_yoy", "y_yoy"]
    assert ts["series"][1]["label"] == "The follower, YoY %"
    assert ts["series"][0]["points"][0][0] >= "2019-01-01"  # clipped to the window
    assert lag["annotations"]["peak"]["lag"] == 1
    assert lag["annotations"]["lead"] == "Leader, YoY % leads The follower, YoY % by 1 period"
    assert stat["stat"]["date"] == "2023-12-01" and stat["stat"]["change_kind"] == "period"
    assert table["table"]["columns"] == ["lag", "r", "n"]


def test_window_override_and_empty_window_warns(conn, repo):
    seed(conn)
    result = run_card(conn, repo, "lead", start=date(2030, 1, 1))
    assert any(e.level == "warning" and "no data in the window" in e.message for e in result.errors)


def test_evaluation_errors_are_collected(conn, tmp_path: Path):
    seed(conn)
    (tmp_path / "mix.yaml").write_text(
        CARD.replace("y_yoy: yoy(y)", "y_yoy: x + w").replace(
            "y: {series: fred:YFOLLOW, label: The follower}", "y: fred:YFOLLOW\n  w: fred:WEEKLY"
        )
    )
    result = run_card(conn, FileCardRepository(tmp_path), "mix")
    err = next(e for e in result.errors if e.path == "formulas.y_yoy")
    assert "frequencies differ (M vs W)" in err.message
    assert err.hint == 'Resample first: resample(w, "M")'


def test_unknown_card_and_static_errors_stop_early(conn, repo):
    seed(conn)
    assert run_card(conn, repo, "nope").errors[0].message == "no such card"
    result = run_card(conn, repo, "lead").to_dict()
    assert result["card"]["title"] == "Leader → follower"
