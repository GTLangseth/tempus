from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from tempus.app import create_app
from tempus.cards.check import check_card
from tempus.cards.errors import has_errors
from tempus.cards.spec import Card
from tempus.compare import compare_card, correlation, default_mode, modes
from tempus.config import Settings, get_settings
from tempus.drafting import card_yaml
from tempus.sources.base import Observation, SeriesMeta
from tempus.sync import sync_series

from .cards.seed import _Source
from .conftest import TEST_DB

MIXED = {
    "fred:A": {
        "title": "Alpha index of something quite long indeed",
        "units": "Index",
        "frequency": "Monthly",
        "observation_start": "1990-01-01",
    },
    "fred:B": {
        "title": "Beta",
        "units": "Index",
        "frequency": "Weekly, Ending Friday",
        "observation_start": "2005-03-01",
    },
    "fred:C": {
        "title": "Gamma",
        "units": "Dollars",
        "frequency": "Daily",
        "observation_start": "2010-06-15",
    },
}
TRIO = list(MIXED)


def checked(card: Card) -> Card:
    assert not has_errors(check_card("compare", card)[1])
    return card


def test_rebased_card_rebases_every_series_at_the_latest_common_start():
    card = checked(compare_card(TRIO, MIXED, "rebased"))
    assert card.model_dump(mode="json", exclude_none=True, exclude_defaults=True) == {
        "version": 1,
        "title": "Alpha index of something quite long vs Beta vs Gamma",
        "question": "How do Alpha index of something quite long, Beta and Gamma move together?",
        "tags": ["compare"],
        "inputs": {
            "s1": {"series": "fred:A", "label": "Alpha index of something quite long indeed"},
            "s2": {"series": "fred:B", "label": "Beta"},
            "s3": {"series": "fred:C", "label": "Gamma"},
        },
        "formulas": {
            "s1_rebased": 'rebase(s1, "2010-06-15")',
            "s2_rebased": 'rebase(s2, "2010-06-15")',
            "s3_rebased": 'rebase(s3, "2010-06-15")',
        },
        "panels": [
            {
                "type": "timeseries",
                "title": "Rebased to 100",
                "y": ["s1_rebased", "s2_rebased", "s3_rebased"],
            }
        ],
    }


def test_rebase_date_is_never_before_2000():
    meta = {ref: {**row, "observation_start": "1950-01-01"} for ref, row in MIXED.items()}
    card = checked(compare_card(TRIO, meta, "rebased"))
    assert card.formulas["s1_rebased"] == 'rebase(s1, "2000-01-01")'


def test_yoy_card_takes_each_change_at_monthly_or_coarser():
    card = checked(compare_card(TRIO, MIXED, "yoy"))
    assert card.formulas == {
        "s1_yoy": "yoy(s1)",
        "s2_yoy": "yoy(s2)",
        "s3_yoy": 'yoy(resample(s3, "M", "mean"))',
    }
    assert card.panels[0].model_dump(exclude_none=True) == {
        "type": "timeseries",
        "title": "Year-over-year %",
        "y": ["s1_yoy", "s2_yoy", "s3_yoy"],
    }


def test_zscore_card_standardizes_each_series():
    card = checked(compare_card(TRIO, MIXED, "zscore"))
    assert card.formulas == {"s1_z": "zscore(s1)", "s2_z": "zscore(s2)", "s3_z": "zscore(s3)"}
    assert (card.panels[0].title, card.panels[0].y) == (
        "Standard deviations",
        ["s1_z", "s2_z", "s3_z"],
    )


def test_levels_card_plots_the_inputs_with_no_formulas():
    card = checked(compare_card(TRIO, MIXED, "levels"))
    assert card.formulas == {}
    assert (card.panels[0].title, card.panels[0].y) == ("Levels", ["s1", "s2", "s3"])


