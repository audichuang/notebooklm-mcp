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
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from notebooklm import NotebookLMClient
from notebooklm.auth import MINIMUM_REQUIRED_COOKIES, extract_cookies_from_storage

from . import runtime

logger = logging.getLogger(__name__)

_AUTH_JSON_ENV = "NOTEBOOKLM_AUTH_JSON"
_DISABLE_KEEPALIVE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"
_HEADLESS_REAUTH_ENV = "NOTEBOOKLM_HEADLESS_REAUTH"
_REFRESH_CMD_ENV = "NOTEBOOKLM_REFRESH_CMD"

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
# 三者都只在 inline 模式壓:登入機讀本機 storage_state 時,重鑄後寫得回檔案,是對的行為。
_INLINE_AUTH_ENV_OVERRIDES: dict[str, str | None] = {
    _DISABLE_KEEPALIVE_ENV: "1",
    _HEADLESS_REAUTH_ENV: None,
    _REFRESH_CMD_ENV: None,
}


# pool 掃描的上限。只是「打錯字偵測」的搜尋範圍,不是帳號數上限的產品決策。
_MAX_POOL_SLOTS = 20


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
    含例外路徑,整個目錄刪除)——同機器上的同一個 user 讀得到。**那個 user 本來就
    讀得到 `/proc/<pid>/environ`**,所以攻擊面沒有實際擴大。

    落檔前先驗一次憑證形狀,不只是為了早點爆:給了 path 之後,SDK 的 **L2 inline
    PSIDTS recovery**(`_auth/psidts_recovery.py` 的 `_resolve_recovery_path`:env 模式
    回 None 而拒絕、有 path 就接受)會重新武裝,而它**不受
    `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` 管**,會在本 process 內發一次 RotateCookies。
    它唯一的入口是 strict loader 丟 ValueError 那條 `except`,所以先在這裡用**同樣的
    條件**驗過,那條路就到不了:「不在本 process 內重鑄 cookie」的紀律維持不變,
    而缺憑證仍然是啟動時的大聲失敗(現況),不會被靜默治好 —— ADR-0010 §Transparency
    最怕的正是 pool 默默吸收一個快死的憑證。

    **「同樣的條件」是這裡的全部重點,而它曾經不成立**:`extract_cookies_from_storage`
    (`_auth/cookies.py:216`)只看 `name`,空字串 value 照樣算「這個 cookie 存在」;
    strict loader(同檔 `:446`)則是 `… or not name or not value`,空值等於不存在。
    於是 `__Secure-1PSIDTS: ""` 的憑證預驗證放行、SDK 開檔時 raise,recovery 真的
    發出 RotateCookies POST(實跑重現過)。所以必要 cookie 的**值**也要在這裡驗。
    兩者仍非逐字等價:同名 cookie 跨網域時 extract 取優先網域那一筆、strict 取任一
    非空的,所以「高優先網域空值 + 低優先網域有值」我們會拒、SDK 會收。方向是
    fail-closed(啟動時大聲失敗),可接受;真正的等價前提由
    `tests/test_client_pool.py::test_precheck_agrees_with_the_sdk_strict_loader` 守著。
    """
    name = _slot_env_name(slot)
    try:
        cookies = extract_cookies_from_storage(json.loads(cred))
        if blank := sorted(n for n in MINIMUM_REQUIRED_COOKIES if not cookies.get(n)):
            raise ValueError(f"必要 cookie 的值是空的:{blank}")
    except Exception as exc:  # ValueError(JSON / 缺 cookie / 空值)、型別不對…一律具名重拋
        raise RuntimeError(
            f"{name} 不是可用的 storage_state:{type(exc).__name__}: {exc}"
        ) from exc
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
    except Exception as exc:
        logger.warning(
            "第 %d 個帳號取不到 email,退回槽位編號 #%d(pool 的稽核標籤會少一個真名,"
            "分享/對帳工具會據此要求人工介入):%s: %s",
            slot,
            slot,
            type(exc).__name__,
            exc,
        )
        return f"#{slot}"
    return email or f"#{slot}"


@contextlib.asynccontextmanager
async def _lifespan(_app: FastMCP) -> AsyncIterator[None]:
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
                #     下一個 0700 目錄,同 user 本來就讀得到 `/proc/<pid>/environ`。
                #   - SIGTERM 不展開 AsyncExitStack,憑證會原封留在 $TMPDIR 直到重開機。
                # **不要改成「啟動時掃掉舊目錄」** —— 同機並行的 MCP process 會互刪。
                stack.callback(shutil.rmtree, cred_dir, ignore_errors=True)
                for slot, cred in enumerate(creds, start=1):
                    path = _write_credential_file(cred, cred_dir / f"slot-{slot}.json", slot)
                    client = await stack.enter_async_context(
                        NotebookLMClient.from_storage(path=str(path))
                    )
                    pool.append((await _account_label(client, slot), client))
            else:
                client = await stack.enter_async_context(NotebookLMClient.from_storage())
                pool.append((await _account_label(client, 1), client))
            _reject_duplicate_accounts(pool)
            runtime.set_clients(pool)
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
- 整季/一般單集生成 → podcast_series(episodes 放一集即單集)。若 host 已建立
  frozen generation input bundle，改用 podcast_episode(brief=null, manifest_path=...,
  input_bundle_path=...)(路徑與冪等規則見 skill)。有 manifest-backed attempt 時，
  依工具回傳的 safe_next_action 續跑。
- 發布成 Apple RSS → publish_series(讀 series_manifest.json;需 env
  PODCAST_PUBLIC_BASE_URL / PODCAST_TOKEN_SALT / PODCAST_UPLOAD_URL /
  PODCAST_UPLOAD_TOKEN)。
- 低階工具(notebook_* / source_* / artifact_* / chat_*)供救援與組裝,
  一般流程不需逐個手動呼叫。

鐵律:
- 任何長跑前先 auth_check;cookie 死了秒退,別燒掉數小時。
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
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)
