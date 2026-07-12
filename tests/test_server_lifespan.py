"""Regressions for the server app:

1. notebooklm-py 0.7.x from_storage() 是同步函式,回傳可直接 async with 的
   context(_FromStorageContext)。lifespan 必須用 no-await 慣用法。MCP 不啟動
   keepalive;Doppler NOTEBOOKLM_AUTH_JSON 是唯讀真相來源,不可觸發 RotateCookies。

2. The canonical `mcp` must actually expose the tools. They were registered on a
   different instance than the one served when launched via `python -m
   notebooklm_mcp.server` (the __main__ double-import trap) — the MCP came up
   with ZERO tools. Tools now live on `notebooklm_mcp.app.mcp`; assert they're there.
"""
import pytest

from notebooklm_mcp import app, runtime


DISABLE_KEEPALIVE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"


class _FakeClientCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_from_storage(*args, **kwargs):
    # 鏡射 0.7.3:同步函式,回傳可直接 async with 的 context。
    return _FakeClientCM()


async def test_lifespan_does_not_enable_background_keepalive(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.delenv(DISABLE_KEEPALIVE_ENV, raising=False)

    def fake_from_storage(*args, **kwargs):
        assert kwargs.get("keepalive") is None
        assert DISABLE_KEEPALIVE_ENV not in app.os.environ
        return _FakeClientCM()

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", fake_from_storage)
    async with app._lifespan(app.mcp):
        assert isinstance(runtime.get_client(), _FakeClientCM)


async def test_lifespan_with_inline_auth_disables_cookie_rotation(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')
    monkeypatch.delenv(DISABLE_KEEPALIVE_ENV, raising=False)

    def fake_from_storage(*args, **kwargs):
        assert kwargs.get("keepalive") is None
        assert app.os.environ[DISABLE_KEEPALIVE_ENV] == "1"
        return _FakeClientCM()

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", fake_from_storage)
    async with app._lifespan(app.mcp):
        assert isinstance(runtime.get_client(), _FakeClientCM)
    assert DISABLE_KEEPALIVE_ENV not in app.os.environ


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
        "episode_set_description",
        "podcast_episode",
        "podcast_series",
    }
    missing = expected - names
    assert not missing, f"MCP is not exposing tools: {missing}"


def test_server_reexports_for_backwards_compat():
    from notebooklm_mcp import server

    assert server.mcp is app.mcp
    assert server.main is app.main
