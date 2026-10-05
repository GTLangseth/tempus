from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgres://postgres:password@localhost:5432/tempus"
    fred_api_key: str | None = None
    typesafe_api_key: str | None = None
    catalog_path: Path = Path("catalog.toml")
    ontology_path: Path = Path("ontology/concepts.yaml")
    drafts_path: Path = Path("ontology/placements.draft.yaml")
    cards_path: Path = Path("cards")
    share_password: str | None = Field(None, validation_alias="TEMPUS_SHARE_PASSWORD")


def get_settings() -> Settings:
    return Settings()
