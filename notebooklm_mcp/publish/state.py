"""Authoritative per-show state (show.json). The feed.xml/index.html are derived
FROM this file, so we write state first (atomically) then render outputs from the
committed state. GUIDs are derived from (show_id, episode_n) and never change."""
from __future__ import annotations

import hashlib
import json
import os

from . import layout

SHOW_JSON = "show.json"


def episode_guid(show_id: str, episode_n: int) -> str:
    return hashlib.sha1(f"{show_id}:{episode_n}".encode("utf-8")).hexdigest()


def load_show(feed_dir: str) -> dict | None:
    path = os.path.join(feed_dir, SHOW_JSON)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_show(feed_dir: str, show: dict) -> None:
    path = os.path.join(feed_dir, SHOW_JSON)
    text = json.dumps(show, ensure_ascii=False, indent=2, sort_keys=False)
    layout.atomic_write_text(path, text)