def test_two_series_question_joins_with_and():
    card = checked(compare_card(["fred:B", "fred:C"], MIXED, "levels"))
    assert (card.title, card.question) == ("Beta vs Gamma", "How do Beta and Gamma move together?")


def test_short_titles_stop_before_a_parenthetical():
    meta = {
        "fred:G": {"title": "Average Price: Gasoline, Unleaded Regular (Cost per Gallon)"},
        "fred:O": {"title": "Crude Oil Prices: West Texas Intermediate (WTI) - Cushing"},
    }
    assert compare_card(list(meta), meta, "zscore").title == (
        "Average Price: Gasoline, Unleaded Regular vs Crude Oil Prices: West Texas Intermediate"
    )


def test_default_mode_and_modes_follow_whether_units_match():
    same = {ref: {**row, "units": "Index"} for ref, row in MIXED.items()}
    assert (default_mode(same), modes(same)) == ("levels", ["levels", "rebased", "yoy", "zscore"])
    assert (default_mode(MIXED), modes(MIXED)) == ("rebased", ["rebased", "yoy", "zscore"])
    assert compare_card(TRIO, MIXED, None).formulas["s1_rebased"] == 'rebase(s1, "2010-06-15")'
    assert compare_card(TRIO, same, None).formulas == {}


# --- against the database ----------------------------------------------------------------

# Year-over-year growth of A from 2019-01 on; C grows by exactly the opposite rate.
GROWTH = [0.02 * ((t * 7) % 5 - 2) + 0.005 * (t % 3) for t in range(36)]


def _levels(base: list[float], sign: int) -> list[float]:
    out = list(base)
    for t, g in enumerate(GROWTH):
        out.append(out[t] * (1 + sign * g))
    return out  # 2018-01 .. 2021-12


A_LEVELS = _levels([100.0 + t for t in range(12)], 1)
C_LEVELS = _levels([50.0 + t for t in range(12)], -1)


def _month(d: date) -> int:
    return (d.year - 2018) * 12 + d.month - 1


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _days(start: date, end: date, step: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(0, (end - start).days + 1, step)]


