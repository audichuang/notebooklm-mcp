"""File layout + atomic writes. Every write goes temp -> fsync -> os.replace so
a static file server never serves a half-written file. Media filenames are
content-addressed (EP{n}-{hash8}.mp3) so regenerated episodes get a NEW url
while the GUID stays stable (clients re-download but treat it as the same ep)."""
from __future__ import annotations

import hashlib
import os
import uuid

_CHUNK = 1 << 20


def content_hash8(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def media_filename(episode_n: int, hash8: str) -> str:
    return f"EP{episode_n:02d}-{hash8}.mp3"


def atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # Unique temp name so concurrent writers to the same target never collide
    # on the temp file (a fixed name would let one writer's os.replace race the
    # other's temp). pid + uuid is unique per process and per call.
    tmp = os.path.join(directory, f".tmp-{os.path.basename(path)}-{os.getpid()}-{uuid.uuid4().hex}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Best-effort directory fsync so the rename itself is durable.
    try:
        dfd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def atomic_write_text(path: str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_copy(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        data = f.read()
    atomic_write_bytes(dst, data)
