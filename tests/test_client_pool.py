"""多帳號 client pool(ADR-0010)。

家庭方案下有多個付費帳號,配額可以合起來用。Doppler 用同一個 config 注入
`NOTEBOOKLM_AUTH_JSON` + `_2`/`_3`…,lifespan 各建一個長駐 client。

**`get_client()` 的語意刻意不變**(「當前作用中的 client」),所以 46 個呼叫點
一行都不用改;pool 在 `runtime.py` 之上是隱形的。
"""
import json
import logging
import stat
from pathlib import Path

import pytest

from notebooklm.auth import (
    NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV,
    NOTEBOOKLM_REFRESH_CMD_ENV,
    NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV,
)
from notebooklm._auth.headless_reauth import NOTEBOOKLM_HEADLESS_REAUTH_ENV
from notebooklm_mcp import app, runtime


class _FakeClientCM:
    """一個槽位一個獨立物件 —— 「這通 RPC 打給哪個 client」才觀測得到。

    pool 測試長期以來每個槽位塞的是**同一個** fake,所以那一維在測試裡根本不存在
    (env 對不對看得到,實際發給誰看不到)。
    """

    def __init__(self, cred=None, path=None):
        self.cred = cred
        self.path = path
        self.closed = False
        self.email = None  # None ⇒ _account_label 退回 "#N"
        self.email_exc = None

    async def get_account_email(self):
        if self.email_exc is not None:
            raise self.email_exc
        return self.email

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture(autouse=True)
def _clean_runtime():
    yield
    runtime.set_client(None)


def _cred(tag: str) -> str:
    """能通過 SDK cookie 驗證的最小 storage_state。

    多帳號路徑落檔前會用 SDK 自己的 `extract_cookies_from_storage` 驗一次
    (見 `app._write_credential_file`),所以測試憑證不能再是 `{"cookies":[1]}`。
    """
    return json.dumps(
        {
            "cookies": [
                {"name": n, "value": tag, "domain": ".google.com", "path": "/"}
                for n in ("SID", "__Secure-1PSIDTS", "OSID")
            ]
        }
    )


