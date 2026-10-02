"""Canonical FastMCP app + tool registration.

This lives in its OWN module (never run as ``__main__``) so there is exactly
ONE ``mcp`` instance regardless of how the server is launched. The earlier
design put this in ``server.py`` and registered tools via ``from .server import
mcp`` — but ``python -m notebooklm_mcp.server`` loads server as ``__main__`` AND
re-imports it as ``notebooklm_mcp.server``, creating TWO ``mcp`` objects: tools
registered on one, ``run()`` served the other (zero tools exposed over the MCP
protocol). Importing the app from a dedicated module avoids that duplication.

Owns one long-lived NotebookLMClient for each MCP process. Auth comes from
NOTEBOOKLM_AUTH_JSON (typically Doppler).
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from notebooklm import NotebookLMClient
from notebooklm._auth.keepalive import _file_lock_try_exclusive
from notebooklm._auth.psidts_recovery import _rotation_lock_path

from . import runtime
from ._cookies import (
    assert_usable_storage_state,
    describe_inline_heal_reason,
    heal_warning_detail,
    psidts_domains,
    would_trigger_inline_heal,
)

logger = logging.getLogger(__name__)
_LIFESPAN_LOCK = threading.Lock()

_AUTH_JSON_ENV = "NOTEBOOKLM_AUTH_JSON"
_DISABLE_KEEPALIVE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"
_HEADLESS_REAUTH_ENV = "NOTEBOOKLM_HEADLESS_REAUTH"
_REFRESH_CMD_ENV = "NOTEBOOKLM_REFRESH_CMD"
_REFRESH_CMD_MIDSESSION_ENV = "NOTEBOOKLM_REFRESH_CMD_MIDSESSION"
_BACKEND_ENV = "NOTEBOOKLM_BACKEND"

# inline auth(Doppler 注入 NOTEBOOKLM_AUTH_JSON)期間強制成這樣;None = 刪掉該變數。
# 共同理由:**任何會在本 process 內重鑄 cookie 的機制,在 inline 模式都是淨損失**——
# 新 cookie 只活在記憶體、寫不回 Doppler,下一個 stdio process 反而拿舊的啟動,
# 而重鑄本身還會把 3 VM 共用的那份作廢。
#   - DISABLE_KEEPALIVE_POKE=1:擋掉 from_storage() 冷啟動的 RotateCookies poke。
#   - HEADLESS_REAUTH 刪掉:0.8.0 新增的 L3「無頭重新認證」(用持久瀏覽器 profile
#     靜默重鑄 cookie)。它預設就是關的,但只要環境裡有人設了 =1 就會在 RPC 中途
#     自動觸發 —— 顯式壓掉,別讓紀律取決於別人的環境。VM 上也根本沒有那個 profile。
#   - REFRESH_CMD 刪掉:設了它之後,SDK 的 token fetch 一撞認證錯誤就跑那支指令,
#     然後呼叫**帶 recovery 的** loader(`_auth/refresh.py:667` 的
#     `build_httpx_cookies_from_storage`,`_should_try_refresh` 只看這個變數有沒有值)
#     —— 同一類「本 process 內重鑄」。生產目前沒設,但 `docs/superpowers/specs/` 的
#     設計文件把它列為「Doppler 過期自癒」方案,誰照著做就打開這條路。
#   - REFRESH_CMD_MIDSESSION 刪掉:0.8.1 新增的 L2.5「session 中途跑 refresh cmd」開關。
#     但 `NOTEBOOKLM_REFRESH_CMD` 已經刪掉,沒有 cmd 就沒有東西可跑,所以這是防禦深度,
#     不是修現存漏洞；仍顯式壓掉,別讓紀律取決於別人的環境。
# 這些 override 都只在 inline 模式壓:登入機讀本機 storage_state 時,重鑄後寫得回檔案,
# 是對的行為。
#
# BACKEND 刪掉的理由**不同**(不是 cookie 重鑄,所以另列):0.8.2 起
# `from_storage()` 在沒傳 `backend=` 時會讀 `NOTEBOOKLM_BACKEND`,`"android"` 會讓整個
# client 換成 master-token + gRPC 的那套 namespace。我們的憑證是 cookie snapshot
# (`NOTEBOOKLM_AUTH_JSON`),換過去只會用一個我們沒有的憑證去打一個沒驗過的傳輸層,而
# **本 repo 的 contract 測試全部釘在 `_web.*` 上、對這件事一個字都證明不了**(見
# `tests/test_contracts.py::test_default_backend_is_still_web`,它驗的是 `env=None` 的
# 預設值,看不見環境裡真的有人設了)。要走 android 是一次帶驗收的決策,不是誰的
# shell 或 Doppler 專案裡多一個變數就發生。
_INLINE_AUTH_ENV_OVERRIDES: dict[str, str | None] = {
    _DISABLE_KEEPALIVE_ENV: "1",
    _HEADLESS_REAUTH_ENV: None,
    _REFRESH_CMD_ENV: None,
    _REFRESH_CMD_MIDSESSION_ENV: None,
    _BACKEND_ENV: None,
}


# pool 掃描的上限。只是「打錯字偵測」的搜尋範圍,不是帳號數上限的產品決策。
_MAX_POOL_SLOTS = 20


def _is_loopback_host(host: str) -> bool:
    """只接受明確的 localhost 或 loopback IP；不做可能被 DNS 改寫的解析。"""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _slot_env_name(slot: int) -> str:
    """第 1 槽是不帶後綴的那個(SDK 只認它);之後才有 `_2`/`_3`…。"""
    return _AUTH_JSON_ENV if slot == 1 else f"{_AUTH_JSON_ENV}_{slot}"


def _require_credential(slot: int, value: str) -> str:
    """空字串 = 「Doppler 有這個 key 但值沒設好」,不是「沒有這個帳號」。

    這個檢查原本只做在 `_2` 以後,第 1 槽沒做 —— 教科書級的補一半。兩處共用一個
    檢查,新增槽位時不會再漏。
    """
    if not value.strip():
        raise RuntimeError(f"{_slot_env_name(slot)} 是空的——憑證沒設好,不是「沒有這個帳號」")
    return value


def _reject_orphan_slots(env: dict[str, str] | os._Environ, *, next_slot: int) -> None:
    """`next_slot` 之後還有槽位存在 = 中間缺號,Doppler 打錯一個字的形狀。

    靜默跳過的話那個付費帳號永遠不進 pool,而症狀只是「配額比預期早用完」——
    幾乎不可能回頭查到根因。`next_slot=1` 是「連不帶後綴的那個都沒有」。
    """
    missing = _slot_env_name(next_slot)
    for higher in range(next_slot + 1, _MAX_POOL_SLOTS + 1):
        if env.get(f"{_AUTH_JSON_ENV}_{higher}") is not None:
            raise RuntimeError(
                f"{_AUTH_JSON_ENV}_{higher} 存在,但編號在 {missing} 就斷了。"
                f"補上 {missing} 或把編號接連續——靜默跳過會讓那個帳號永遠不進 pool"
            )


def _pooled_auth_json(env: dict[str, str] | os._Environ) -> list[str]:
    """依序取出 pool 的憑證:`NOTEBOOKLM_AUTH_JSON`, `_2`, `_3`…(ADR-0010)。

    純函式、零副作用,所以 lifespan 可以在動任何 env 之前先跑完整驗證。
    """
    first = env.get(_AUTH_JSON_ENV)
    if first is None:
        # base 缺、`_2`/`_3` 卻在 —— 舊版在這裡直接 `return []`,而 lifespan 又是用
        # 「base 在不在」判斷 inline_auth,於是連 inline 模式一起被判成 False:
        # env override 沒設(`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 缺席 ⇒ 冷啟動那顆
        # RotateCookies poke 會作廢 3 VM 共用的 cookie;`NOTEBOOKLM_HEADLESS_REAUTH`
        # 保持生效),pool 等於關掉,而在有本機 storage_state 的登入機上還會**啟動
        # 成功**、用舊身分跑。最糟的失敗形狀:看起來好好的。
        _reject_orphan_slots(env, next_slot=1)
        return []
    creds = [_require_credential(1, first)]
    slot = 2
    while (value := env.get(f"{_AUTH_JSON_ENV}_{slot}")) is not None:
        creds.append(_require_credential(slot, value))
        slot += 1
    _reject_orphan_slots(env, next_slot=slot)
    return creds


def _write_credential_file(cred: str, path: Path, slot: int) -> Path:
    """把一份 pool 憑證寫成 0600 的 storage_state 檔,回傳路徑。

    **為什麼值得讓憑證落檔**(此前刻意不落檔,是 v0.8.x pool 實作在這裡的註解自己
    列出的優點 —— ADR-0010 全文沒有這句,別再往上引錯出處):
    SDK 的媒體下載在下載當下重讀 `NOTEBOOKLM_AUTH_JSON`(`_artifact/downloads.py`
    的 `self._cookie_loader(self._storage_path)`,`_storage_path is None` 才回頭讀
    env),而 MCP 是**並行**的 —— 一個 process、一份全域 env、N 個 client。EP05 正在
    finalize(client 已經 pin 住)時 EP06 撞配額 rotate,EP05 的下載就以別人的身分
    發出。把身分放進 process 全域變數,在並行 server 裡結構上就是錯的:**沒有任何
    加鎖方式能讓一個全域槽同時是兩個值**。`from_storage(path=…)` 建構時把路徑注入
    download service,身分於是跟著 client 走,race 從根上消失。
    代價是憑證在 server 生命週期內存在於 0700 目錄下的 0600 檔案(lifespan 結束、
    含例外路徑,整個目錄刪除;**AsyncExitStack 沒展開完的路徑不保證**,見下方
    `stack.callback` 那裡的三條註解)——同機器上的同一個 user 讀得到。**那個 user
    本來就讀得到 `/proc/<pid>/environ`**,所以攻擊面沒有實際擴大。

    落檔前先驗一次憑證形狀,不只是為了早點爆:給了 path 之後,SDK 的 **L2 inline
    PSIDTS recovery**(`_auth/psidts_recovery.py` 的 `_resolve_recovery_path`:env 模式
    回 None 而拒絕、有 path 就接受)會重新武裝,而它**不受
    `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**,可能在本 process 內發一次 RotateCookies。
    0.8.1 的生產路徑即使 strict loader 使用 NAME_ONLY 而接受過期 PSIDTS,heal 自己的
    routability predicate 仍會把它判成可觸發 RotateCookies;因此這裡保留可見 warning,
    並由 `_lifespan` 持有 rotation flock 擋住 POST。這樣「不在本 process 內重鑄 cookie」
    的紀律維持不變,而缺憑證仍在啟動時大聲失敗,不可 refresh 的憑證則可見但不會弄掛
    server。

    **0.8.1 的實際語義是**:`extract_cookies_from_storage` 的 sanitizer 在解析階段
    就把空字串 value 的 cookie row 整列丟棄,所以缺 key 與空值對上游而言都是同一種
    `ValueError`;本地必要 cookie 非空檢查現在是 backstop,用來擋繞過 sanitizer 的
    呼叫端與上游未來改回放行空值的情況。routability 問的是能不能 refresh,不是能不
    能用,所以 `would_trigger_inline_heal` 只發 warning;真正擋住 heal 的承重牆是
    `_lifespan` 的 rotation flock。憑證過期或 scope 錯本來就該更新 Doppler,warning
    會留下根因,但不會讓一個壞槽位弄掛整台 server。
    `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 現在
    只守著指定 good / blank case 的等價前提;過期情境由同檔的獨立測試守著。

    **判準本身住在 `_cookies.assert_usable_storage_state`,`auth_cli` 走同一支。**
    那條 tripwire 只認得這裡的呼叫路徑,所以 CLI 自己抄一份的話它照不到 —— 上游改語義
    時只有 pool 這邊會被改到,而 CLI 正是「認證已經壞掉」時才會用到的救援工具。
    """
    name = _slot_env_name(slot)
    try:
        # 判準本身在 `_cookies.assert_usable_storage_state` —— `auth_cli` 走同一支,
        # 上面那段等價論證才不會只在其中一邊被維護(見該 module 的 docstring)。
        storage_state = json.loads(cred)
        assert_usable_storage_state(storage_state)
        if would_trigger_inline_heal(storage_state):
            logger.warning("%s %s", name, heal_warning_detail(storage_state))
    except Exception as exc:  # ValueError(JSON / 缺 cookie / 空值)、型別不對…一律具名重拋
        raise RuntimeError(f"{name} 不是可用的 storage_state:{type(exc).__name__}: {exc}") from exc
    # O_EXCL + 0600:目錄是 mkdtemp 建的(0700),但檔案模式要自己指定,
    # 別讓 umask 決定憑證的權限。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(cred)
    return path


def _reject_duplicate_accounts(pool: list[tuple[str, object]]) -> None:
    """兩個槽位輪到同一個帳號 —— 配額沒有變多,而 failover 會寫一筆謊報的紀錄。

    ADR-0010 自己點名的 `dev_alt` 陷阱(Doppler branch config 會繼承 root 未覆寫的
    secret,於是 root 的 `_2`/`_3` 被一起繼承進來)此前**沒有任何機器檢查**:
    `rotate_client()` 照樣回報「換到第二格」,`_record_dispatch_failover` 照樣寫
    `from_account` → `to_account`,而實際上兩邊是同一個帳號,配額當然照樣是滿的。

    **偵測的前提是拿得到 email**:`_account_label` 取不到就退回 `#N`,而 `#1`/`#2`
    天然不相撞 —— 兩個槽位都退不回真名時這道檢查靜默失效,也就是說**保護正好在認證
    退化時消失**,而那正是最可能同時發生的情境(同一份憑證被複製到兩個槽、又一起過期)。
    刻意不補「拿不到 email 就 raise」:一個死憑證不該讓整台 server 起不來
    (理由見 `_account_label`)。那個情況的兜底是那裡的 warning,不是這裡。
    """
    labels = [label for label, _ in pool]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise RuntimeError(
            f"pool 裡有重複帳號 {duplicates}:兩個槽位是同一個帳號,配額沒有變多,"
            "而 failover 會寫下一筆謊報的 rotation。最常見成因是 Doppler branch "
            f"config 繼承了 root 未覆寫的 {_AUTH_JSON_ENV}_N(ADR-0010 的 dev_alt 陷阱)"
        )


async def _account_label(client: object, slot: int) -> str:
    """稽核用的帳號標籤。拿不到 email 不該讓 server 起不來,退回槽位編號。

    **刻意不 raise**:一個死憑證讓整台 server 起不來更糟。但也**不能吞得無聲無息**
    —— 這是啟動時唯一的真 RPC,而 `tools_basic._pool_peers` 之後看到 `#N` 會 raise
    「請手動分享或重啟 server」,訊息裡說不出真正原因,因為原因在這裡被吃掉了。
    stdio transport 下 stderr 不影響協定,warning 直接寫出去。
    """
    try:
        email = await client.get_account_email()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 —— 取不到 email 只退回槽位編號,不讓啟動失敗
        logger.warning(
            "第 %d 個帳號取不到 email,退回槽位編號 #%d(pool 的稽核標籤會少一個真名,"
            "分享/對帳工具會據此要求人工介入):%s: %s",
            slot,
            slot,
            type(exc).__name__,
            exc,
        )
        return f"#{slot}"
    if not email:
        logger.warning(
            "第 %d 個帳號取不到 email,退回槽位編號 #%d(pool 的稽核標籤會少一個真名):"
            "get_account_email 回傳空值",
            slot,
            slot,
        )
        return f"#{slot}"
    return email


def _slot_diagnostic(slot: int, cred: str | None) -> dict[str, object]:
    """一個槽位的**非機密**啟動診斷,給 `auth_check(all_slots=True)` 事後回報用。

    `refreshable` 與啟動 warning **量的是同一個 predicate**,這是重點:兩盞燈不同源的話,
    人看到的狀態與 log 說的會各講各的,而那正是這輪要修掉的問題。

    `cred` 是 None 代表非 inline(本機 storage_state 檔),我們手上沒有那份 JSON ——
    誠實回 `None` 表示「沒量」,不要拿 True/False 假裝量過。

    ⚠️ **整段包在 try 裡,因為這是觀測層,不該有能力弄掛被觀測的東西。** 走到這裡時
    憑證其實已經通過 `_write_credential_file` / `from_storage`,理論上解得開;但這支的
    唯一產出是一則診斷,而它若拋例外就會**把整台 server 的啟動變成失敗** —— 拿「看儀表」
    去換「引擎發不動」是絕對划不來的交易。量不到就誠實回 `None`(= 沒量),與非 inline
    路徑同一種語意。
    """
    row: dict[str, object] = {"slot": slot, "env": _slot_env_name(slot)}
    if cred is None:
        row.update(refreshable=None, heal_reason=None, psidts_domains=[])
        return row
    try:
        storage_state = json.loads(cred)
        reason = describe_inline_heal_reason(storage_state)
        row.update(
            refreshable=not reason,
            heal_reason=reason or None,
            psidts_domains=psidts_domains(storage_state),
        )
    except Exception as exc:  # noqa: BLE001 —— 診斷失敗只降級成「沒量」,不影響啟動
        logger.debug("%s 的啟動診斷算不出來(不影響服役):%r", row["env"], exc)
        row.update(refreshable=None, heal_reason=None, psidts_domains=[])
    return row


@contextlib.asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
    # ponytail:全域 pool 一次只屬於一個 session；要支援多 client 才搬進 session context。
    if not _LIFESPAN_LOCK.acquire(blocking=False):
        raise RuntimeError("另一個 MCP session 正在使用此 server；HTTP 模式一次只能連一個 client")
    try:
        # httpx 的 INFO 會把 Google 登入轉址 URL 的 osidt 等憑證參數印進 stderr。
        logging.getLogger("httpx").setLevel(logging.WARNING)
        async with _lifespan_client_pool(_app):
            yield
    finally:
        _LIFESPAN_LOCK.release()


@contextlib.asynccontextmanager
async def _lifespan_client_pool(_app: FastMCP) -> AsyncIterator[None]:
    # notebooklm-py 0.8.x:from_storage() 是同步函式,回傳可直接 async with 的
    # context(0.4.x「coroutine 必須 await」慣用法已走入歷史)。
    # MCP 一律**不傳 keepalive=**,再加上下面這組 env override(理由見上)。
    #
    # 驗證先於任何副作用:`_pooled_auth_json` 是純函式,所以打錯字在動 env、建 client
    # 之前就爆。`inline_auth` 也從它推導 —— 舊版用「base env 在不在」判斷,base 缺但
    # `_N` 在時會靜默退成非 inline 模式(見 `_pooled_auth_json` 的註解)。
    creds = _pooled_auth_json(os.environ)
    inline_auth = bool(creds)
    saved = {name: os.environ.get(name) for name in _INLINE_AUTH_ENV_OVERRIDES}
    if inline_auth:
        for name, value in _INLINE_AUTH_ENV_OVERRIDES.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    try:
        async with contextlib.AsyncExitStack() as stack:
            pool: list[tuple[str, object]] = []
            diagnostics: list[dict[str, object]] = []
            if len(creds) > 1:
                # 多帳號:每個槽位各自一份 storage_state 檔,client 自己帶著身分走。
                # **env 完全不動** —— 它不再是身分,只是 Doppler 注入的原始輸入。
                # (單帳號不走這條:沒有輪替就沒有 race,而 env 本來就等於那唯一的
                #  帳號,改成落檔只會平白多一份憑證副本與一組新的 SDK 行為。)
                cred_dir = Path(tempfile.mkdtemp(prefix="notebooklm-mcp-auth-"))
                # 先註冊 → LIFO 展開時最後才跑:client 關閉時 SDK 可能回寫 cookie,
                # 目錄要活到那之後。例外路徑也走同一條 stack,不會漏刪。
                # 兩個講出來的取捨,別讀成「憑證一定被刪掉」:
                #   - `ignore_errors=True`:刪不掉時無聲跳過(換來的是「清理失敗不會蓋掉
                #     真正該讀的那個例外」,同 `_account_label` 的取捨)。殘留的是 $TMPDIR
                #     下一個目錄,同 user 本來就讀得到 `/proc/<pid>/environ`。
                #   - SIGTERM 不展開 AsyncExitStack,憑證會原封留在 $TMPDIR 直到重開機。
                #   - **streamable-http 被 Ctrl-C(只有子 process 收到 SIGINT)時,rmtree
                #     跑完之後目錄還會被「重建」**:uvicorn 的 `capture_signals` 離場時
                #     `raise_signal` 打斷 AsyncExitStack 展開,遲到的 cookie 回寫走
                #     `filelock/_util.py` 的 `parent.mkdir(...)` —— 那個 mkdir 走 umask,
                #     所以留下的是 0775 空目錄(不是 mkdtemp 的 0700)。**不是外洩**:
                #     憑證檔一律 0600(SDK `_atomic_io` 的 `fchmod`),與目錄模式無關。
                #     v0.9.13 驗收也量到更壞的交錯:rmtree 刪到一半被打斷,留下真憑證。
                #     **送 SIGINT 給整個 process group 也救不了**(發 tag 後重跑 Phase 1
                #     實測:兩台 HTTP server 都用 group SIGINT 停,仍留下 4 個目錄,其中
                #     一個含一份 slot-*.json)—— 一開始以為那是乾淨路徑,是樣本數 1 的
                #     錯覺。**stdio 正常結束(stdin EOF)才是唯一實測可靠的那條**;
                #     HTTP 模式停掉之後要自己檢查 `$TMPDIR/notebooklm-mcp-auth-*`。
                #     (順帶:streamable-http 的 lifespan 是 **per-session**,一個 process
                #      會建出多個憑證目錄,清的時候別只找一個。)
                # **不要改成「啟動時掃掉舊目錄」** —— 同機並行的 MCP process 會互刪。
                stack.callback(shutil.rmtree, cred_dir, ignore_errors=True)
                for slot, cred in enumerate(creds, start=1):
                    path = _write_credential_file(cred, cred_dir / f"slot-{slot}.json", slot)
                    # 上游前提 4 把這把 lock 定義成「另一個 process 正在 rotation」;
                    # 我們持有它是宣告這個槽位檔的 rotation 由外部 Doppler 負責。
                    # 這不是 hack:這正是上游 heal 用來決定是否能發 POST 的協調語義,
                    # 而且鎖會跟著 AsyncExitStack 持有到 client 關閉之後才釋放。
                    # 它同時擋過期與 scope 錯兩種觸發原因;真實 Doppler prd 的 slot 1
                    # 曾是 `.youtube.com`,送不到 `accounts.google.com`,只看 expires
                    # 擋不了這種 scope 錯。
                    # 這裡不是在跨 process 協調 rotation,而是讓每個 process 各自不
                    # rotate；每個 process 只需擋住載入自己這份憑證的 heal。即使
                    # `_file_lock_try_exclusive` 因 OS flock 不可用而 fail-open 回 True,
                    # 承重的仍是 `storage_lock.StorageLockManager._acquire_once` 一開始
                    # 搶下、並持有整個 `with` block 的 per-path in-process
                    # `threading.Lock`,不是 OS 層協調。P1/P2 的語義由
                    # `test_pool_rotation_flock_blocks_each_slot_independently` 與
                    # `test_pool_rotation_flock_blocks_heal_even_when_os_flock_is_unavailable`
                    # 交叉引用。**若紅了,先確認下面這段持鎖還在**(拿掉它兩條都會紅,
                    # 而那是編輯這一塊的人最可能造成的原因);持鎖還在才代表 in-process
                    # 鎖的語義變了,那時要改成直接讀 `keepalive._file_lock` 的
                    # `LockState`,只有 `HELD` 才算數。
                    lock_path = _rotation_lock_path(path)
                    if lock_path is not None:
                        acquired = stack.enter_context(_file_lock_try_exclusive(lock_path))
                        if not acquired:
                            logger.warning(
                                "%s 的 rotation flock 無法取得;另一個 process 可能正在"
                                "處理同一份私有憑證檔。",
                                _slot_env_name(slot),
                            )
                    # 0.8.1 將 L3 護欄升為建構參數;與上面刪除
                    # `NOTEBOOKLM_HEADLESS_REAUTH` 的 env 護欄互補,兩層都保留。
                    #
                    # 失敗一定要講出是**哪一個槽位**(v0.9.14 真實驗收 FINDING-B):
                    # `_write_credential_file` 那條(結構不合法)本來就會指名,但這條
                    # ——「結構合法、cookie 已死」——原本讓 SDK 的 `_LoginRedirectError`
                    # 原樣穿透,訊息只有「Authentication expired or invalid. Run
                    # 'notebooklm login'」。9 槽 pool 裡任何一槽過期就整台起不來,
                    # 而人看不出要去重登哪一個帳號。實測就是這個形狀。
                    try:
                        client = await stack.enter_async_context(
                            # backend="web" 顯式傳,不靠 _INLINE_AUTH_ENV_OVERRIDES 刪
                            # env 那條路(理由見上方 _BACKEND_ENV 的註解):shell 裡若有
                            # NOTEBOOKLM_BACKEND=android,SDK docstring 講明 explicit 勝
                            # 過 env,只有這裡沒傳過才會被讀到。
                            NotebookLMClient.from_storage(
                                path=str(path), allow_headless=False, backend="web"
                            )
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"{_slot_env_name(slot)} 的憑證建不出 client:{exc}"
                        ) from exc
                    pool.append((await _account_label(client, slot), client))
                    diagnostics.append(_slot_diagnostic(slot, cred))
            else:
                # 單帳號 inline 不需要這把 flock:`_resolve_recovery_path` 先看 path,
                # 再看 `resolve_auth_json_env()`。這條分支不傳 path,而 inline_auth 為真
                # 時 env 一定存在,所以 resolver 回 None,上游 heal 直接 decline。
                # 沒有 inline_auth 時則是本機 storage_state,rotation 寫回本機檔案即可。
                # 顯式關閉 0.8.1 的 L3 headless re-auth;也與刪除同名 env 的護欄互補。
                client = await stack.enter_async_context(
                    # 這條分支涵蓋登入機的本機 storage_state,不受 inline auth 的 env
                    # override 保護(_INLINE_AUTH_ENV_OVERRIDES 只在 inline_auth 為真時
                    # 生效)——顯式傳 backend="web" 才不會被 shell 裡的
                    # NOTEBOOKLM_BACKEND=android 讀走,同上面多帳號分支同一個理由。
                    NotebookLMClient.from_storage(allow_headless=False, backend="web")
                )
                pool.append((await _account_label(client, 1), client))
                diagnostics.append(_slot_diagnostic(1, creds[0] if creds else None))
            _reject_duplicate_accounts(pool)
            runtime.set_clients(pool)
            # 一定要在 set_clients 之後:它會清空診斷(換 pool = 換一輪)。
            runtime.set_slot_diagnostics(diagnostics)
            try:
                yield
            finally:
                runtime.set_client(None)
    finally:
        if inline_auth:
            for name, old in saved.items():
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old


# Protocol-level server instructions: surfaced to ANY MCP client (even one
# without our external skill). Deliberately a SKELETON — workflow order + env
# vars + the few killer gotchas — with detail delegated to the skill, so it does
# not drift from SKILL.md (see the Cross-Repo Sync Checklist in AGENTS.md).
_INSTRUCTIONS = """\
建在 notebooklm-py 之上的薄 MCP + 確定性 podcast 續集工具。認證由環境變數
NOTEBOOKLM_AUTH_JSON 注入(通常來自 Doppler,唯讀)。

