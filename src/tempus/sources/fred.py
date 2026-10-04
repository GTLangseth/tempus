"""FRED (Federal Reserve Economic Data) — https://fred.stlouisfed.org/docs/api/fred/"""

import time
from datetime import UTC, datetime

import httpx

from tempus.sources.base import Observation, SeriesMeta

BASE_URL = "https://api.stlouisfed.org/fred"
PAGE_SIZE = 100_000  # FRED's max for series/observations
MAX_RETRIES = 4


class FredError(RuntimeError):
    pass


class FredSource:
    name = "fred"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise FredError(
                "FRED_API_KEY is not set. Get a free key at "
                "https://fred.stlouisfed.org/docs/api/api_key.html and put it in .env"
            )
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30)

    def _get(self, path: str, **params) -> dict:
        params |= {"api_key": self._api_key, "file_type": "json"}
        for attempt in range(MAX_RETRIES):
            resp = self._client.get(f"{BASE_URL}/{path}", params=params)
            # FRED allows 120 req/min; back off on throttling and transient errors.
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                message = resp.json().get("error_message", resp.text)
                raise FredError(f"FRED {path} failed ({resp.status_code}): {message}")
            return resp.json()
        raise FredError(f"FRED {path} failed after {MAX_RETRIES} attempts")

    def fetch_meta(self, series_id: str) -> SeriesMeta:
        data = self._get("series", series_id=series_id)
        return _parse_meta(data["seriess"][0])

    def fetch_observations(self, series_id: str) -> list[Observation]:
        observations: list[Observation] = []
        offset = 0
        while True:
            data = self._get(
                "series/observations", series_id=series_id, limit=PAGE_SIZE, offset=offset
            )
            observations += [
                Observation(ts=_parse_date(o["date"]), value=_parse_value(o["value"]))
                for o in data["observations"]
            ]
            offset += PAGE_SIZE
            if offset >= data["count"]:
                return observations

    def search(self, text: str, limit: int = 20) -> list[SeriesMeta]:
        data = self._get(
            "series/search",
            search_text=text,
            limit=limit,
            order_by="popularity",
            sort_order="desc",
        )
        return [_parse_meta(s) for s in data["seriess"]]


def _parse_meta(s: dict) -> SeriesMeta:
    return SeriesMeta(
        series_id=s["id"],
        title=s.get("title"),
        units=s.get("units"),
        frequency=s.get("frequency"),
        seasonal_adjustment=s.get("seasonal_adjustment"),
        notes=s.get("notes"),
        upstream_updated_at=_parse_last_updated(s.get("last_updated")),
        extra={
            k: s[k]
            for k in ("observation_start", "observation_end", "popularity", "frequency_short")
            if k in s
        },
    )


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def _parse_value(value: str) -> float | None:
    return None if value == "." else float(value)


def _parse_last_updated(value: str | None) -> datetime | None:
    # FRED format: "2024-08-02 07:44:02-05" (hour-only UTC offset)
    if not value:
        return None
    return datetime.strptime(value + "00", "%Y-%m-%d %H:%M:%S%z")
