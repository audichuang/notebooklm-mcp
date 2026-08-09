"""Holds the long-lived NotebookLMClient(s) set up by the server lifespan.

Tool functions can fetch it without threading it through every call, and tests
can monkeypatch it with a fake client.

多帳號(ADR-0010):lifespan 可以裝進 N 個帳號的 client。`get_client()` 的語意
**刻意維持不變**——「當前作用中的那一個」——所以 46 個呼叫點一行都不用改,pool
在這個模組之上是隱形的。切換由 `rotate_client()` 顯式驅動,只有配額 failover
那一條路徑會用到。
"""
from __future__ import annotations

from typing import Any

# (label, client);label 是帳號 email(拿不到就退回 "#N"),用於稽核紀錄。
_POOL: list[tuple[str, Any]] = []
_ACTIVE: int = 0


def set_clients(pairs: list[tuple[str, Any]]) -> None:
    """裝入整個 pool 並把作用中的位置重設回第一個。"""
    global _POOL, _ACTIVE
    _POOL = list(pairs)
    _ACTIVE = 0


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
    return _POOL[_ACTIVE][0]
