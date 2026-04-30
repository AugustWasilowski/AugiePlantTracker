"""Thin client for the n8n plant-identification webhook."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from . import imaging
from .config import settings

log = logging.getLogger(__name__)


class IdentifyResult(dict):
    """Permissive container — n8n / the LLM may add fields we didn't anticipate."""

    @property
    def species(self) -> str | None:
        return self.get("species") or None

    @property
    def common_name(self) -> str | None:
        return self.get("common_name") or self.get("commonName") or None

    @property
    def confidence(self) -> float | None:
        c = self.get("confidence")
        try:
            return float(c) if c is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def care_notes(self) -> str | None:
        return self.get("care_notes") or self.get("careNotes") or None

    @property
    def growth(self) -> dict[str, Any]:
        g = self.get("growth")
        return g if isinstance(g, dict) else {}


async def identify(image_path: Path) -> IdentifyResult | None:
    """POST the image to n8n and return parsed result, or None if disabled / failed."""
    if settings.disable_identify or not settings.n8n_identify_webhook_url:
        log.info("Identify disabled or webhook not set; skipping.")
        return None

    headers = {}
    if settings.n8n_webhook_token:
        headers["X-Plant-Tracker-Token"] = settings.n8n_webhook_token

    try:
        payload = imaging.make_identify_payload(image_path)
    except Exception as e:
        log.warning("Could not encode %s for identify: %s", image_path, e)
        return None

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"image": ("plant.jpg", payload, "image/jpeg")}
            resp = await client.post(
                settings.n8n_identify_webhook_url,
                files=files,
                headers=headers,
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("n8n identify call failed: %s", e)
        return None

    try:
        data = resp.json()
    except json.JSONDecodeError:
        log.warning("n8n returned non-JSON: %r", resp.text[:200])
        return None

    # n8n often wraps single results in an array — unwrap.
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None
    return IdentifyResult(data)
