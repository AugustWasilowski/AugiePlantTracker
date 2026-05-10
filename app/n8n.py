"""Thin client for the n8n plant-identification webhook."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

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


IdentifyStatus = Literal["ok", "retry", "permanent", "skipped"]


@dataclass
class IdentifyOutcome:
    """Result of one identify attempt.

    - ``ok``: ``result`` is an IdentifyResult (possibly with confidence=0 if
      Claude looked but couldn't tell — that's a final answer, not a failure).
    - ``retry``: transient failure (HTTP 429/5xx, timeout, connect error,
      n8n hit an Anthropic rate limit / overload). Caller should re-queue.
    - ``permanent``: non-retryable (e.g. image couldn't be encoded). The
      caller shouldn't keep banging on this one.
    - ``skipped``: identify is disabled or no webhook URL configured.
    """

    status: IdentifyStatus
    result: Optional[IdentifyResult] = None
    error: Optional[str] = None  # short tag for logs / DB, e.g. "http_429"


# HTTP status codes from n8n that mean "Claude was unreachable / rate-limited
# / overloaded; try again later." 429 = rate limit; 529 = Anthropic overloaded;
# 5xx = generic server error (the workflow itself failing reaches the caller
# as 500 from n8n since the Anthropic node throws before the Respond node).
_RETRY_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 522, 524, 529}


async def identify(image_path: Path) -> IdentifyOutcome:
    """POST the image to n8n and return a structured outcome."""
    if settings.disable_identify or not settings.n8n_identify_webhook_url:
        log.info("Identify disabled or webhook not set; skipping.")
        return IdentifyOutcome(status="skipped")

    headers = {}
    if settings.n8n_webhook_token:
        headers["X-Plant-Tracker-Token"] = settings.n8n_webhook_token

    try:
        payload = imaging.make_identify_payload(image_path)
    except Exception as e:
        log.warning("Could not encode %s for identify: %s", image_path, e)
        return IdentifyOutcome(status="permanent", error=f"encode:{e.__class__.__name__}")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"image": ("plant.jpg", payload, "image/jpeg")}
            resp = await client.post(
                settings.n8n_identify_webhook_url,
                files=files,
                headers=headers,
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
        log.warning("n8n identify network error: %s", e)
        return IdentifyOutcome(status="retry", error=f"net:{e.__class__.__name__}")
    except httpx.HTTPError as e:
        log.warning("n8n identify HTTP error: %s", e)
        return IdentifyOutcome(status="retry", error=f"http:{e.__class__.__name__}")

    if resp.status_code in _RETRY_STATUS_CODES:
        log.warning("n8n identify returned %d — will retry", resp.status_code)
        return IdentifyOutcome(status="retry", error=f"http_{resp.status_code}")
    if resp.status_code >= 400:
        log.warning("n8n identify returned %d (not retryable)", resp.status_code)
        return IdentifyOutcome(status="permanent", error=f"http_{resp.status_code}")

    try:
        data = resp.json()
    except json.JSONDecodeError:
        log.warning("n8n returned non-JSON: %r", resp.text[:200])
        return IdentifyOutcome(status="retry", error="bad_json")

    # n8n often wraps single results in an array — unwrap.
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return IdentifyOutcome(status="retry", error="bad_shape")

    # If the Parse Plant JSON node failed to parse Claude's reply, it emits a
    # `_parse_error` field. That's likely a Claude-side issue (truncation,
    # rate-limit fallback message, etc.) — worth retrying.
    if data.get("_parse_error"):
        log.warning("n8n parse error in identify response: %s", data.get("_parse_error"))
        return IdentifyOutcome(status="retry", error="parse_error")

    return IdentifyOutcome(status="ok", result=IdentifyResult(data))
