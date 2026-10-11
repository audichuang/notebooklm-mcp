"""Episodes in episode-number order(單一 consumer:feed.py)."""

from __future__ import annotations


def live_episodes(show: dict) -> list[tuple[int, dict]]:
    eps = [(int(k), ep) for k, ep in show.get("episodes", {}).items()]
    eps.sort(key=lambda pair: pair[0])
    return eps
