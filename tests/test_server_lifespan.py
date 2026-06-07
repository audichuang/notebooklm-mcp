"""Regression: notebooklm-py 0.3.x from_storage() is a coroutine that MUST be
awaited. The lifespan used `async with NotebookLMClient.from_storage()` without
`await`, which raises TypeError at runtime on 0.3.x (offline tests using the
fake client never exercise from_storage, so they missed it). This test drives
the real lifespan against a fake from_storage and fails if the await is dropped.
"""
import pytest

from notebooklm_mcp import server, runtime


class _FakeClientCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _fake_from_storage(*args, **kwargs):
    # Mirrors 0.3.x: a coroutine returning an async-context-manager client.
    return _FakeClientCM()


async def test_lifespan_awaits_from_storage(monkeypatch):
    monkeypatch.setattr(server.NotebookLMClient, "from_storage", _fake_from_storage)
    async with server._lifespan(server.mcp):
        # Inside the lifespan the client must be set (proves the CM entered).
        assert isinstance(runtime.get_client(), _FakeClientCM)
    # After exit the holder is cleared.
    with pytest.raises(RuntimeError):
        runtime.get_client()