def _blank_psidts_cred() -> str:
    """必要 cookie 都「在」,但 `__Secure-1PSIDTS` 的值是空字串。

    真實 0.8.1 的 sanitizer 會在解析階段把空值 row 整列丟掉,所以缺 key 與空值對
    `extract_cookies_from_storage` 而言都是同一種 raise。這裡仍保留本地非空值檢查作
    backstop,擋繞過 sanitizer 的呼叫端與上游未來改回放行空值的情況;真正擋住過期或
    scope 錯 PSIDTS 觸發 heal 的承重牆是 rotation flock。
    """
    return json.dumps(
        {
            "cookies": [
                {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                {"name": "__Secure-1PSIDTS", "value": "", "domain": ".google.com", "path": "/"},
                {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
                {"name": "SAPISID", "value": "s", "domain": ".google.com", "path": "/"},
            ]
        }
    )


def _expired_psidts_cred() -> str:
    """必要 cookie 都非空,但 PSIDTS 的固定 expiry 已經在過去。"""
    return json.dumps(
        {
            "cookies": [
                {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "t",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 1,
                },
                {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
                {"name": "SAPISID", "value": "s", "domain": ".google.com", "path": "/"},
            ]
        }
    )


def _expired_unique_psidts_cred() -> str:
    """建立一份 PSIDTS 值可辨識的過期憑證,避免 warning 測試誤判短字串。"""
    storage_state = json.loads(_expired_psidts_cred())
    for cookie in storage_state["cookies"]:
        if cookie["name"] == "__Secure-1PSIDTS":
            cookie["value"] = "unique-secret-psidts-marker-should-not-leak"
    return json.dumps(storage_state)


def test_blank_value_is_rejected_even_if_upstream_stops_doing_it(monkeypatch):
    """本地非空值檢查目前零覆蓋——註解掉它,全套照樣可能全綠,因為真實 0.8.1 的
    sanitizer 已經在 `extract_cookies_from_storage` 那一關把空值攔下來。這條測試
    monkeypatch 掉上游函式、繞過 sanitizer,直接打中本地分支,鎖住它是 backstop
    這件事本身：上游哪天把預設行為改回「放行空值」,這裡不能跟著失守。
    """
    from notebooklm_mcp import _cookies

    monkeypatch.setattr(
        _cookies,
        "extract_cookies_from_storage",
        lambda _storage_state: {"SID": "", "__Secure-1PSIDTS": "t"},
    )
    with pytest.raises(ValueError, match=r"必要 cookie 缺少或值是空的:\['SID'\]"):
        _cookies.assert_usable_storage_state({"cookies": []})


def _strict_loader_accepts(cred: str, path: Path) -> bool:
    """SDK 真正開檔時走的那顆驗證器(recovery 的上游,不含 recovery 本身)。"""
    from notebooklm._auth.cookies import _build_httpx_cookies_from_storage_strict

    path.write_text(cred, encoding="utf-8")
    try:
        _build_httpx_cookies_from_storage_strict(path)
    except ValueError:
        return False
    return True


def _precheck_accepts(cred: str, path: Path) -> bool:
    """我們在落檔前那顆預驗證(`_write_credential_file` 的前半段)。"""
    try:
        app._write_credential_file(cred, path, 1)
    except RuntimeError:
        return False
    return True


def _fake_from_storage(built, emails=None):
    """`NotebookLMClient.from_storage` 的替身,記下每個 client 是用哪份憑證建的。

    多帳號走 `path=`(讀檔),單帳號走 env —— 兩條都折算成「憑證字串」,測試因此
    不必知道實作走了哪一條,只在意「這個 client 是誰」。
    """

    def fake(*args, path=None, **kwargs):
        assert kwargs.get("keepalive") is None
        cred = (
            Path(path).read_text(encoding="utf-8")
            if path
            else app.os.environ["NOTEBOOKLM_AUTH_JSON"]
        )
        client = _FakeClientCM(cred=cred, path=Path(path) if path else None)
        if emails is not None:
            client.email = emails[len(built)]
        built.append(client)
        return client

    return fake


def _fake_from_storage_no_env(built):
    """非 inline 模式的替身:既沒有 path 也沒有 env,client 只是存在。"""

    def fake(*args, **kwargs):
        built.append(_FakeClientCM())
        return built[-1]

    return fake


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


def test_snapshot_takes_the_label_and_client_from_the_same_slot():
    """記帳點要的是「這兩個值屬於同一個槽位」,不是「各自都是最新的」。

    MCP 是並行的:`active_account()` 與 `get_client()` 分兩次讀,中間夾著 await,
    另一個工具呼叫在那個縫裡 rotate,manifest 就會記 A 而實際由 B 送出 ——
    ADR-0010 §Transparency 唯一的稽核憑據當場失真,而且事後查不出來
    (兩個帳號都成功,只是掛錯名)。
    """
    a, b = _FakeClientCM("a"), _FakeClientCM("b")
    runtime.set_clients([("a@x", a), ("b@x", b)])

    label, client = runtime.snapshot()
    assert (label, client) == ("a@x", a)

    # 拿到之後才 rotate:呼叫端手上的 client 不受影響,記下的 label 仍然對應它。
    runtime.rotate_client()
    assert client is a
    assert runtime.snapshot() == ("b@x", b)


def test_snapshot_without_a_pool_raises_like_get_client():
    runtime.set_client(None)
    with pytest.raises(RuntimeError):
        runtime.snapshot()


# --- lifespan 的憑證掃描 ----------------------------------------------------


async def test_lifespan_builds_one_client_per_credential(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_3", _cred("3"))
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        assert runtime.account_count() == 3
        assert runtime.get_client() is built[0]

    # 每份憑證都真的被拿去建過 client。
    assert [c.cred for c in built] == [_cred("1"), _cred("2"), _cred("3")]


async def test_each_pooled_client_carries_its_own_credential_file(monkeypatch):
    """身分跟著 client 走,**不是**跟著 process 全域 env 走(v0.8.0 驗收 F-1)。

    SDK 的媒體下載在下載當下重讀身分:`_storage_path is None` 時才回頭讀
    `NOTEBOOKLM_AUTH_JSON`,有 path 就用 client 自己那一份
    (`_artifact/downloads.py` 的 `self._cookie_loader(self._storage_path)`)。

    舊修法是「set_clients / rotate_client 同步 env」。那在**並行**的 MCP 下不成立:
    一個全域槽沒辦法同時是兩個值 —— EP05 正在 finalize(client 已 pin 住)時
    EP06 撞配額 rotate,EP05 的下載就以別人的身分發出,而症狀十幾分鐘後才浮現。
    所以身分改成建構時注入,env 從此**完全不動**。

    憑證因此會落檔(此前刻意不落檔)。代價受控:0700 目錄 + 0600 檔案,
    lifespan 結束就整個刪掉。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        paths = [c.path for c in built]
        assert all(p is not None for p in paths), "每個槽位都要有自己的憑證檔"
        assert len(set(paths)) == 2, "兩個槽位不能共用同一個檔"
        for path, tag in zip(paths, ("1", "2")):
            assert path.read_text(encoding="utf-8") == _cred(tag)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, "憑證檔不能讓 umask 決定權限"
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

        # env 從頭到尾都是呼叫端給的那一份 —— 它不再是身分,也不再是暫存槽。
        assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == _cred("1")
        runtime.rotate_client()
        assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == _cred("1")

    # 關掉之後憑證不留在磁碟上。
    assert not paths[0].exists()
    assert not paths[0].parent.exists()


async def test_lifespan_without_extra_credentials_is_a_single_account(monkeypatch):
    """單帳號路徑刻意不變:沒有輪替就沒有 race,不必平白多一份憑證副本。"""
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        assert runtime.account_count() == 1
        assert runtime.rotate_client() is None
    assert [c.cred for c in built] == [_cred("1")]
    assert built[0].path is None, "單帳號仍走 env,不落檔"


async def test_gap_in_the_numbering_fails_loud(monkeypatch):
    """`_2` 缺號但 `_4` 存在 —— 打錯字的形狀。

    靜默跳過會讓一個付費帳號永遠不進 pool,而症狀只是「配額比預期早用完」,
    幾乎不可能回頭查到是 Doppler 打錯一個字。啟動時就爆掉。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_4", _cred("4"))
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_4"):
        async with app._lifespan(app.mcp):
            pass
    assert built == [], "驗證先於任何 client 建立"


async def test_missing_base_credential_with_extras_fails_loud(monkeypatch):
    """base 槽位缺席、`_2` 卻在 —— 最糟的失敗形狀,因為它**看起來好好的**。

    舊版在這裡 `return []`,而 lifespan 又用「base 在不在」判斷 inline_auth,於是
    整個 inline 模式一起被判成 False:`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 沒設
    (冷啟動那顆 RotateCookies poke 會作廢 3 VM 共用的 cookie)、
    `NOTEBOOKLM_HEADLESS_REAUTH` 保持生效、pool 等於關掉,而在有本機 storage_state
    的登入機上還會**啟動成功**、用舊身分跑完一整季。
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_2"):
        async with app._lifespan(app.mcp):
            pass
    assert built == []


async def test_no_credentials_at_all_is_still_the_local_storage_path(monkeypatch):
    """完全沒有 inline 憑證 = 登入機讀本機 storage_state,行為刻意不變。"""
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_HEADLESS_REAUTH", "1")
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage_no_env(built))

    async with app._lifespan(app.mcp):
        assert runtime.account_count() == 1
        # 非 inline 模式不壓 env override(登入機重鑄 cookie 寫得回檔案,是對的行為)。
        assert app.os.environ["NOTEBOOKLM_HEADLESS_REAUTH"] == "1"


async def test_inline_auth_suppresses_the_refresh_command(monkeypatch):
    """`NOTEBOOKLM_REFRESH_CMD` 在 inline(Doppler)模式必須被壓掉。

    設了它之後,SDK 的 token fetch 一撞認證錯誤就跑那支指令、然後呼叫**帶 recovery
    的** loader(`_auth/refresh.py:667` 的 `build_httpx_cookies_from_storage`)——
    等於在本 process 內重鑄一次 cookie,跟 keepalive poke / L3 headless re-auth 同一類
    災難,而且新 cookie 寫不回 Doppler。生產目前沒設,但 `docs/superpowers/specs/`
    的設計文件把它列為「Doppler 過期自癒」方案:誰照著做就打開這條路,所以顯式關掉,
    別讓紀律取決於別人的環境。退出後要**原樣還原**(不能順手吃掉使用者的設定)。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", "doppler-relogin")
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        assert "NOTEBOOKLM_REFRESH_CMD" not in app.os.environ
    assert app.os.environ["NOTEBOOKLM_REFRESH_CMD"] == "doppler-relogin"


@pytest.mark.parametrize(
    "name",
    [
        NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV,
        NOTEBOOKLM_HEADLESS_REAUTH_ENV,
        NOTEBOOKLM_REFRESH_CMD_ENV,
        NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV,
    ],
)
async def test_inline_auth_overrides_are_applied_and_restored(monkeypatch, name):
    """四個 inline override 都要真的進出 lifespan,不是只在 mapping 裡存在。"""
    sentinel = "pre-existing-value"
    override = app._INLINE_AUTH_ENV_OVERRIDES[name]
    assert override != sentinel

    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    monkeypatch.setenv(name, sentinel)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        if override is None:
            assert name not in app.os.environ
        else:
            assert app.os.environ[name] == override

    assert app.os.environ[name] == sentinel


def test_inline_auth_suppresses_mid_session_refresh_command():
    """綁上游公開常數,避免測試只拿 app 自己的字面 key 做同義反覆。"""
    assert set(app._INLINE_AUTH_ENV_OVERRIDES) >= {
        NOTEBOOKLM_REFRESH_CMD_ENV,
        NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV,
        NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV,
        NOTEBOOKLM_HEADLESS_REAUTH_ENV,
    }
    assert app._INLINE_AUTH_ENV_OVERRIDES[NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV] is None


async def test_from_storage_disables_headless_reauth_explicitly(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return _FakeClientCM()

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", fake)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))

    async with app._lifespan(app.mcp):
        pass
    assert len(calls) == 2
    assert all(call.get("allow_headless") is False for call in calls)
    assert all("path" in call for call in calls)

    calls.clear()
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2")
    async with app._lifespan(app.mcp):
        pass
    assert calls == [{"allow_headless": False}]


async def test_empty_base_credential_fails_loud(monkeypatch):
    """空字串檢查原本只做在 `_2` 以後,第 1 槽沒做 —— 教科書級的補一半。"""
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "   ")
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON 是空的"):
        async with app._lifespan(app.mcp):
            pass
    assert built == []


async def test_empty_extra_credential_fails_loud(monkeypatch):
    """空字串是「Doppler 有這個 key 但值沒設好」,不是「沒有這個帳號」。"""
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", "   ")
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_2 是空的"):
        async with app._lifespan(app.mcp):
            pass
    assert built == []


async def test_duplicate_accounts_fail_loud(monkeypatch):
    """兩個槽位輪到同一個帳號 —— 配額沒有變多,而 failover 會寫一筆謊報的 rotation。

    ADR-0010 自己點名的 `dev_alt` 陷阱(Doppler branch config 繼承 root 未覆寫的
    `_2`/`_3`)此前沒有任何機器檢查:`rotate_client()` 照樣回報「換到第二格」,
    manifest 照樣寫 `a@x → a@x`,而配額當然照樣是滿的 —— 看起來像 pool 壞掉。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    built = []
    monkeypatch.setattr(
        app.NotebookLMClient,
        "from_storage",
        _fake_from_storage(built, emails=["dup@x.com", "dup@x.com"]),
    )

    with pytest.raises(RuntimeError, match="dup@x.com"):
        async with app._lifespan(app.mcp):
            pass
    assert all(c.closed for c in built), "擋下來也要把已經建好的 client 關掉"


async def test_distinct_accounts_are_accepted(monkeypatch):
    """突變防護:重複檢查不能把正常的兩個帳號也擋掉。"""
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    built = []
    monkeypatch.setattr(
        app.NotebookLMClient,
        "from_storage",
        _fake_from_storage(built, emails=["a@x.com", "b@x.com"]),
    )

    async with app._lifespan(app.mcp):
        assert runtime.all_accounts() == ["a@x.com", "b@x.com"]


async def test_label_lookup_failure_says_why(monkeypatch, caplog):
    """`get_account_email()` 是啟動時唯一的真 RPC;失敗時**不 raise,但要留下原因**。

    退回 `#N` 是對的(一個死憑證讓整台 server 起不來更糟),但原本連 log 都沒有
    —— 而 `tools_basic._pool_peers` 之後看到 `#N` 會 raise「請手動分享或重啟
    server」,訊息裡說不出真正壞在哪,因為根因在這裡被吃掉了。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON_2", raising=False)

    def failing(*args, **kwargs):
        client = _FakeClientCM(cred="x")
        client.email_exc = RuntimeError("cookie 已失效")
        return client

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", failing)
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        async with app._lifespan(app.mcp):
            assert runtime.all_accounts() == ["#1"], "不 raise —— 一個死憑證不該讓 server 起不來"
    assert "cookie 已失效" in caplog.text, "但退回 #N 的真正原因必須留在 log 裡"


async def test_empty_account_label_warns_before_fallback(caplog):
    client = _FakeClientCM()

    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        assert await app._account_label(client, 2) == "#2"

    assert "取不到 email" in caplog.text
    assert "#2" in caplog.text


def test_precheck_agrees_with_the_sdk_strict_loader(tmp_path):
    """`good` / `blank-psidts` 兩個 case 上,預驗證與 SDK strict loader 判定一致。

    真實 0.8.1 的 sanitizer 會把空值 row 整列丟掉,所以缺 key 與空值對上游而言都是
    同一種 raise;本地非空值檢查是 backstop。現在擋住 heal 的承重牆是
    `app._lifespan` 持有每個 pool 檔案的 rotation flock,
    `assert_usable_storage_state` 只負責必要 cookie 的存在與非空。這兩個 case 的
    等價前提仍由這條迴圈守著。

    expired-psidts 沒塞進同一個迴圈,因為 strict loader 使用 NAME_ONLY、明說 never
    fires a heal,不檢查過期;它的生產路徑差異由
    `test_precheck_rejects_expired_psidts_that_would_trigger_heal` 獨立守住。
    """
    cases = {"good": (_cred("1"), True), "blank-psidts": (_blank_psidts_cred(), False)}
    for tag, (cred, accepted) in cases.items():
        assert _strict_loader_accepts(cred, tmp_path / f"{tag}-strict.json") is accepted, (
            f"{tag}:SDK strict loader 的判定變了 —— 預驗證的等價前提要重新推導"
        )
        assert _precheck_accepts(cred, tmp_path / f"{tag}-precheck.json") is accepted, (
            f"{tag}:預驗證與 strict loader 分岔了 —— L2 recovery 那條路又打開了"
        )


def test_precheck_warns_but_accepts_expired_psidts_that_would_trigger_heal(
    tmp_path, caplog
):
    """routability 只代表能否 refresh,不代表這份憑證能否使用。"""
    from notebooklm_mcp import _cookies

    cred = _expired_unique_psidts_cred()
    assert _cookies.would_trigger_inline_heal(json.loads(cred)) is True

    path = tmp_path / "expired-precheck.json"
    with caplog.at_level(logging.WARNING, logger=app.logger.name):
        assert app._write_credential_file(cred, path, 1) == path

    assert path.read_text(encoding="utf-8") == cred
    assert "NOTEBOOKLM_AUTH_JSON" in caplog.text
    assert "unique-secret-psidts-marker-should-not-leak" not in caplog.text


async def test_pool_rotation_flock_blocks_inline_heal(monkeypatch, tmp_path):
    """pool 持有 rotation flock 時,生產 loader 不應進入真正的 rotation POST。"""
    from notebooklm._auth import psidts_recovery
    from notebooklm._auth.cookies import build_httpx_cookies_from_storage

    cred = _expired_psidts_cred()
    rotation_calls = []
    monkeypatch.setattr(
        psidts_recovery,
        "_attempt_rotation",
        lambda path, entries: rotation_calls.append(path) or False,
    )

    unlocked_path = tmp_path / "unlocked.json"
    unlocked_path.write_text(cred, encoding="utf-8")
    assert build_httpx_cookies_from_storage(unlocked_path) is not None
    assert rotation_calls == [unlocked_path]

    rotation_calls.clear()
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", cred)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", cred)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        slot_path = built[0].path
        assert slot_path is not None
        assert build_httpx_cookies_from_storage(slot_path) is not None

    assert rotation_calls == []


async def test_pool_rotation_flock_blocks_heal_even_when_os_flock_is_unavailable(
    monkeypatch,
):
    """OS flock 回 UNAVAILABLE 時,in-process 鎖仍須擋住同 process 的 heal。

    `_file_lock_try_exclusive` 對 UNAVAILABLE 刻意 fail-open,所以 True 本身不代表
    OS 層真的持有鎖；這裡的承重牆是 `StorageLockManager._acquire_once` 一開始搶下
    的 per-path `threading.Lock`,而且整個 `with` block 期間都持有。app 的 outer
    context 若已持有同一 path,SDK 的 inner context 會在碰 OS 層前得到 CONTENDED,
    `_attempt_rotation` 因而不會被呼叫。這條測試紅了代表 in-process 鎖的語義變了;
    OS lock infra 不可用時就會漏 heal,應改成直接讀 `keepalive._file_lock` 的
    `LockState`,只有 HELD 才算數。雙槽位版本的對照由
    `test_pool_rotation_flock_blocks_each_slot_independently` 守住。
    """
    from notebooklm._auth import psidts_recovery, storage_lock
    from notebooklm._auth.cookies import build_httpx_cookies_from_storage

    monkeypatch.setattr(
        storage_lock._PlatformLockGateway,
        "acquire",
        lambda self, fd, *, blocking, operation: storage_lock.LockState.UNAVAILABLE,
    )
    rotation_calls = []
    monkeypatch.setattr(
        psidts_recovery,
        "_attempt_rotation",
        lambda path, entries: rotation_calls.append(path) or False,
    )
    cred = _expired_psidts_cred()
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", cred)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", cred)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        slot_path = built[0].path
        assert slot_path is not None
        assert build_httpx_cookies_from_storage(slot_path) is not None

    assert rotation_calls == []


async def test_pool_rotation_flock_blocks_each_slot_independently(monkeypatch, tmp_path):
    """每個槽位各自擋自己的 heal,不同 lock path 是設計而不是缺陷。

    pool 的兩份憑證檔各有自己的 rotation lock,因為這裡與每個 process 的生產語義
    一樣,目標是讓每個載入憑證的執行個體不在本 process 內 rotate,不是跨槽位或跨
    process 協調 rotation 本身。結束 lifespan 後的對照組只鎖其中一份獨立憑證;
    未鎖那份必須呼叫 `_attempt_rotation`,證明 pool 內兩次都是被各自的鎖擋下,不是
    過期 PSIDTS 根本不會觸發 heal。

    **這條紅了不代表 in-process 鎖出事**(那句話是從上面那條測試複製過來的,實測是錯的:
    把上游 `_inprocess_lock_for` 改成每次回新鎖之後,這條仍然綠——真 flock 在同一個
    process 的兩個 open file description 之間照樣衝突)。它紅的三種可能是:
    ①`_lifespan` 不再持有每個槽位的鎖;②槽位的 lock path 塌成同一個(那會讓槽位互相
    擋到);③OS flock 不再跨 OFD 衝突。
    UNAVAILABLE(OS lock infra 不可用時 fail-open)那條承重牆由
    `test_pool_rotation_flock_blocks_heal_even_when_os_flock_is_unavailable` 單獨守住。
    """
    from notebooklm._auth import keepalive, psidts_recovery
    from notebooklm._auth.cookies import build_httpx_cookies_from_storage

    rotation_calls = []
    monkeypatch.setattr(
        psidts_recovery,
        "_attempt_rotation",
        lambda path, entries: rotation_calls.append(path) or False,
    )
    cred = _expired_psidts_cred()
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", cred)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", cred)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        paths = [client.path for client in built]
        assert paths[0] is not None and paths[1] is not None
        assert paths[0] != paths[1]
        for path in paths:
            assert build_httpx_cookies_from_storage(path) is not None
        assert rotation_calls == []

    rotation_calls.clear()
    locked_path = tmp_path / "locked.json"
    unlocked_path = tmp_path / "unlocked.json"
    locked_path.write_text(cred, encoding="utf-8")
    unlocked_path.write_text(cred, encoding="utf-8")
    lock_path = psidts_recovery._rotation_lock_path(locked_path)
    assert lock_path is not None
    with keepalive._file_lock_try_exclusive(lock_path) as acquired:
        assert acquired
        for path in (locked_path, unlocked_path):
            assert build_httpx_cookies_from_storage(path) is not None

    assert rotation_calls == [unlocked_path]


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(
            json.dumps({"cookies": [{"name": "SID", "value": "x", "domain": ".google.com"}]}),
            id="cookie-absent",
        ),
        pytest.param(_blank_psidts_cred(), id="cookie-present-but-blank"),
    ],
)
async def test_malformed_credential_fails_before_any_client_is_built(monkeypatch, broken):
    """必要 cookie **缺席或值是空的** ⇒ 落檔前就 raise,而不是交給 SDK 去「治好」。

    給了 storage path 之後,SDK 的 L2 inline PSIDTS recovery 會重新武裝
    (`_auth/psidts_recovery._resolve_recovery_path`:env 模式回 None 而拒絕,
    有 path 就接受),而它**不受 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**,會在本
    process 內發一次 RotateCookies —— 正是 AGENTS.md 禁止的「在本 process 內重鑄
    cookie」。它唯一的入口是 strict loader 丟 ValueError 那條 except,所以在這裡先
    用**同樣的條件**驗過,那條路就到不了。

    兩個 id 是兩半:此前只鎖了 `cookie-absent`,而真正漏掉的是
    `cookie-present-but-blank`(舊預驗證不看 value,整條 recovery 照樣打得開)。
    共通的不變式只有一條 —— 缺憑證是啟動時的大聲失敗,不會被靜默吸收
    (ADR-0010 §Transparency)。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", broken)
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_2 不是可用的 storage_state"):
        async with app._lifespan(app.mcp):
            pass


async def test_every_client_is_closed_on_normal_shutdown(monkeypatch):
    """正常關閉時 N 個 client 都要被關掉。

    這件事此前完全沒鎖:在 `set_clients` 前插一行 `stack.pop_all()`(= 所有 client
    永不關閉)整套測試照樣全綠。失敗路徑有測試、成功路徑沒有 —— 又一個補一半。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_3", _cred("3"))
    built = []
    monkeypatch.setattr(app.NotebookLMClient, "from_storage", _fake_from_storage(built))

    async with app._lifespan(app.mcp):
        assert [c.closed for c in built] == [False, False, False]
    assert [c.closed for c in built] == [True, True, True]
    assert not built[0].path.parent.exists(), "憑證目錄也要跟著收掉"


