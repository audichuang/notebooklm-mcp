"""Holds the long-lived NotebookLMClient(s) set up by the server lifespan.

Tool functions can fetch it without threading it through every call, and tests
can monkeypatch it with a fake client.

多帳號(ADR-0010):lifespan 可以裝進 N 個帳號的 client。`get_client()` 的語意
**刻意維持不變**——「當前作用中的那一個」——所以 46 個呼叫點一行都不用改,pool
在這個模組之上是隱形的。切換由 `rotate_client()` 顯式驅動,只有配額 failover
那一條路徑會用到。
"""
from __future__ import annotations

import os
from typing import Any

AUTH_JSON_ENV = "NOTEBOOKLM_AUTH_JSON"

# (label, client, auth_json);label 是帳號 email(拿不到就退回 "#N"),用於稽核紀錄。
# auth_json 是該帳號的憑證,None 表示「不是 inline auth 建的」(單一 client 的舊路徑)。
_POOL: list[tuple[str, Any, str | None]] = []
_ACTIVE: int = 0


def _sync_auth_env() -> None:
    """讓 `NOTEBOOKLM_AUTH_JSON` 永遠等於**作用中帳號**的憑證。

    SDK 的媒體下載在下載當下**重讀這個 env**,不是用 client 自己的 session
    (v0.8.0 驗收 F-1,已隔離重現:同一個 client 只要把 env 換掉,下載身分就跟著換)。
    原本 pool 建完 env 停在最後一個槽位、還原寫在 lifespan 最外層的 finally
    ——那是 server 關閉才跑——於是整個 server 生命週期裡**所有下載都以最後一個帳號
    的身分發出**:notebook 沒分享給它就一律 401,而症狀出現在十幾分鐘後的 finalize,
    根因卻在這裡。

    pool 空的時候**不動 env**:清空由 lifespan 的 finally 做總還原,兩處都動會打架。
    """
    if not _POOL:
        return
    cred = _POOL[_ACTIVE][2]
    if cred is not None:
        os.environ[AUTH_JSON_ENV] = cred


def set_clients(entries: list[tuple]) -> None:
    """裝入整個 pool 並把作用中的位置重設回第一個。

    entry 是 `(label, client)` 或 `(label, client, auth_json)`;測試用的 fake client
    沒有真憑證,強迫它們補一個 None 只是噪音。
    """
    global _POOL, _ACTIVE
    _POOL = [(e[0], e[1], e[2] if len(e) > 2 else None) for e in entries]
    _ACTIVE = 0
    _sync_auth_env()


def set_client(client: Any) -> None:
    """單一 client 的舊介面(None = 清空)。測試與單帳號路徑都還在用。"""
    set_clients([] if client is None else [("#1", client)])


def get_client() -> Any:
    if not _POOL:
        raise RuntimeError("NotebookLM client not initialized (server lifespan not started)")
    return _POOL[_ACTIVE][1]


def account_count() -> int:
    return len(_POOL)


def active_account() -> str | None:
    return _POOL[_ACTIVE][0] if _POOL else None


def rotate_client() -> str | None:
    """切到下一個還沒用過的帳號,回傳它的 label;沒有下一個就回 None。

    **繞完一圈就停,不回到第一個**:呼叫端(配額 failover)必須能分辨「還有沒試過
    的帳號」與「全部都拒絕了」,無限輪替會把一次配額耗盡變成永遠重試。
    """
    global _ACTIVE
    if _ACTIVE + 1 >= len(_POOL):
        return None
    _ACTIVE += 1
    _sync_auth_env()
    return _POOL[_ACTIVE][0]
