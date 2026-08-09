"""多帳號 client pool(ADR-0010)。

家庭方案下有多個付費帳號,配額可以合起來用。Doppler 用同一個 config 注入
`NOTEBOOKLM_AUTH_JSON` + `_2`/`_3`…,lifespan 各建一個長駐 client。

**`get_client()` 的語意刻意不變**(「當前作用中的 client」),所以 46 個呼叫點
一行都不用改;pool 在 `runtime.py` 之上是隱形的。
"""
import pytest

from notebooklm_mcp import app, runtime


class _FakeClientCM:
    def __init__(self, tag=None):
        self.tag = tag

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clean_runtime():
    yield
    runtime.set_client(None)


# --- runtime 的 pool 語意 ---------------------------------------------------


def test_single_client_keeps_legacy_semantics():
    c = _FakeClientCM()
    runtime.set_client(c)
    assert runtime.get_client() is c
    # 只有一個帳號時輪替沒有對象:回 None,呼叫端據此知道「不用再試了」。
    assert runtime.rotate_client() is None
    assert runtime.account_count() == 1


def test_set_client_none_clears_the_pool():
    runtime.set_client(_FakeClientCM())
    runtime.set_client(None)
    with pytest.raises(RuntimeError):
        runtime.get_client()
    assert runtime.account_count() == 0
    assert runtime.rotate_client() is None


def test_rotate_cycles_through_every_account_then_returns_none():
    a, b, c = _FakeClientCM("a"), _FakeClientCM("b"), _FakeClientCM("c")
    runtime.set_clients([("a@x", a), ("b@x", b), ("c@x", c)])

    assert runtime.get_client() is a
    assert runtime.active_account() == "a@x"

    assert runtime.rotate_client() == "b@x"
    assert runtime.get_client() is b
    assert runtime.rotate_client() == "c@x"
    assert runtime.get_client() is c

    # 繞完一圈就停:回 None 而不是回到 a。呼叫端(failover)要能分辨
    # 「還有沒試過的帳號」與「全部都拒絕了」——無限輪替會變成永遠重試。
    assert runtime.rotate_client() is None
    assert runtime.get_client() is c


def test_rotation_resets_when_the_pool_is_reinstalled():
    runtime.set_clients([("a@x", _FakeClientCM()), ("b@x", _FakeClientCM())])
    runtime.rotate_client()
    runtime.set_clients([("a@x", _FakeClientCM()), ("b@x", _FakeClientCM())])
    assert runtime.active_account() == "a@x"


# --- lifespan 的憑證掃描 ----------------------------------------------------


def _fake_from_storage_recording(seen):
    def fake(*args, **kwargs):
        assert kwargs.get("keepalive") is None
        seen.append(app.os.environ["NOTEBOOKLM_AUTH_JSON"])
        return _FakeClientCM()

    return fake


async def test_lifespan_builds_one_client_per_credential(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[1]}')
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", '{"cookies":[2]}')
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_3", '{"cookies":[3]}')
    seen = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage_recording(seen))

    async with app._lifespan(app.mcp):
        assert runtime.account_count() == 3
        assert runtime.get_client() is not None

    # 每份憑證都真的被拿去建過 client(輪流覆寫 env,SDK 只認不帶後綴的那個)。
    assert seen == ['{"cookies":[1]}', '{"cookies":[2]}', '{"cookies":[3]}']
    # 而且 env 必須還原成原本那一份,不能停在最後一個帳號上。
    assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == '{"cookies":[1]}'


async def test_auth_env_tracks_the_active_account_inside_the_lifespan(monkeypatch):
    """`NOTEBOOKLM_AUTH_JSON` 必須**隨時**等於作用中帳號的憑證(v0.8.0 驗收 F-1)。

    SDK 的媒體下載在下載當下重讀這個 env,**不是**用 client 自己的 session。原本
    pool 建完 env 停在最後一個槽位、還原寫在 lifespan 最外層的 finally(server 關閉
    才跑),於是整個 server 生命週期裡所有下載都以**最後一個帳號**的身分發出:notebook
    沒分享給它就一律 401,而症狀出現在十幾分鐘後的 finalize,根因在這裡。

    **斷言必須在 `async with` 內部**——原本的測試在退出後才檢查 env,那時最外層
    finally 已經還原過,bug 正是從這個縫溜過去的。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[1]}')
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", '{"cookies":[2]}')
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_3", '{"cookies":[3]}')
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage_recording([]))

    async with app._lifespan(app.mcp):
        assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == '{"cookies":[1]}', (
            "pool 建完就要還原成作用中的那一個,不能停在最後一個槽位"
        )
        runtime.rotate_client()
        assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == '{"cookies":[2]}', (
            "failover 換到 B 之後,下載也必須以 B 的身分發出"
        )
        runtime.rotate_client()
        assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == '{"cookies":[3]}'


async def test_lifespan_without_extra_credentials_is_a_single_account(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[1]}')
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    seen = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage_recording(seen))

    async with app._lifespan(app.mcp):
        assert runtime.account_count() == 1
        assert runtime.rotate_client() is None
    assert seen == ['{"cookies":[1]}']


async def test_gap_in_the_numbering_fails_loud(monkeypatch):
    """`_2` 缺號但 `_4` 存在 —— 打錯字的形狀。

    靜默跳過會讓一個付費帳號永遠不進 pool,而症狀只是「配額比預期早用完」,
    幾乎不可能回頭查到是 Doppler 打錯一個字。啟動時就爆掉。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[1]}')
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_4", '{"cookies":[4]}')
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage_recording([]))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_4"):
        async with app._lifespan(app.mcp):
            pass


async def test_empty_extra_credential_fails_loud(monkeypatch):
    """空字串是「Doppler 有這個 key 但值沒設好」,不是「沒有這個帳號」。"""
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[1]}')
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", "   ")
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage_recording([]))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_2"):
        async with app._lifespan(app.mcp):
            pass


async def test_pool_is_torn_down_even_if_a_later_client_fails(monkeypatch):
    """第 2 個 client 建到一半炸掉,第 1 個已經 enter 的必須被關掉。

    否則 stdio server 啟動失敗會留下一條沒關的 HTTP 連線,而失敗路徑正是
    最不會被人盯著看的地方。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[1]}')
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", '{"cookies":[2]}')
    closed = []

    class _Tracking(_FakeClientCM):
        async def __aexit__(self, *exc):
            closed.append(self.tag)
            return False

    def fake(*args, **kwargs):
        if app.os.environ["NOTEBOOKLM_AUTH_JSON"] == '{"cookies":[2]}':
            raise ValueError("boom")
        return _Tracking("first")

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", fake)

    with pytest.raises(ValueError, match="boom"):
        async with app._lifespan(app.mcp):
            pass
    assert closed == ["first"]
    # 失敗也要還原 env,別把 pool 掃描的中間狀態留給下一段程式。
    assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == '{"cookies":[1]}'
