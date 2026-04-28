"""Image utilities: EXIF timestamp extraction, thumbnail generation."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from PIL import ExifTags, Image, ImageOps

# 0x8769 is the EXIF SubIFD tag (where DateTimeOriginal lives on most cameras).
_EXIF_IFD_POINTER = 0x8769

_TAG_NAME_TO_ID = {name: tid for tid, name in ExifTags.TAGS.items()}
_DATETIME_TAG_NAMES = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")


def _parse_exif_dt(raw) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    # Cameras write 'YYYY:MM:DD HH:MM:SS'.
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def extract_captured_at(image_path: Path) -> Optional[datetime]:
    """Pull a capture timestamp out of EXIF, or return None.

    Photos from cameras / phones store DateTimeOriginal inside the EXIF
    sub-IFD, so we have to descend into it. We try the most-trustworthy
    fields first (Original > Digitized > top-level DateTime).
    """
    try:
        with Image.open(image_path) as im:
            exif = im.getexif()
    except Exception:
        return None

    if not exif:
        return None

    # Descend into the EXIF sub-IFD if it's there.
    candidates = [exif]
    try:
        sub = exif.get_ifd(_EXIF_IFD_POINTER)
        if sub:
            candidates.insert(0, sub)
    except Exception:
        pass

    for ifd in candidates:
        for name in _DATETIME_TAG_NAMES:
            tid = _TAG_NAME_TO_ID.get(name)
            if tid is None:
                continue
            dt = _parse_exif_dt(ifd.get(tid))
            if dt:
                return dt

    return None


def make_thumbnail(src: Path, dest: Path, max_side: int = 600) -> Tuple[int, int]:
    """Create a thumbnail honouring EXIF orientation. Returns (w, h) of the
    *original* image so the caller can record dimensions."""
    with Image.open(src) as im:
        original_size = im.size
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_side, max_side))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=85, optimize=True)
    return original_size
