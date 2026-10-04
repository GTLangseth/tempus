from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class SeriesMeta:
    series_id: str
    title: str | None = None
    units: str | None = None
    frequency: str | None = None
    seasonal_adjustment: str | None = None
    notes: str | None = None
    upstream_updated_at: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    ts: datetime
    value: float | None


class Source(Protocol):
    """A place time series come from. Implement this to add a new data provider."""

    name: str

    def fetch_meta(self, series_id: str) -> SeriesMeta: ...

    def fetch_observations(self, series_id: str) -> list[Observation]: ...
