"""Runtime configuration loaded from environment / .env file."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, List

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    n8n_identify_webhook_url: str = ""
    n8n_lookup_webhook_url: str = ""
    n8n_chat_webhook_url: str = ""
    n8n_webhook_token: str = ""
    # Public origin used to build absolute photo URLs sent to n8n (so the
    # Anthropic API can fetch the image). Falls back to the request's host.
    public_base_url: str = ""
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
        # Broad
        "houseplant",
        "potted plant indoors",
        "indoor plant",
        # Succulents / cacti
        "succulent plant",
        "cactus in a pot",
        # Macro / close-up / unusual color
        "close-up of a plant leaf",
        "variegated plant leaf",
        "small plant in a pot",
        # Terrariums / planted scenes
        "terrarium with plants",
        "miniature indoor garden",
        # Plant-care contexts
        "plant under a grow light",
        "plant cutting propagation",
        "hanging plant indoors",
        "flowering houseplant",
        # Common species (CLIP handles these well)
        "philodendron",
        "monstera plant",
        "pothos vine",
        "snake plant",
        "orchid in bloom",
    ]
    clip_results_per_query: int = 25
    # Optional extra named geofences as a JSON array, e.g.
    # GEOFENCES='[{"name":"Mom and Dad","lat":42.04,"lon":-88.30,"radius_km":0.5}]'
    # Anything inside any geofence is imported and tagged with that location.
    # The single HOME_* set above is treated as the implicit "Home" geofence.
    geofences: List[dict] = []
    home_name: str = "Home"

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

    @property
    def all_geofences(self) -> List[dict]:
        """Combined list of geofences: implicit Home (from HOME_*) + GEOFENCES extras.

        Each entry is a dict with keys: name, lat, lon, radius_km.
        Returns [] if no geofence is configured at all (both HOME_* and GEOFENCES empty)."""
        out: List[dict] = []
        if self.home_lat or self.home_lon:
            out.append({
                "name": self.home_name,
                "lat": float(self.home_lat),
                "lon": float(self.home_lon),
                "radius_km": float(self.home_radius_km),
            })
        for g in self.geofences:
            try:
                out.append({
                    "name": str(g["name"]),
                    "lat": float(g["lat"]),
                    "lon": float(g["lon"]),
                    "radius_km": float(g.get("radius_km", 0.5)),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out


settings = Settings()

# Ensure required directories exist on import.
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.photos_dir.mkdir(parents=True, exist_ok=True)
settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
