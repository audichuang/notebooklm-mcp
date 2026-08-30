"""Holds the long-lived NotebookLMClient(s) set up by the server lifespan.

Tool functions can fetch it without threading it through every call, and tests
can monkeypatch it with a fake client.

多帳號(ADR-0010):lifespan 可以裝進 N 個帳號的 client。`get_client()` 的語意
**刻意維持不變**——「當前作用中的那一個」——所以 46 個呼叫點一行都不用改,pool
在這個模組之上是隱形的。切換由 `rotate_client()` 顯式驅動,只有配額 failover
那一條路徑會用到。

**這裡沒有鎖,也不需要有。** MCP 是並行的(`mcp/server/lowlevel/server.py` 對每則
incoming message `tg.start_soon`),而 `_ACTIVE` 是全域的 —— 但 pool 的每個成員都是
等價的長駐 client,「作用中是誰」本身不是需要互斥保護的資源。真正會出事的是**呼叫端
把 label 和 client 分兩次讀**(中間夾著 await,並行的 rotate 落在縫裡 → manifest 記 A、
實際 B 送出)。那個縫由 `snapshot()` 一次取用關掉,見它的 docstring。
"""
from __future__ import annotations

import time
from typing import Any

# (label, client);label 是帳號 email(拿不到就退回 "#N"),用於稽核紀錄。
#
# **憑證不再存在這裡**(v0.9.0 移除的第三元素):身分改由 client 自己帶著
# (`from_storage(path=…)` 建構時注入的 storage_path),不再靠覆寫 process 全域的
# `NOTEBOOKLM_AUTH_JSON`。理由見 `app.py` 的 `_write_credential_file`。
_POOL: list[tuple[str, Any]] = []
_ACTIVE: int = 0

# 槽位 index → 它上次被伺服器拒絕的 `time.monotonic()` 時刻。**這不是「今天已耗盡」**:
# v0.9.6 真實驗收量到同一個帳號被拒後 26 分鐘又被受理,所以 `RateLimitError` 常常只是
# 瞬時限流。冷卻期取得比那個觀測保守 —— 試錯成本是一次 RPC,丟錯成本是整個 process
# 少一個帳號可用。
_COOLING: dict[int, float] = {}
_COOLDOWN_SECONDS: float = 600.0

# 槽位 index → 啟動時算出來的**非機密**診斷(env 名稱、refreshable 與否、PSIDTS domain)。
#
# **這不是 v0.9.0 移除的那個第三元素。** 那個移除的是**憑證**——身分改由 client 自己
# 帶著(`from_storage(path=…)`),不再有第二份副本。這裡放的是啟動時就已經算完、而且
# 印在 log 裡的描述性資料,**不含 cookie 值**,單純因為 `auth_check` 事後問不到:
# routability 是從 storage_state 算的,而 pool 只留 `(label, client)`。
# pool 的 `(label, client)` 形狀刻意不動 —— 46 個呼叫點對此無感,是那個決定的重點。
_SLOT_DIAGNOSTICS: list[dict[str, Any]] = []


def set_clients(entries: list[tuple[str, Any]]) -> None:
    """裝入整個 pool 並把作用中的位置重設回第一個。entry 是 `(label, client)`。"""
    global _POOL, _ACTIVE, _COOLING, _SLOT_DIAGNOSTICS
    _POOL = [(label, client) for label, client in entries]
    _ACTIVE = 0
    # 重新裝 pool = 新的一輪(server 重啟走的就是這條),冷卻紀錄一起清掉。
    _COOLING = {}
    # 診斷跟著 pool 走:清掉才不會讓上一輪的 refreshable 留在新 pool 上冒充現況。
    # lifespan 會在 `set_clients` **之後**再塞進來。
    _SLOT_DIAGNOSTICS = []


def set_slot_diagnostics(rows: list[dict[str, Any]]) -> None:
    """記下每個槽位的非機密啟動診斷;**一定要在 `set_clients` 之後呼叫**(它會清空)。"""
    global _SLOT_DIAGNOSTICS
    _SLOT_DIAGNOSTICS = [dict(row) for row in rows]


