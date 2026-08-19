"""Episode display title vs Studio / source name.

Serial RSS titles must carry ``EP{NN}. `` because most players hide
``itunes:episode``. The Studio artifact and the self-uploaded source still use
the iron rule ``EP{NN} 正文`` — same string on both sides, no dotted prefix.
This module is the only place that derives one from the other.
"""
from __future__ import annotations


def bare_episode_title(episode_n: int, title: str) -> str:
    """Strip a matching ``EP{NN}. `` / ``EP{NN} `` prefix; leave anything else."""
    bare = title.strip()
    dotted = f"EP{episode_n:02d}."
    plain = f"EP{episode_n:02d}"
    if bare.startswith(dotted + " "):
        return bare[len(dotted) + 1 :].strip()
    if bare == dotted:
        return ""
    if bare.startswith(plain + " "):
        return bare[len(plain) + 1 :].strip()
    if bare == plain:
        return ""
    return bare


def episode_label(episode_n: int, title: str) -> str:
    """Unified Studio artifact + feedback-source name: ``EP{NN} 正文``."""
    bare = bare_episode_title(episode_n, title)
    return f"EP{episode_n:02d} {bare}" if bare else f"EP{episode_n:02d}"
