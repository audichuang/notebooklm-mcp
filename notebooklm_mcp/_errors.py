"""跨工具共用的錯誤型別與判別。

`NotebookAccessDenied` 與 `_is_permission_denied` 原本住在 `tools_podcast.py`,
v0.9.0 真實驗收(Phase 9-1)之後 `tools_basic.notebook_share_with_pool` 也要判同一件事
——**不能各寫一份**:上游哪天改了 `rpc_code` 的語意或欄位名,兩處只會有一處被改到,
而這正是本 repo 反覆出事的「補一半」。
"""
from __future__ import annotations

from notebooklm.exceptions import ClientError

# gRPC PERMISSION_DENIED。pool 換到的帳號看不到那個 notebook 時就是這個。
_RPC_PERMISSION_DENIED = 7


class NotebookAccessDenied(RuntimeError):
    """pool 裡的這個帳號看不到目標 notebook(v0.8.0 驗收 F-2)。

    **刻意繼承 `RuntimeError`**:兩個 dispatch 呼叫端本來就把 `RuntimeError` 當作
    「沒建出 task 的乾淨終態」處理(`podcast_series` 回結構化安全停點、
    `_run_episode` 原樣重拋),所以分類自動正確,不必在兩處各加一個分支——那正是
    本 repo 反覆出事的「補一半」。

    **不放進 `_REFUSED_WITHOUT_DISPATCH`**:那個集合的契約是「配額/限流」,
    AGENTS.md 明令它不准長大;而且權限問題不該觸發 failover。
    """


def is_permission_denied(exc: BaseException) -> bool:
    """這個例外是不是「這個帳號看不到那個 notebook」。

    `rpc_code` 的型別在上游是 `str | int | None`,所以兩種形狀都比。
    contract 測試 `test_client_error_still_carries_rpc_code` 守著這個欄位還在。
    """
    if not isinstance(exc, ClientError):
        return False
    code = getattr(exc, "rpc_code", None)
    return code == _RPC_PERMISSION_DENIED or str(code) == str(_RPC_PERMISSION_DENIED)