主流程(優先用高階工具,別自己拼低階步驟):
- 生成 → 預設 podcast_episode(manifest_path, source_ids=[…]);podcast_series 只在
  「共用 notebook + <=5 集 + 不指名來源」時用,且**一次呼叫會連續生完 episodes 清單
  裡的每一集**(清單放錯 = 整季內容錯置)。host 已凍 bundle 時
  podcast_episode(brief=null, input_bundle_path=...)(路徑與冪等規則見 skill)。
  有 manifest-backed attempt 時,依工具回傳的 safe_next_action 續跑。
- 來源 >= 10 筆時音檔入口全部 fail-closed(上限 9);重生不指名 source_ids
  **就是讀整本筆記本**,後面各集的回錄會洩進那一集。
- 發布成 Apple RSS → publish_series(讀 series_manifest.json;需 env
  PODCAST_PUBLIC_BASE_URL / PODCAST_TOKEN_SALT / PODCAST_UPLOAD_URL /
  PODCAST_UPLOAD_TOKEN)。
- 低階工具(notebook_* / source_* / artifact_* / chat_*)供救援與組裝,
  一般流程不需逐個手動呼叫。

鐵律:
- 任何長跑前先 auth_check(all_slots=true) —— **預設只量當下作用中那一槽**,而
  配額 failover 會中途換帳號,不傳旗標拿到的是 1/N 的綠燈。
