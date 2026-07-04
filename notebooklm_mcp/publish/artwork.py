"""Validate podcast artwork against Apple's Show Cover requirements:
square, 1400-3000 px per side, PNG or JPEG, RGB, no alpha channel."""
from __future__ import annotations

import os

from PIL import Image

_MIN, _MAX = 1400, 3000


def validate_artwork(path: str) -> dict:
    if not os.path.exists(path):
        raise ValueError(f"artwork file not found: {path}")
    try:
        with Image.open(path) as img:
            fmt = img.format
            width, height = img.size
            bands = img.getbands()
            mode = img.mode
    except OSError as exc:
        raise ValueError(f"artwork is not a readable image: {exc}") from None

    if fmt not in ("PNG", "JPEG"):
        raise ValueError(f"artwork must be PNG or JPEG (got: {fmt})")
    if width != height:
        raise ValueError(f"artwork must be square (got: {width}x{height})")
    if width < _MIN:
        raise ValueError(f"artwork side must be >= {_MIN}px (got: {width})")
    if width > _MAX:
        raise ValueError(f"artwork side must be <= {_MAX}px (got: {width})")
    if "A" in bands:
        raise ValueError("artwork must not have an alpha channel (use flat RGB)")
    if mode != "RGB":
        # Reject grayscale (L), CMYK, palette (P) etc. Apple wants flat RGB.
        raise ValueError(f"artwork must be RGB color space (got mode: {mode})")

    return {"width": width, "height": height, "format": fmt}
