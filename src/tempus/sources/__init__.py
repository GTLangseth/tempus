from tempus.config import Settings
from tempus.sources.base import Observation, SeriesMeta, Source
from tempus.sources.fred import FredSource

SOURCE_NAMES = ("fred",)


def get_source(name: str, settings: Settings) -> Source:
    match name:
        case "fred":
            return FredSource(settings.fred_api_key)
    raise ValueError(f"Unknown source {name!r}. Known: {', '.join(SOURCE_NAMES)}")


__all__ = ["SOURCE_NAMES", "Observation", "SeriesMeta", "Source", "get_source"]
