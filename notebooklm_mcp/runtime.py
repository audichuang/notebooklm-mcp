"""Holds the long-lived NotebookLMClient set up by the server lifespan.

Tool functions can fetch it without threading it through every call, and tests
can monkeypatch it with a fake client.
"""
from __future__ import annotations

from typing import Any

_CLIENT: Any | None = None


def set_client(client: Any) -> None:
    global _CLIENT
    _CLIENT = client


def get_client() -> Any:
    if _CLIENT is None:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    return _CLIENT
