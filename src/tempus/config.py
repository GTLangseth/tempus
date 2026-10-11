from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "postgres://postgres:password@localhost:5432/tempus"
    fred_api_key: str | None = None
    typesafe_api_key: str | None = None
    catalog_path: Path = Path("catalog.toml")
    ontology_path: Path = Path("ontology/concepts.yaml")
    drafts_path: Path = Path("ontology/placements.draft.yaml")
    cards_path: Path = Path("cards")
    share_password: str | None = Field(None, validation_alias="TEMPUS_SHARE_PASSWORD")

    # Dev and prod databases: docs/specs/2026-10-11-dev-prod-db-sync-design.md.
    db: Literal["dev", "prod"] = Field("dev", validation_alias="TEMPUS_DB")
    homebase_host: str = Field("homebase", validation_alias="TEMPUS_HOMEBASE_HOST")
    prod_db_port: int = Field(5432, validation_alias="TEMPUS_PROD_DB_PORT")
    prod_ro_password: str | None = Field(None, validation_alias="TEMPUS_PROD_RO_PASSWORD")
    prod_password: str | None = Field(None, validation_alias="TEMPUS_PROD_PASSWORD")
    # On the home base: the password `tempus db readonly-role` gives tempus_ro.
    db_ro_password: str | None = Field(None, validation_alias="TEMPUS_DB_RO_PASSWORD")


def get_settings() -> Settings:
    """Settings from the environment and .env.

    With TEMPUS_DB=prod, `database_url` points at the home base's database through an SSH
    tunnel, as the read-only role (the tunnel opens on first use).
    """
    settings = Settings()
    if settings.db == "prod":
        from tempus.remote import read_only_settings

        return read_only_settings(settings)
    return settings
