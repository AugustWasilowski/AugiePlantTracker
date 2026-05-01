"""Async Immich HTTP client.

Used by the auto-import worker. Wraps the small slice of Immich's API we
need: CLIP smart search, asset enrichment, and image download.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ImmichClient:
    """Small async client. Use as `async with ImmichClient(...) as client:`."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={
                "x-api-key": api_key,
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def __aenter__(self) -> "ImmichClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    # ---- search ----

    async def smart_search(self, query: str, size: int = 50) -> list[dict[str, Any]]:
        """CLIP smart search. NOTE: returns trimmed asset records — exifInfo
        (and therefore GPS) is NOT included. Follow up with get_asset() to
        enrich the few records you actually want to keep."""
        resp = await self._client.post(
            "/api/search/smart",
            json={"query": query, "size": size},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json().get("assets", {}).get("items", [])

    async def has_assets_after(self, after: datetime) -> bool:
        """Cheap delta check: True if any asset was uploaded to Immich after `after`.
        Uses metadata search (indexed on createdAt) with size=1 — no CLIP work."""
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        resp = await self._client.post(
            "/api/search/metadata",
            json={"createdAfter": after.isoformat(), "size": 1},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return bool(resp.json().get("assets", {}).get("items", []))

    # ---- assets ----

    async def get_asset(self, asset_id: str) -> dict[str, Any]:
        """Full asset record including exifInfo / GPS / capture timestamps."""
        resp = await self._client.get(f"/api/assets/{asset_id}")
        resp.raise_for_status()
        return resp.json()

    async def download_original(self, asset_id: str) -> bytes:
        """Full-resolution original bytes. We save these to disk verbatim;
        compression for the identify webhook happens via app.imaging."""
        resp = await self._client.get(f"/api/assets/{asset_id}/original")
        resp.raise_for_status()
        return resp.content

    # ---- url helpers ----

    def web_url(self, asset_id: str) -> str:
        """Deep-link into the Immich web UI for human inspection."""
        return f"{self._base}/photos/{asset_id}"