def _monthly(values: list[float], first: date) -> list[Observation]:
    m0 = _month(first)
    return [
        Observation(datetime(2018 + (m0 + i) // 12, (m0 + i) % 12 + 1, 1, tzinfo=UTC), v)
        for i, v in enumerate(values)
    ]


def seed_trio(conn) -> None:
    """A monthly, B weekly at twice A, C daily from 2018-07 growing opposite to A; D monthly
    from 2020-06, so it shares 7 months of YoY with A; E has no YoY at all."""
    weeks = _days(date(2018, 1, 1), date(2021, 12, 27), 7)
    days = _days(date(2018, 7, 1), date(2021, 12, 31), 1)
    metas = {
        "A": ("Alpha", "Index", "Monthly", _monthly(A_LEVELS, date(2018, 1, 1))),
        "B": (
            "Beta",
            "Index",
            "Weekly, Ending Monday",
            [Observation(_at(d), 2 * A_LEVELS[_month(d)]) for d in weeks],
        ),
        "C": (
            "Gamma",
            "Dollars",
            "Daily",
            [Observation(_at(d), C_LEVELS[_month(d)]) for d in days],
        ),
        "D": (
            "Delta",
            "Index",
            "Monthly",
            _monthly([1.0 + t * t for t in range(19)], date(2020, 6, 1)),
        ),
        "E": ("Epsilon", "Index", "Monthly", _monthly([1.0, 2.0, 3.0], date(2021, 1, 1))),
    }
    src = _Source(
        {
            sid: SeriesMeta(sid, title=title, units=units, frequency=freq)
            for sid, (title, units, freq, _) in metas.items()
        },
        {sid: points for sid, (*_, points) in metas.items()},
    )
    for sid in metas:
        sync_series(conn, src, sid)


def test_correlation_of_yoy_changes_over_the_months_they_share(conn):
    seed_trio(conn)
    assert correlation(conn, ["fred:A", "fred:B", "fred:C"]) == {
        "refs": ["fred:A", "fred:B", "fred:C"],
        "matrix": [[1.0, 1.0, -1.0], [1.0, 1.0, -1.0], [-1.0, -1.0, 1.0]],
        "months": 30,
        "start": "2019-07-01",
        "end": "2021-12-01",
    }


def test_correlation_is_none_with_fewer_than_12_shared_months(conn):
    seed_trio(conn)
    assert correlation(conn, ["fred:A", "fred:D"]) == {
        "refs": ["fred:A", "fred:D"],
        "matrix": [[1.0, None], [None, 1.0]],
        "months": 7,
        "start": "2021-06-01",
        "end": "2021-12-01",
    }
    assert correlation(conn, ["fred:A", "fred:E"]) == {
        "refs": ["fred:A", "fred:E"],
        "matrix": [[1.0, None], [None, 1.0]],
        "months": 0,
        "start": None,
        "end": None,
    }


@pytest.fixture
def settings(tmp_path: Path):
    url = make_conninfo(get_settings().database_url, dbname=TEST_DB)
    return Settings(database_url=url, cards_path=tmp_path / "cards")


def test_compare_route_returns_the_card_its_run_and_the_correlation(conn, settings):
    seed_trio(conn)
    client = TestClient(create_app(settings))
    refs = ["fred:A", "fred:B", "fred:C"]
    response = client.post("/api/compare", json={"series": refs, "mode": None})
    assert response.status_code == 200
    body = response.json()
    assert {k: body[k] for k in ("series", "mode", "modes", "correlation")} == {
        "series": [
            {"series": "fred:A", "title": "Alpha", "units": "Index", "frequency": "Monthly"},
            {
                "series": "fred:B",
                "title": "Beta",
                "units": "Index",
                "frequency": "Weekly, Ending Monday",
            },
            {"series": "fred:C", "title": "Gamma", "units": "Dollars", "frequency": "Daily"},
        ],
        "mode": "rebased",
        "modes": ["rebased", "yoy", "zscore"],
        "correlation": correlation(conn, refs),
    }
    card = Card.model_validate(body["card"])
    assert card.formulas["s3_rebased"] == 'rebase(s3, "2018-07-01")'
    assert body["yaml"] == card_yaml(card)
    assert [p["type"] for p in body["payloads"]] == ["timeseries"]
    assert body["errors"] == []


def test_compare_route_uses_the_mode_sent(conn, settings):
    seed_trio(conn)
    client = TestClient(create_app(settings))
    body = client.post("/api/compare", json={"series": ["fred:A", "fred:C"], "mode": "yoy"}).json()
    assert (body["mode"], body["card"]["formulas"]) == (
        "yoy",
        {"s1_yoy": "yoy(s1)", "s2_yoy": 'yoy(resample(s2, "M", "mean"))'},
    )
    assert body["errors"] == []


@pytest.mark.parametrize(
    "request_body",
    [
        {"series": ["fred:A"]},
        {"series": [f"fred:S{i}" for i in range(7)]},
        {"series": ["fred:A", "fred:A"]},
        {"series": ["fred:A", "fred:NOPE"]},
        {"series": ["fred:A", "not-a-ref"]},
        {"series": ["fred:A", "fred:B"], "mode": "log"},
    ],
)
def test_compare_route_refuses_bad_requests(conn, settings, request_body):
    seed_trio(conn)
    client = TestClient(create_app(settings))
    assert client.post("/api/compare", json=request_body).status_code == 422


def test_guest_and_read_only_apps_may_compare(conn, settings):
    seed_trio(conn)
    for app in (create_app(settings, guest=True), create_app(settings, read_only=True)):
        response = TestClient(app).post("/api/compare", json={"series": ["fred:A", "fred:B"]})
        assert (response.status_code, response.json()["mode"]) == (200, "levels")
