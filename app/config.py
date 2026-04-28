"""Runtime configuration loaded from environment / .env file."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    n8n_identify_webhook_url: str = ""
    n8n_webhook_token: str = ""
    data_dir: Path = Path("/data")
    max_upload_mb: int = 25
    disable_identify: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "plants.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"


settings = Settings()

# Ensure required directories exist on import.
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.photos_dir.mkdir(parents=True, exist_ok=True)
settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
