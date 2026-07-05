"""Content-addressed media naming. Atomic writes moved to the feed host's
uploader: the MCP renders in memory and PUTs, so it never writes durable files."""
from __future__ import annotations

import hashlib

_CHUNK = 1 << 20


def content_hash8(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def media_filename(episode_n: int, hash8: str) -> str:
    return f"EP{episode_n:02d}-{hash8}.mp3"


def attachment_filename(episode_n: int, hash8: str, ext: str) -> str:
    """單集附件(pdf/html)的 content-addressed 檔名,與 media_filename 同族。"""
    return f"EP{episode_n:02d}-{hash8}.{ext}"
