"""Stable feed identity. The feed's token is derived deterministically from a
stable ``show_id`` via HMAC, so the same show always maps to the same URL with
NO global registry (hence no multi-VM read-modify-write race)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

# Lowercase, digits, internal hyphens; 2..64 chars; no leading/trailing hyphen.
_SHOW_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])$")


def validate_show_id(show_id: str) -> str:
    if not isinstance(show_id, str) or not _SHOW_ID_RE.fullmatch(show_id):
        raise ValueError(
            "show_id must be 2-64 chars of [a-z0-9-], no leading/trailing hyphen "
            f"(got: {show_id!r})"
        )
    return show_id


def make_token(show_id: str, salt: str) -> str:
    """Deterministic, unguessable feed token: base32(lower) of HMAC-SHA256.

    15 raw bytes -> 24 base32 chars with no '=' padding, charset [a-z2-7]
    (URL/filename safe; avoids '-' '_' '+' '/')."""
    validate_show_id(show_id)
    if not salt:
        raise ValueError("PODCAST_TOKEN_SALT is required and must be non-empty")
    digest = hmac.new(salt.encode("utf-8"), show_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(digest[:15]).decode("ascii").lower().rstrip("=")


def episode_guid(show_id: str, episode_n: int) -> str:
    """Stable per-episode GUID, decoupled from the content source: same
    (show_id, episode_n) -> same GUID forever, so regenerating an episode reads
    as an update, not a new item. (Moved here from the deleted state.py.)"""
    validate_show_id(show_id)
    return hashlib.sha1(f"{show_id}:{episode_n}".encode()).hexdigest()
