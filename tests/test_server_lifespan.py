"""Regressions for the server app:

1. notebooklm-py 0.7.x from_storage() 是同步函式,回傳可直接 async with 的
   context(_FromStorageContext)。lifespan 必須用 no-await 慣用法。MCP 不啟動
   keepalive;Doppler NOTEBOOKLM_AUTH_JSON 是唯讀真相來源,不可觸發 RotateCookies。

2. The canonical `mcp` must actually expose the tools. They were registered on a
   different instance than the one served when launched via `python -m
   notebooklm_mcp.server` (the __main__ double-import trap) — the MCP came up
   with ZERO tools. Tools now live on `notebooklm_mcp.app.mcp`; assert they're there.
"""
import logging

import pytest

from notebooklm_mcp import app, runtime


DISABLE_KEEPALIVE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"
HEADLESS_REAUTH_ENV = "NOTEBOOKLM_HEADLESS_REAUTH"


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


async def test_lifespan_with_inline_auth_suppresses_headless_reauth(monkeypatch):
    """0.8.0 的 L3 headless re-auth 在 inline(Doppler)模式必須被壓掉。

    它會用持久瀏覽器 profile 靜默重鑄 cookie —— 跟 keepalive/RotateCookies 同一類
    災難:新 cookie 只活在這個 process、寫不回 Doppler,還會把 3 VM 共用的那份作廢。
    預設關不夠,環境裡被誰設成 "1" 就會在 RPC 中途自動觸發,所以 lifespan 要顯式刪掉,
    並在退出後**原樣還原**(不能順手把使用者的設定吃掉)。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')
    monkeypatch.setenv(HEADLESS_REAUTH_ENV, "1")

    def fake_from_storage(*args, **kwargs):
        assert kwargs.get("keepalive") is None
        assert HEADLESS_REAUTH_ENV not in app.os.environ
        return _FakeClientCM()

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", fake_from_storage)
    async with app._lifespan(app.mcp):
        assert HEADLESS_REAUTH_ENV not in app.os.environ
    assert app.os.environ[HEADLESS_REAUTH_ENV] == "1"


async def test_lifespan_enters_from_storage_context(monkeypatch):
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage)
    async with app._lifespan(app.mcp):
        # Inside the lifespan the client must be set (proves the CM entered).
        assert isinstance(runtime.get_client(), _FakeClientCM)
    # After exit the holder is cleared.
    with pytest.raises(RuntimeError):
        runtime.get_client()


async def test_second_lifespan_cannot_replace_the_first_pool(monkeypatch):
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage)
    async with app._lifespan(app.mcp):
        first = runtime.get_client()
        with pytest.raises(RuntimeError, match="另一個 MCP session"):
            async with app._lifespan(app.mcp):
                pass
        assert runtime.get_client() is first
    with pytest.raises(RuntimeError, match="not initialized"):
        runtime.get_client()


async def test_lifespan_suppresses_http_request_urls(monkeypatch, caplog):
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", logging.INFO)
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage)
    with caplog.at_level(logging.INFO):
        async with app._lifespan(app.mcp):
            httpx_logger.info("HTTP Request: GET https://example.test/?osidt=secret")
    assert "osidt=secret" not in caplog.text


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


def test_instructions_are_skeleton_not_parameter_detail():
    """P1 瘦身的漂移防護(不是行為契約):instructions 是「骨架」,參數級細節與
    routing 已各自存在 podcast_episode docstring / skill 文件裡一份,instructions
    重複第二份正是 AGENTS.md「刻意不複製 SKILL.md 以免漂移」要防的漂移源。
    (P0 manifest_path+prior_mp3_path 那條是鐵律而非參數細節,但它已由工具端的
    ValueError 秒退編碼、訊息自帶指引,instructions 不重複第二份。)

    正向鎖兩句必須存在的指引;細節長回來則靠總長度上限擋,不用逐字負向斷言把
    一次性的刪除固化成永久約束。
    """
    text = app._INSTRUCTIONS
    assert "路徑與冪等規則見 skill" in text
    assert "safe_next_action 續跑" in text
    # v0.9.26 起 1,271 chars(瘦身當時 1,065);上限留緩衝,但擋得住任何一整段細節長回來。
    assert len(text) <= 1400, f"instructions 長到 {len(text)} chars——細節請放 skill"
