from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgres://postgres:password@localhost:5432/tempus"
    fred_api_key: str | None = None
    catalog_path: Path = Path("catalog.toml")


def get_settings() -> Settings:
    return Settings()
