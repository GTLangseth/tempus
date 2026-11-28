from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "postgres://postgres:password@localhost:5432/tempus"
    fred_api_key: str | None = None
    typesafe_api_key: str | None = None
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    storage: Literal["db", "files"] = Field("db", validation_alias="TEMPUS_STORAGE")
    ingest_provider: Literal["auto", "gemini", "anthropic"] = Field(
        "auto", validation_alias="TEMPUS_INGEST_PROVIDER"
    )
    gemini_model: str = Field("gemini-2.5-flash", validation_alias="TEMPUS_GEMINI_MODEL")
    anthropic_model: str = Field(
        "claude-sonnet-4-5-20250929", validation_alias="TEMPUS_ANTHROPIC_MODEL"
    )
    catalog_path: Path = Path("catalog.toml")
    ontology_path: Path = Path("ontology/concepts.yaml")
    drafts_path: Path = Path("ontology/placements.draft.yaml")
    cards_path: Path = Path("cards")
    readings_path: Path = Path("readings")
    share_password: str | None = Field(None, validation_alias="TEMPUS_SHARE_PASSWORD")

    # Dev and prod databases: docs/specs/2026-10-11-dev-prod-db-sync-design.md.
    db: Literal["dev", "prod"] = Field("dev", validation_alias="TEMPUS_DB")
    homebase_host: str = Field("homebase", validation_alias="TEMPUS_HOMEBASE_HOST")
    prod_db_port: int = Field(5432, validation_alias="TEMPUS_PROD_DB_PORT")
    prod_ro_password: str | None = Field(None, validation_alias="TEMPUS_PROD_RO_PASSWORD")
    prod_password: str | None = Field(None, validation_alias="TEMPUS_PROD_PASSWORD")
    # On the home base: the password `tempus db readonly-role` gives tempus_ro.
    db_ro_password: str | None = Field(None, validation_alias="TEMPUS_DB_RO_PASSWORD")

    @property
    def uses_db_storage(self) -> bool:
        """Whether cards and readings live in Postgres rather than local repo files.

        Tests that construct Settings(cards_path=...) or Settings(readings_path=...) without
        overriding storage keep file-backed repositories so fixtures remain isolated.
        """
        if "storage" in self.model_fields_set:
            return self.storage == "db"
        if "cards_path" in self.model_fields_set or "readings_path" in self.model_fields_set:
            return False
        return self.storage == "db"


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
