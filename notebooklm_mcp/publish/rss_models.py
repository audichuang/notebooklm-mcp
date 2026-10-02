"""Episodes in episode-number order (tombstone removed project-wide)."""

from __future__ import annotations


def live_episodes(show: dict) -> list[tuple[int, dict]]:
    eps = [(int(k), ep) for k, ep in show.get("episodes", {}).items()]
    eps.sort(key=lambda pair: pair[0])
    return eps