def slot_diagnostics() -> list[dict[str, Any]]:
    """啟動診斷,依槽位順序;沒記錄過就是空 list(單帳號路徑、測試的 fake client)。"""
    return [dict(row) for row in _SLOT_DIAGNOSTICS]


def set_client(client: Any) -> None:
    """單一 client 的舊介面(None = 清空)。測試與單帳號路徑都還在用。"""
    set_clients([] if client is None else [("#1", client)])


def get_client() -> Any:
    if not _POOL:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    return _POOL[_ACTIVE][1]


def snapshot() -> tuple[str | None, Any]:
    """一次取出作用中的 `(label, client)`——**記帳點專用**。

    `active_account()` 與 `get_client()` 分開讀是有縫的:`tools_podcast` 在
    `_claim_prepared_dispatch(..., account=…)`(await)與真正 `generate(client)`
    之間隔著至少一次 await,而 MCP 是並行的 —— 另一個工具呼叫在那個縫裡撞到配額
    並 `rotate_client()`,manifest 就會記下 A、實際卻由 B 送出。

    ADR-0010 §Transparency 說得很明白:client 不會被告知是誰服務了這次呼叫,
    **manifest 是唯一的稽核憑據**。記錯帳等於那個憑據失真,而且事後無法發現
    (兩個帳號都成功,只是掛在錯的名下)。

    這個函式內部沒有 await,所以取出的兩個值必然屬於同一個槽位;呼叫端拿著
    `client` 往下走,之後任何 rotate 都影響不到這一次的送出。
    """
    if not _POOL:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    label, client = _POOL[_ACTIVE]
    return label, client


def account_count() -> int:
    return len(_POOL)


def active_account() -> str | None:
    return _POOL[_ACTIVE][0] if _POOL else None


def all_accounts() -> list[str]:
    """pool 裡每個帳號的 label,依槽位順序。"""
    return [label for label, _ in _POOL]


def all_clients() -> list[tuple[str, Any]]:
    """pool 裡每個 `(label, client)`,依槽位順序 —— **不動作用中游標**。

    給「這件事只有某個特定帳號做得到」的操作用,目前唯一的呼叫端是
    `notebook_share_with_pool`:既有 notebook 只有能看到它的那個帳號分享得動,
    而作用中帳號在 failover 之後**正好是看不到它的那一個**(v0.9.0 真實驗收
    Phase 9-1 抓到:停點的指引叫人跑那支工具,那支工具自己也 permission denied)。

    **刻意不提供「切到某個槽位」的 API**:`_ACTIVE` 的語意是「配額輪替走到哪」,
    讓別的功能去挪它會讓 failover 的帳號記帳失去意義。要用特定帳號就直接拿它的
    client 呼叫,游標不動。
    """
    return list(_POOL)