async def test_pool_is_torn_down_even_if_a_later_client_fails(monkeypatch):
    """第 2 個 client 建到一半炸掉,第 1 個已經 enter 的必須被關掉。

    否則 stdio server 啟動失敗會留下一條沒關的 HTTP 連線,而失敗路徑正是
    最不會被人盯著看的地方。憑證檔同理:失敗路徑也要刪乾淨。
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", _cred("1"))
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON_2", _cred("2"))
    built = []

    def fake(*args, path=None, **kwargs):
        if Path(path).read_text(encoding="utf-8") == _cred("2"):
            raise ValueError("boom")
        client = _FakeClientCM(cred=_cred("1"), path=Path(path))
        built.append(client)
        return client

    monkeypatch.setattr(app.NotebookLMClient, "from_storage", fake)

    # v0.9.14 FINDING-B:建 client 失敗一定要講出是**哪一個槽位**(原本 SDK 的
    # `_LoginRedirectError` 原樣穿透,9 槽 pool 裡看不出要去重登哪一個帳號),
    # 所以這裡改成 RuntimeError + 槽位名;原因用 `from exc` 保留在 __cause__。
    with pytest.raises(RuntimeError, match="NOTEBOOKLM_AUTH_JSON_2") as excinfo:
        async with app._lifespan(app.mcp):
            pass
    assert "boom" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert [c.closed for c in built] == [True]
    assert not built[0].path.parent.exists()
    # 失敗也要還原 env,別把中間狀態留給下一段程式。
    assert app.os.environ["NOTEBOOKLM_AUTH_JSON"] == _cred("1")
