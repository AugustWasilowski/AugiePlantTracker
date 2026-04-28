"""Helpers for saving uploaded photos onto disk."""
from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from .config import settings


def _safe_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    # Whitelist of formats Pillow + browsers handle well.
    if ext in {"jpg", "jpeg", "png", "webp", "heic", "tif", "tiff", "bmp"}:
        return ext
    return "jpg"


def save_upload(stream: BinaryIO, original_filename: str) -> Path:
    """Persist an uploaded file under data/photos/YYYY/MM/<random>.<ext>.
    Returns the absolute path. Caller is responsible for closing the stream."""
    now = datetime.utcnow()
    rel_dir = Path(f"{now:%Y}") / f"{now:%m}"
    out_dir = settings.photos_dir / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = _safe_ext(original_filename)
    name = f"{now:%Y%m%dT%H%M%S}-{secrets.token_hex(4)}.{ext}"
    out_path = out_dir / name

    with out_path.open("wb") as fh:
        # Stream in 1MB chunks so we don't load huge images fully into RAM.
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    return out_path


def relative(path: Path) -> str:
    """Path relative to data_dir, suitable for storing in the DB."""
    return str(path.relative_to(settings.data_dir))


def absolute(rel: str) -> Path:
    return settings.data_dir / rel
