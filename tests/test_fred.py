from datetime import UTC, datetime, timedelta, timezone

import httpx
import respx

from tempus.sources.fred import BASE_URL, FredSource


@respx.mock
def test_fetch_meta_parses_last_updated_offset():
    respx.get(f"{BASE_URL}/series").respond(
        json={
            "seriess": [
                {
                    "id": "UNRATE",
                    "title": "Unemployment Rate",
                    "units": "Percent",
                    "frequency": "Monthly",
                    "last_updated": "2024-08-02 07:44:02-05",
                    "popularity": 94,
                }
            ]
        }
    )
    meta = FredSource("key").fetch_meta("UNRATE")
    assert meta.title == "Unemployment Rate"
    assert meta.upstream_updated_at == datetime(
        2024, 8, 2, 7, 44, 2, tzinfo=timezone(timedelta(hours=-5))
    )
    assert meta.extra == {"popularity": 94}


@respx.mock
def test_fetch_observations_handles_missing_values_and_pagination(monkeypatch):
    monkeypatch.setattr("tempus.sources.fred.PAGE_SIZE", 2)
    route = respx.get(f"{BASE_URL}/series/observations")
    route.side_effect = [
        httpx.Response(200, json={"count": 3, "observations": [
            {"date": "2024-01-01", "value": "3.7"},
            {"date": "2024-02-01", "value": "."},
        ]}),
        httpx.Response(200, json={"count": 3, "observations": [
            {"date": "2024-03-01", "value": "3.9"},
        ]}),
    ]  # fmt: skip
    obs = FredSource("key").fetch_observations("UNRATE")
    assert [o.value for o in obs] == [3.7, None, 3.9]
    assert obs[0].ts == datetime(2024, 1, 1, tzinfo=UTC)
    assert route.calls[1].request.url.params["offset"] == "2"