def rotate_client(
    refused: str | None = None, skip: frozenset[str] = frozenset()
) -> str | None:
    """把**真正被拒**的那個槽位標成冷卻中,切到下一個**不在冷卻中且不在 `skip` 裡**
    的帳號;繞完一圈都沒有就回 None。

    Args:
        refused: 這次真正被拒的帳號 label —— 呼叫端從自己的 `snapshot()` 拿到的那個,
            不是「現在的 `_ACTIVE`」。**參數是 label 不是 index**:呼叫端手上只有
            `snapshot()` 給的 label(它是拿去送出 RPC 的那個身分的唯一線索),
            index 是這個模組的內部表示,呼叫端從來沒有、也不該持有它。
            `None`(或反查不到,例如 pool 已重裝)= 保守退回冷卻當前 `_ACTIVE`
            (舊行為,相容沒有 snapshot 可用的呼叫端)。
        skip: 呼叫端這一批 failover 已經試過的 label(`tools_podcast._dispatch_
            audio_with_failover` 的 `tried` 集合)。**排除要發生在這裡的掃描裡,
            不能只擋回傳值**(這一輪 P1 修復):舊版本這個函式不知道 `skip` 是誰,
            只回「游標後方第一個不在冷卻中的槽位」——呼叫端事後發現那個槽位剛好
            試過,只能整批放棄,但游標後面可能還有完全沒試過、也沒在冷卻中的帳號
            (真實復現:pool A/B/C/D,tried={A,B},並行 request 把游標推到 D、A 的
            冷卻剛好到期——不掃描時直接跳過 tried 的話,回的是已經試過的 A,C 就這樣
            白白被漏試)。掃描時 `skip` 與冷卻是**兩個獨立條件**,都要滿足才是候選;
            冷卻副作用(標記 `refused` 的槽位)不受 `skip` 影響,永遠做。

    **為什麼不能無條件冷卻 `_ACTIVE`(v0.9.7 修復的並行缺陷)**:呼叫端是拿早先
    `snapshot()` 取到的 `(label, client)` 去送出的,中間隔著至少一次 await。並行下
    pool [a0,a1,a2] 兩個呼叫都 snapshot 到 a0、都被拒:A 先 rotate(冷卻 slot0、
    `_ACTIVE` 變 1);B 接著 rotate 時如果照舊冷卻「現在的 `_ACTIVE`」,冷的會是
    **從沒被試過的 a1**,而真正被拒的 a0 這次反而沒被記上(它已經在 slot0 冷卻過一次,
    但下一輪 A 若也還沒認識 a1,a1 就這樣平白少了一次候選機會)。改成「用 `refused`
    的 label 反查槽位去冷卻」之後,兩次呼叫冷卻的都是 slot0(同一顆),`_ACTIVE`
    的搜尋起點仍然用「當下的游標」——這樣才會撿到 A 已經推進過的位置,兩個呼叫最終
    會分別落在 a1、a2 上,沒有槽位被錯殺。**現在只有真的被拒的槽位會進冷卻,跳過去的
    那個下次照樣是候選**——這句話在並行下才終於成立。

    **終止性只在單次呼叫內有保證**:迴圈最多繞 `len(_POOL)` 步,全部冷卻中就回 None,
    不會無限迴圈。但**跨呼叫的收斂靠牆上時鐘**,不是這個函式能保證的:如果繞完一圈的
    耗時超過 `_COOLDOWN_SECONDS`,第一格的冷卻已經過期,下一輪又會被選中重試——這在
    正常配額 failover 的時間尺度下不是問題,但誠實地說,**這個函式本身不保證「同一批
    failover 不會重複試同一個已知失敗的帳號」**;真正防止在同一批 failover 內原地打轉
    的,是呼叫端(`tools_podcast._dispatch_audio_with_failover`)自己記的 tried set,
    不是這裡的冷卻表。

    冷卻用 `time.monotonic()`(不受系統時鐘調整影響)。`_COOLDOWN_SECONDS` 取得比
    v0.9.6 實測的 26 分鐘**保守**:寧可多花一次 RPC 去試、也不要白白丟掉一個還有額度
    的帳號 —— 試錯成本是一次 RPC,丟錯成本是整個 process 少一個帳號。

    仍然**沒有鎖**,理由見模組 docstring:真正會出事的是呼叫端把 label 與 client 分兩次
    讀,那個縫由 `snapshot()` 關掉,不是這裡。
    """
    global _ACTIVE
    if not _POOL:
        return None
    now = time.monotonic()
    # 反查 refused 對應的槽位;找不到(None、或 label 不在目前的 pool 裡,例如
    # pool 重裝過)就保守退回冷卻現在的 _ACTIVE —— 舊呼叫端相容。
    refused_index = _ACTIVE
    if refused is not None:
        for i, (label, _client) in enumerate(_POOL):
            if label == refused:
                refused_index = i
                break
    _COOLING[refused_index] = now
    for step in range(1, len(_POOL) + 1):
        candidate = (_ACTIVE + step) % len(_POOL)
        label = _POOL[candidate][0]
        if label in skip:
            continue
        cooled_at = _COOLING.get(candidate)
        if cooled_at is None or now - cooled_at >= _COOLDOWN_SECONDS:
            _ACTIVE = candidate
            return label
    return None
