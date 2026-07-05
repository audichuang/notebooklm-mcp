"""Regressions for the server app:

1. notebooklm-py 0.7.x from_storage() 是同步函式,回傳可直接 async with 的
   context(_FromStorageContext)。lifespan 必須用 no-await 慣用法,且必須帶
   keepalive=600(session 內背景 RotateCookies;掉了會讓長生成中途認證死)。

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


def _fake_from_storage(*args, **kwargs):
    # 鏡射 0.7.3:同步函式,回傳可直接 async with 的 context。
    # lifespan 掉了 keepalive=600 這裡就紅(它是長生成不中途死的關鍵)。
    assert kwargs.get("keepalive") == 600
    return _FakeClientCM()


async def test_lifespan_enters_from_storage_context(monkeypatch):
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
        "auth_check",
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
