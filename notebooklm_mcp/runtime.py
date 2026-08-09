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

from typing import Any

# (label, client);label 是帳號 email(拿不到就退回 "#N"),用於稽核紀錄。
#
# **憑證不再存在這裡**(v0.9.0 移除的第三元素):身分改由 client 自己帶著
# (`from_storage(path=…)` 建構時注入的 storage_path),不再靠覆寫 process 全域的
# `NOTEBOOKLM_AUTH_JSON`。理由見 `app.py` 的 `_write_credential_file`。
_POOL: list[tuple[str, Any]] = []
_ACTIVE: int = 0


def set_clients(entries: list[tuple[str, Any]]) -> None:
    """裝入整個 pool 並把作用中的位置重設回第一個。entry 是 `(label, client)`。"""
    global _POOL, _ACTIVE
    _POOL = [(label, client) for label, client in entries]
    _ACTIVE = 0


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


def rotate_client() -> str | None:
    """切到下一個還沒用過的帳號,回傳它的 label;沒有下一個就回 None。

    **繞完一圈就停,不回到第一個**:呼叫端(配額 failover)必須能分辨「還有沒試過
    的帳號」與「全部都拒絕了」,無限輪替會把一次配額耗盡變成永遠重試。

    **已知限制:游標是按「被拒次數」前進,不是按「帳號狀態」。** 兩個並行呼叫同時撞到
    配額時,游標會被推兩格,第二個呼叫拿到 `None` 就回報「所有帳號都拒絕了」——其實
    中間那個從來沒被試過。不該用鎖修(問題不是互斥,是這個游標語意本身),真要修得讓
    每個帳號帶自己的「今天是否已耗盡」狀態。實務上代價只是那一輪少試一個帳號、下一次
    呼叫(新 process 或隔天)照樣會從頭輪,所以留成已知取捨。
    """
    global _ACTIVE
    if _ACTIVE + 1 >= len(_POOL):
        return None
    _ACTIVE += 1
    return _POOL[_ACTIVE][0]