- start=N 只是 execution lower bound/trust boundary,不會重生已完成集。
- 長 MCP request 可能被 client cancellation 終止；可靠性來自 manifest checkpoint
  與重呼,不保證 server 在背景跑完。未傳 manifest_path 的 standalone call 僅 best-effort。
- QA/protected facts 是可選 host workflow,不是 MCP lifecycle；外部 manifest writer
  必須改用 MCP tool 或同一 ManifestStore,不能直接覆寫 JSON。
- streamable-http / sse 模式「無認證」——勿綁非 loopback host。

完整路由與參數細節見 notebooklm skill(audi-skill/notebooklm)。
"""

mcp = FastMCP("notebooklm", instructions=_INSTRUCTIONS, lifespan=_lifespan)

# Register tools. Each module imports `mcp` from here and calls @mcp.tool().
from . import (  # noqa: E402,F401
    tools_artifacts,
    tools_basic,
    tools_podcast,
    tools_publish,
    tools_research,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="NotebookLM MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8484)
    parser.add_argument(
        "--allow-insecure-remote",
        action="store_true",
        help="允許無認證的 HTTP/SSE server 綁定非 loopback host",
    )
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        if not args.allow_insecure_remote and not _is_loopback_host(args.host):
            raise SystemExit(
                "HTTP/SSE transport 沒有認證；非 loopback host 必須明確傳入 --allow-insecure-remote"
            )
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)
