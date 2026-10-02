"""跨工具共用的錯誤型別與判別。

`NotebookAccessDenied` 與 `_is_permission_denied` 原本住在 `tools_podcast.py`,
v0.9.0 真實驗收(Phase 9-1)之後 `tools_basic.notebook_share_with_pool` 也要判同一件事
——**不能各寫一份**:上游哪天改了 `rpc_code` 的語意或欄位名,兩處只會有一處被改到,
而這正是本 repo 反覆出事的「補一半」。
"""

from __future__ import annotations

from contextlib import contextmanager

from notebooklm.exceptions import ClientError
from notebooklm.rpc.types import GrpcStatusCode, normalize_rpc_code


class NotebookAccessDenied(RuntimeError):
    """pool 裡的這個帳號看不到目標 notebook(v0.8.0 驗收 F-2)。

    **刻意繼承 `RuntimeError`**:兩個 dispatch 呼叫端本來就把 `RuntimeError` 當作
    「沒建出 task 的乾淨終態」處理(`podcast_series` 回結構化安全停點、
    `_run_episode` 原樣重拋),所以分類自動正確,不必在兩處各加一個分支——那正是
    本 repo 反覆出事的「補一半」。

    **不放進 `_REFUSED_WITHOUT_DISPATCH`**:那個集合的契約是「配額/限流」,
    AGENTS.md 明令它不准長大;而且權限問題不該觸發 failover。
    """


def access_denied_error(
    exc: BaseException,
    *,
    notebook_id: str | None = None,
    account: str | None = None,
) -> NotebookAccessDenied:
    """把權限錯誤翻成**帶下一步動作**的 `NotebookAccessDenied` —— 這段文字的唯一產地。

    v0.9.16 之前這段話有**兩份**:一份在這裡(`raise_if_access_denied`,指名 notebook),
    一份在 `tools_podcast` 的 failover 迴圈裡(指名帳號)。內容一樣、結尾一句還不一樣,
    正是本檔頂端那條紀律要擋的形狀 —— 訊息要改(例如工具改名)只會有一處被改到。
    合成一支之後兩邊都指名得出來,而且比原來任一份都完整:`account` 講「誰被拒」,
    `notebook_id` 講「哪一本」,failover 那條路兩個都有。
    """
    # 兩個都是可選的,而缺哪一個都要退回 v0.9.16 之前那兩份**逐字相同**的措辭 ——
    # 既有測試只 match「分享」,但這段話會被寫進 manifest 當稽核紀錄、也會原樣回給
    # 呼叫端當停點指引,無謂的措辭漂移只會讓事後比對紀錄的人以為換了條路。
    # (合併的第一版真的漂了:少一個空格、還刪掉結尾的「既有 notebook」,
    #  由 codex 獨立複審抓出來 —— `test_permission_denied_message_is_stable` 現在釘住它。)
    subject = f"帳號 {account!r} " if account else "這個帳號"
    target = f"對 notebook {notebook_id} " if notebook_id else "對這個 notebook "
    return NotebookAccessDenied(
        f"{exc}\n{subject}{target}沒有存取權。"
        "多帳號 pool 模式要求 notebook 對 pool 全員可存取 —— 呼叫 "
        "notebook_share_with_pool(notebook_id=...) 補分享給其餘帳號(EDITOR)後再重試;"
        "MCP 自建 notebook(v0.8.1 起)已自動分享,這通常是舊版建立或在網頁上手動建立的"
        "既有 notebook。"
    )


def raise_if_access_denied(exc: BaseException, notebook_id: str) -> None:
    """權限被拒就翻成帶指引的 `NotebookAccessDenied`,其餘原樣放行給呼叫端重拋。

    **v0.9.14 真實驗收 FINDING-F**:這段訊息原本只長在 `_sources._list_sources` 裡,
    於是 `notebook_get` 撞到同一件事時裸拋上游的 `ClientError` —— 而上游那句講的是
    `authuser` account-routing、指向 SDK issue #114/#294,**與 pool 情境無關且沒有任何
    修復指引**。skill 正是引導呼叫端「生成前先 `notebook_get` 確認目標對不對」,
    所以那是實務上最先撞到的一支。兩處共用同一段文字,不再各寫一份(同本檔頂端的紀律)。

    只轉權限那一種:網路錯誤、認證過期一路吞下去只會把根因埋掉。
    """
    if not is_permission_denied(exc):
        return
    raise access_denied_error(exc, notebook_id=notebook_id) from exc


def is_permission_denied(exc: BaseException) -> bool:
    """這個例外是不是「這個帳號看不到那個 notebook」。

    gRPC 狀態碼的定義與正規化由上游 API 統一處理,我們不再自行記住數字 7。
    contract 測試 `test_client_error_still_carries_rpc_code` 守著這個欄位還在。
    """
    # 承重判準：_sources._list_sources 與 tools_basic.notebook_share_with_pool 都依賴它；
    # 退化會把 not-found 誤判成權限問題，進而繼續白跑整個 pool。
    if not isinstance(exc, ClientError):
        return False
    return normalize_rpc_code(getattr(exc, "rpc_code", None)) == GrpcStatusCode.PERMISSION_DENIED


@contextmanager
def reconcile_hint_if_unconfirmed(notebook_id: str):
    """來源建立「已送出、結果不明」時,在例外訊息補上「先對帳再重試」—— 這段話的唯一產地。

    notebooklm-py 0.8.3 起來源建立不再 probe/重送(retry-unsafe write),傳輸在送出後斷掉
    就原樣 raise 並掛 `unconfirmed`。型別與訊息跟普通網路錯誤一模一樣,host 照直覺重試
    就多一筆重複來源。只改 `args`、原樣重拋,型別與 SDK 的結構化欄位都保留。
    """
    try:
        yield
    except Exception as exc:
        if getattr(exc, "unconfirmed", False):
            known = getattr(exc, "source_id", None)
            candidates = [c for c in getattr(exc, "reconciliation_candidates", ()) or () if c]
            if known and known not in candidates:
                candidates.insert(0, known)
            report = getattr(getattr(exc, "operation_metadata", None), "reconciliation", None)
            unresolved = list(getattr(report, "unresolved_inputs", ()) or ())
            exc.args = (
                f"{exc}\n送出後結果不明:來源**可能已經建立**。先 "
                f"source_list(notebook_id={notebook_id!r}) 對帳,確認沒有才重試 —— "
                "直接重試可能產生重複來源。"
                + (f" 上游看到的候選 source_id:{candidates}。" if candidates else "")
                + (f" 上游無法對上的輸入:{unresolved}。" if unresolved else ""),
            )
        raise
