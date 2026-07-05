"""Regressions for the server app:

1. notebooklm-py 0.3.x from_storage() is a coroutine that MUST be awaited.
   The lifespan used `async with NotebookLMClient.from_storage()` without
   `await`, which raises TypeError at runtime on 0.3.x (offline tests using the
   fake client never exercise from_storage, so they missed it).

2. The canonical `mcp` must actually expose the tools. They were registered on a
   different instance than the one served when launched via `python -m
   notebooklm_mcp.server` (the __main__ double-import trap) — the MCP came up
   with ZERO tools. Tools now live on `notebooklm_mcp.app.mcp`; assert they're there.
"""
import pytest

from notebooklm_mcp import app, runtime


class _FakeClientCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _fake_from_storage(*args, **kwargs):
    # Mirrors 0.3.x: a coroutine returning an async-context-manager client.
    return _FakeClientCM()


async def test_lifespan_awaits_from_storage(monkeypatch):
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage)
    async with app._lifespan(app.mcp):
        # Inside the lifespan the client must be set (proves the CM entered).
        assert isinstance(runtime.get_client(), _FakeClientCM)
    # After exit the holder is cleared.
    with pytest.raises(RuntimeError):
        runtime.get_client()


async def test_mcp_exposes_expected_tools():
    tools = await app.mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "notebook_create",
        "notebook_list",
        "source_add_url",
        "source_add_text",
        "source_add_file",
        "source_delete",
        "generate_audio",
        "artifact_list",
        "artifact_wait",
        "artifact_download_audio",
        "artifact_rename",
        "source_list",
        "source_fulltext",
        "notebook_get",
        "chat_ask",
        "podcast_episode",
        "podcast_series",
    }
    missing = expected - names
    assert not missing, f"MCP is not exposing tools: {missing}"


def test_server_reexports_for_backwards_compat():
    from notebooklm_mcp import server

    assert server.mcp is app.mcp
    assert server.main is app.main
