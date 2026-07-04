"""Plain helpers for selecting live (non-tombstone) episodes in episode order."""
from __future__ import annotations


def live_episodes(show: dict) -> list[tuple[int, dict]]:
    """Return [(episode_n, ep_dict), ...] sorted by episode number, excluding
    tombstoned episodes."""
    out: list[tuple[int, dict]] = []
    for key, ep in show.get("episodes", {}).items():
        if ep.get("tombstone"):
            continue
        out.append((int(key), ep))
    out.sort(key=lambda pair: pair[0])
    return out
