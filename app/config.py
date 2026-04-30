"""Runtime configuration loaded from environment / .env file."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, List

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    n8n_identify_webhook_url: str = ""
    n8n_webhook_token: str = ""
    data_dir: Path = Path("/data")
    max_upload_mb: int = 25
    disable_identify: bool = False

    # ---- Immich auto-import (optional; leave IMMICH_URL/IMMICH_API_KEY blank to disable) ----
    immich_url: str = ""
    immich_api_key: str = ""
    # Decimal degrees of your house. Anything outside HOME_RADIUS_KM is dropped.
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_radius_km: float = 0.5
    # How often the background worker polls Immich, in minutes. 0 disables it.
    sync_interval_minutes: int = 15
    # CLIP smart-search queries. Comma-separated in env (the validator splits).
    # NoDecode disables pydantic-settings' default JSON parse for List fields.
    clip_queries: Annotated[List[str], NoDecode] = [
        "houseplant",
        "potted plant indoors",
        "succulent on a shelf",
        "leafy green plant in a pot",
    ]
    clip_results_per_query: int = 50

    @field_validator("clip_queries", mode="before")
    @classmethod
    def _split_queries(cls, v):
        if isinstance(v, str):
            return [q.strip() for q in v.split(",") if q.strip()]
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "plants.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def immich_enabled(self) -> bool:
        return bool(self.immich_url and self.immich_api_key)


settings = Settings()

# Ensure required directories exist on import.
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.photos_dir.mkdir(parents=True, exist_ok=True)
settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
